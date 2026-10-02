import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from backlog_harness.dashboard import create_dashboard_server


def test_dashboard_serves_exact_projection_and_rejects_mutation():
    projection = {
        "revision": "same",
        "items": [{"item_id": "one", "state": "Completed"}],
        "traces": [],
    }
    with create_dashboard_server(lambda: projection) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(url + "/api/snapshot") as response:
                assert json.load(response) == projection
            try:
                urlopen(Request(url + "/api/snapshot", data=b"{}"))
            except HTTPError as error:
                assert error.code == 405
            else:
                raise AssertionError("Mutation was accepted")
            try:
                urlopen(Request(url + "/api/snapshot", headers={"Host": "attacker.example"}))
            except HTTPError as error:
                assert error.code == 403
            else:
                raise AssertionError("Untrusted Host was accepted")
        finally:
            server.shutdown()
            thread.join()


def test_trace_projection_reads_only_appended_complete_lines_and_orders_by_time(
    config_file, monkeypatch
):
    from backlog_harness import projections
    from backlog_harness.application import Application
    from backlog_harness.evidence import JsonlWriter

    config, _ = config_file
    app = Application(config)
    path = app.root / "telemetry/runs/run/invocation/spans.jsonl"

    def payload(identity, timestamp):
        return {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "a" * 32,
                                    "spanId": identity * 16,
                                    "name": identity,
                                    "endTimeUnixNano": str(timestamp),
                                    "attributes": [],
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    JsonlWriter(path).append(payload("b", 20))
    calls = []
    reader = projections.read_jsonl

    def observe(path, offset=0):
        calls.append(offset)
        return reader(path, offset)

    monkeypatch.setattr(projections, "read_jsonl", observe)
    count, rows, uncertainty = projections.trace_snapshot(app)
    assert count == 1 and not uncertainty
    offset = path.stat().st_size
    assert projections.trace_snapshot(app) == (count, rows, uncertainty)
    assert calls == [0]
    JsonlWriter(path).append(payload("c", 10))
    count, rows, uncertainty = projections.trace_snapshot(app)
    assert count == 2 and [r["name"] for r in rows] == ["c", "b"]
    assert calls == [0, offset]
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    count, rows, uncertainty = projections.trace_snapshot(app)
    assert count == 2 and uncertainty == ["partial telemetry:invocation"]
    assert app.trace_cache[path]["offset"] < path.stat().st_size


def test_projection_rejects_provider_change_instead_of_mixing_counts(
    config_file, provider, monkeypatch
):
    import pytest
    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceError
    from backlog_harness.projections import snapshot

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    original = app.provider.snapshot
    calls = 0

    def changing():
        nonlocal calls
        calls += 1
        return original() if calls == 1 else []

    from backlog_harness.provider import FileProvider

    monkeypatch.setattr(FileProvider, "snapshot", lambda self: changing())
    with pytest.raises(EvidenceError, match="Provider changed"):
        snapshot(app)


def test_long_lived_view_observes_invalid_configuration_without_reusing_generation_settings(
    config_file, provider
):
    import yaml

    from backlog_harness.application import Application
    from backlog_harness.projections import snapshot

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    assert snapshot(app)["generation_configuration_error"] is None
    data.pop("profiles")
    config.write_text(yaml.safe_dump(data))
    value = snapshot(app)
    assert value["generation_configuration_error"]
    assert value["items"][0]["usage"]["may_generate"] is False
    assert "profiles" in app.config.data


@pytest.mark.parametrize("receipt_location", ["direct", "relocated_alias", "outside_inventory"])
def test_cold_projection_rejects_same_count_content_change(
    config_file, provider, tmp_path, receipt_location
):
    import pytest
    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceError, EvidenceStore, JsonlWriter, atomic_json
    from backlog_harness.projections import snapshot
    from backlog_harness.telemetry import TelemetryReceiver

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    store = EvidenceStore(app.root, "run")
    path = store.begin("op", "inv", app.config, app.config.binding("coordinator"), action="test")
    EvidenceStore.requested(path)
    JsonlWriter(path / "events.jsonl").append(
        {"at": "2026-09-30T21:00:00+00:00", "type": "turn.completed"}
    )
    EvidenceStore.outcome(path, "returned")
    with TelemetryReceiver(app.root) as receiver:
        dest = receiver.register(
            run_id="run", invocation_id="inv", role="coordinator", adapter="codex"
        )
        value = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "name": "original"}]}
                    ]
                }
            ]
        }
        receiver.sinks[dest.token].write(value)
        atomic_json(path / "telemetry.json", {"path": str(dest.path)})
        atomic_json(path / "telemetry-report.json", receiver.report(dest))
    if receipt_location != "direct":
        alias = tmp_path / "old-evidence-root"
        alias.symlink_to(app.root, target_is_directory=True)
        receipt_path = alias / dest.path.relative_to(app.root)
        if receipt_location == "outside_inventory":
            other = app.root / "non-inventory-spans.jsonl"
            other.write_bytes(dest.path.read_bytes())
            receipt_path = alias / other.relative_to(app.root)
        atomic_json(path / "telemetry.json", {"path": str(receipt_path)})
    if receipt_location == "outside_inventory":
        with pytest.raises(EvidenceError, match="outside the projection inventory"):
            snapshot(app)
        return
    assert snapshot(app)["trace_span_count"] == 1
    assert snapshot(app)["trace_span_count"] == 1
    dest.path.write_text(dest.path.read_text().replace("original", "changed"))
    cold = Application(config)
    with pytest.raises(EvidenceError, match="receipt mismatch"):
        snapshot(cold)


