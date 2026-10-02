import gzip
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backlog_harness.evidence import read_jsonl
from backlog_harness.telemetry import MAX_BODY, TelemetryReceiver


def payload():
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "native"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "codex"},
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "parentSpanId": "c" * 16,
                                "name": "turn",
                                "attributes": [],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def send(dest, data, token=None, encoding="identity"):
    request = Request(
        dest.endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": encoding,
            "x-harness-invocation": dest.token if token is None else token,
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def test_http_attribution_retry_and_gzip(tmp_path):
    with TelemetryReceiver(tmp_path) as receiver:
        a = receiver.register(
            run_id="r",
            invocation_id="a",
            role="worker",
            adapter="codex",
            item_id="../../opaque",
            provider_id="file",
        )
        b = receiver.register(run_id="r", invocation_id="b", role="coordinator", adapter="codex")
        raw = json.dumps(payload()).encode()
        assert send(a, raw, token="wrong") == 403
        assert send(a, raw) == 200
        assert send(a, gzip.compress(raw), encoding="gzip") == 200
        assert send(b, raw) == 200
        rows, _, partial = read_jsonl(a.path)
        assert len(rows) == 1 and not partial
        span = rows[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["parentSpanId"] == "c" * 16
        attrs = {v["key"]: v["value"]["stringValue"] for v in span["attributes"]}
        assert attrs["harness.work_item.id"] == "../../opaque"
        assert a.token not in a.path.read_text()
        assert a.path != b.path
        assert receiver.report(a)["span_count"] == 1


def test_invalid_conflict_oversize_and_durability_failure(tmp_path, monkeypatch):
    with TelemetryReceiver(tmp_path) as receiver:
        dest = receiver.register(run_id="r", invocation_id="i", role="worker", adapter="codex")
        assert send(dest, b"{") == 400
        assert send(dest, gzip.compress(b"x" * (MAX_BODY + 1)), encoding="gzip") == 413
        value = payload()
        value["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
            {"key": "harness.invocation.id", "value": {"stringValue": "spoof"}}
        ]
        assert send(dest, json.dumps(value).encode()) == 400

        def fail(_):
            raise OSError("disk full")

        monkeypatch.setattr(receiver.sinks[dest.token].writer, "append", fail)
        assert send(dest, json.dumps(payload()).encode()) == 503
        assert not dest.path.exists()


def test_storage_failure_remains_fenced_until_same_export_is_durable(tmp_path, monkeypatch):
    with TelemetryReceiver(tmp_path) as receiver:
        dest = receiver.register(run_id="r", invocation_id="retry", role="worker", adapter="codex")
        sink = receiver.sinks[dest.token]
        native_append = sink.writer.append
        attempts = 0

        def fail_once(value):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary disk failure")
            native_append(value)

        monkeypatch.setattr(sink.writer, "append", fail_once)
        raw = json.dumps(payload()).encode()
        assert send(dest, raw) == 503
        assert receiver.report(dest)["rejected_exports"] == 1
        different = payload()
        different["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "d" * 16
        assert send(dest, json.dumps(different).encode()) == 200
        assert receiver.report(dest)["rejected_exports"] == 1
        assert send(dest, raw) == 200
        report = receiver.report(dest)
        assert report["rejected_exports"] == 0
        assert report["storage_failures"] == 1
        assert report["span_count"] == 2
        assert not report["unresolved_retries"]
        assert send(dest, raw) == 200
        assert len(read_jsonl(dest.path)[0]) == 2


def test_partial_export_retry_does_not_clear_whole_batch_gap(tmp_path, monkeypatch):
    from copy import deepcopy

    with TelemetryReceiver(tmp_path) as receiver:
        dest = receiver.register(run_id="r", invocation_id="batch", role="worker", adapter="codex")
        sink = receiver.sinks[dest.token]
        append = sink.writer.append
        batch = payload()
        first = deepcopy(batch["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        first["spanId"] = "d" * 16
        batch["resourceSpans"][0]["scopeSpans"][0]["spans"].insert(0, first)
        monkeypatch.setattr(
            sink.writer, "append", lambda value: (_ for _ in ()).throw(OSError("disk full"))
        )
        assert send(dest, json.dumps(batch).encode()) == 503
        monkeypatch.setattr(sink.writer, "append", append)
        assert send(dest, json.dumps(payload()).encode()) == 200
        assert receiver.report(dest)["rejected_exports"] == 1
        assert send(dest, json.dumps(batch).encode()) == 200
        assert receiver.report(dest)["rejected_exports"] == 0


def test_malformed_native_shapes_never_enter_evidence():
    from copy import deepcopy

    import pytest

    from backlog_harness.telemetry import InvalidPayload, normalize

    variants = [
        [],
        {"unknown": []},
        {"resourceSpans": {}},
        {"resourceSpans": [None]},
        {"resourceSpans": [{"scopeSpans": [None]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [None]}]}]},
    ]
    for field, value in [
        ("traceId", "0" * 32),
        ("spanId", None),
        ("parentSpanId", "invalid"),
        ("parentSpanId", 1),
        ("attributes", [None]),
        ("attributes", [{"key": 1}]),
    ]:
        variant = deepcopy(payload())
        variant["resourceSpans"][0]["scopeSpans"][0]["spans"][0][field] = value
        variants.append(variant)
    for variant in variants:
        with pytest.raises(InvalidPayload):
            normalize(variant, {})


def test_http_framing_errors_are_rejected_without_persisting(tmp_path):
    import http.client
    import socket

    cases = [
        ("/wrong", {}, b"{}", 404),
        ("/v1/traces", {"Transfer-Encoding": "chunked"}, b"", 400),
        ("/v1/traces", {"Content-Length": "-1"}, b"", 400),
        ("/v1/traces", {"Content-Length": str(MAX_BODY + 1)}, b"", 413),
        ("/v1/traces", {"Content-Type": "text/plain"}, b"{}", 400),
        ("/v1/traces", {"Content-Length": "10"}, b"{}", 400),
        ("/v1/traces", {"Content-Encoding": "gzip"}, gzip.compress(b"{}")[:-2], 400),
        ("/v1/traces", {"Content-Encoding": "gzip"}, gzip.compress(b"{}") + b"extra", 400),
        ("/v1/traces", {"Content-Encoding": "brotli"}, b"{}", 400),
        ("/v1/traces", {}, b'{"resourceSpans":NaN}', 400),
    ]
    with TelemetryReceiver(tmp_path) as receiver:
        dest = receiver.register(run_id="run", invocation_id="bad", role="worker", adapter="codex")
        for path, overrides, body, expected in cases:
            connection = http.client.HTTPConnection(
                "127.0.0.1", receiver.server.server_port, timeout=5
            )
            connection.putrequest("POST", path)
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "x-harness-invocation": dest.token,
                **overrides,
            }
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders(body)
            connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            assert response.status == expected, (path, overrides)
            response.read()
            connection.close()
        assert not dest.path.exists()


def test_registration_and_retained_evidence_conflicts(tmp_path):
    import pytest

    from backlog_harness.evidence import EvidenceError
    from backlog_harness.telemetry import Sink

    with TelemetryReceiver(tmp_path) as receiver:
        with pytest.raises(ValueError, match="provider identity"):
            receiver.register(
                run_id="r", invocation_id="i", role="worker", adapter="codex", item_id="item"
            )
        dest = receiver.register(run_id="r", invocation_id="i", role="worker", adapter="codex")
        with pytest.raises(ValueError, match="already registered"):
            receiver.register(run_id="r", invocation_id="i", role="worker", adapter="codex")
        assert send(dest, json.dumps(payload()).encode()) == 200
        different = payload()
        different["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = "conflict"
        assert send(dest, json.dumps(different).encode()) == 400
        assert receiver.report(dest)["span_count"] == 1
    dest.path.write_text('{"partial":')
    with pytest.raises(EvidenceError, match="incomplete line"):
        Sink(dest.path, {})


def test_unexpected_storage_error_reports_failure(tmp_path, monkeypatch):
    with TelemetryReceiver(tmp_path) as receiver:
        dest = receiver.register(run_id="r", invocation_id="i", role="worker", adapter="codex")

        def fail(_):
            raise OSError("storage unavailable")

        monkeypatch.setattr(receiver.sinks[dest.token], "write", fail)
        assert send(dest, json.dumps(payload()).encode()) == 503
        assert receiver.report(dest)["rejected_exports"] == 1


def test_reopened_sink_preserves_deduplication_and_rejects_conflicting_history(tmp_path):
    from copy import deepcopy

    import pytest

    from backlog_harness.telemetry import InvalidPayload, Sink, normalize

    path = tmp_path / "spans.jsonl"
    correlation = {"harness.invocation.id": "invocation"}
    native = payload()
    native["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
        {"key": "native.detail", "value": {"stringValue": "preserved"}}
    ]
    normalized = normalize(native, correlation)
    # Matching preexisting attribution is preserved once, never duplicated.
    assert normalize(normalized, correlation) == normalized
    sink = Sink(path, correlation)
    sink.write(normalized)
    original = path.read_bytes()
    recovered = Sink(path, correlation)
    assert recovered.evidence_digest() == sink.evidence_digest()
    recovered.write(normalized)
    assert path.read_bytes() == original
    duplicate = deepcopy(normalized)
    attributes = duplicate["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
    attributes.append(attributes[-1])
    with pytest.raises(InvalidPayload, match="Conflicting reserved"):
        recovered.write(duplicate)
    changed = deepcopy(normalized)
    changed["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = "conflicting"
    path.write_bytes(original + json.dumps(changed).encode() + b"\n")
    with pytest.raises(InvalidPayload, match="Conflicting native span identity"):
        Sink(path, correlation)
