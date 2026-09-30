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