@pytest.mark.parametrize("change", ["shrink", "rewrite", "conflict", "replace", "remove"])
def test_trace_cache_handles_changed_evidence_without_silent_stale_rows(tmp_path, change):
    import os
    from types import SimpleNamespace

    from backlog_harness.evidence import EvidenceError, JsonlWriter
    from backlog_harness.projections import trace_snapshot

    app = SimpleNamespace(root=tmp_path, trace_cache={})
    path = tmp_path / "telemetry/runs/run/invocation/spans.jsonl"
    value = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "name": "original"}]}
                ]
            }
        ]
    }
    JsonlWriter(path).append(value)
    assert trace_snapshot(app)[0] == 1
    if change == "shrink":
        path.write_bytes(b"")
    elif change == "rewrite":
        stat = path.stat()
        path.write_text(path.read_text().replace("original", "modified"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    elif change == "conflict":
        value["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = "different"
        JsonlWriter(path).append(value)
    elif change == "replace":
        replacement = path.with_name("replacement")
        replacement.write_text(path.read_text().replace("original", "modified"))
        replacement.replace(path)
        assert trace_snapshot(app)[1][0]["name"] == "modified"
        return
    else:
        path.unlink()
        assert trace_snapshot(app) == (0, [], [])
        assert not app.trace_cache
        return
    with pytest.raises(EvidenceError, match="shrank|rewritten|Conflicting"):
        trace_snapshot(app)


def test_cross_file_span_duplicates_are_not_double_counted_and_conflicts_are_visible(tmp_path):
    from types import SimpleNamespace

    from backlog_harness.evidence import JsonlWriter
    from backlog_harness.projections import trace_snapshot

    app = SimpleNamespace(root=tmp_path, trace_cache={})
    value = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "name": "original"}]}
                ]
            }
        ]
    }
    for name in ("one", "two"):
        JsonlWriter(tmp_path / f"telemetry/runs/run/{name}/spans.jsonl").append(value)
    assert trace_snapshot(app)[0] == 1
    assert not trace_snapshot(app)[2]
    value["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = "different"
    JsonlWriter(tmp_path / "telemetry/runs/run/three/spans.jsonl").append(value)
    count, _, uncertainty = trace_snapshot(app)
    assert count == 1
    assert uncertainty == ["conflicting trace span:" + "b" * 16]


@pytest.mark.parametrize("fault", ["missing-receipt", "rejected", "missing-file", "outside-root"])
def test_missing_trace_receipts_are_reported_as_uncertainty(config_file, tmp_path, fault):
    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceStore, atomic_json
    from backlog_harness.projections import snapshot

    app = Application(config_file[0])
    (app.config.repository / "backlog").mkdir()
    store = EvidenceStore(app.root, "run")
    path = store.begin("op", "inv", app.config, app.config.binding("coordinator"), action="test")
    store.requested(path)
    store.outcome(path, "returned")
    if fault != "missing-receipt":
        telemetry = app.root / "missing.jsonl"
        if fault == "outside-root":
            telemetry = tmp_path / "outside.jsonl"
            telemetry.write_text("")
        atomic_json(path / "telemetry.json", {"path": str(telemetry)})
        atomic_json(path / "telemetry-report.json", {"rejected_exports": int(fault == "rejected")})
    result = snapshot(app)
    assert result["uncertainty"]
    assert result["trace_span_count"] == 0


def test_long_lived_view_rejects_storage_identity_change(config_file, tmp_path):
    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceError
    from backlog_harness.projections import snapshot

    path, data = config_file
    app = Application(path)
    data["operational_root"] = str(tmp_path / "different-storage")
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(EvidenceError, match="storage identity changed"):
        snapshot(app)
