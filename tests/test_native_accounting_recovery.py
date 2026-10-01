"""Lost supervisor receipt is recoverable only from exact durable native accounting."""

import json

import pytest
from test_codex_completion_evidence import retained  # noqa: F401 - shared native-shape fixture

from backlog_harness.analytics import invocation_usage
from backlog_harness.application import Application
from backlog_harness.evidence import EvidenceStore, JsonlWriter, atomic_json
from backlog_harness.provider import TransitionBlocked
from backlog_harness.telemetry import normalize


@pytest.fixture
def recovery(retained, config_file, tmp_path):  # noqa: F811
    path, native, rows, save, _session, _turn = retained
    rows.insert(
        -1,
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"output_tokens": 7}}},
        },
    )
    save()
    intent = EvidenceStore.reconcile(path)
    events = [
        {
            "type": "thread.started",
            "invocation_id": intent["invocation_id"],
            "at": "2026-10-01T00:00:00+00:00",
        },
        {
            "type": "item.completed",
            "item_type": "agent_message",
            "text": rows[-1]["payload"]["last_agent_message"],
            "invocation_id": intent["invocation_id"],
            "at": "2026-10-01T00:00:01+00:00",
        },
        {
            "type": "turn.completed",
            "usage": {"output_tokens": 7},
            "invocation_id": intent["invocation_id"],
            "at": "2026-10-01T00:00:02+00:00",
        },
    ]
    for event in events:
        JsonlWriter(path / "events.jsonl").append(event)
    telemetry = path / "spans.jsonl"
    payload = normalize(
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "a" * 32,
                                    "spanId": "b" * 16,
                                    "name": "usage",
                                    "attributes": [
                                        {
                                            "key": "gen_ai.usage.output_tokens",
                                            "value": {"intValue": "7"},
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        },
        {
            "harness.invocation.id": intent["invocation_id"],
            "harness.run.id": intent["run_id"],
            "harness.agent.role": intent["binding"]["role"],
            "harness.agent.adapter": intent["binding"]["adapter"],
            "harness.work_item.id": intent["item_id"],
            "harness.provider.id": "file:" + str(Application(config_file[0]).config.repository),
        },
    )
    JsonlWriter(telemetry).append(payload)
    atomic_json(path / "telemetry.json", {"path": str(telemetry)})
    app = Application(config_file[0])
    app.root = tmp_path
    return app, path, native, telemetry, rows, save


def test_missing_receiver_history_recovers_exact_accounting(recovery):
    app, path, native, _telemetry, _rows, _save = recovery
    result = app.recover_invocation(path)
    assert result["outcome"] == "returned"
    assert result["telemetry"]["rejected_exports"] is None
    assert result["telemetry"]["receiver_history"] == "unavailable_after_supervisor_crash"
    assert invocation_usage(result) == 7
    assert EvidenceStore.reconcile(path)["outcome"] == "returned"
    assert app.recover_invocation(path) == result
    # A later legitimate turn does not invalidate the retained invocation prefix.
    with native.open("a") as stream:
        stream.write(
            json.dumps(
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}}
            )
            + "\n"
        )
    app.validate_invocation_result(result)
    assert invocation_usage(result) == 7


@pytest.mark.parametrize("fault", ["usage", "partial", "wrong-turn", "live", "unmarked"])
def test_unproven_completion_stays_unresolved(recovery, fault):
    import os

    app, path, _native, telemetry, rows, save = recovery
    if fault == "usage":
        telemetry.write_text(telemetry.read_text().replace('"7"', '"8"'))
    elif fault == "partial":
        telemetry.write_text(telemetry.read_text().rstrip("\n"))
    elif fault == "wrong-turn":
        rows[-1]["payload"]["turn_id"] = "wrong"
        save()
    elif fault == "live":
        atomic_json(path / "process.json", {"pid": os.getpid(), "started": "still-live"})
    else:
        (path / "native-request.json").unlink()
    if fault == "unmarked":
        assert app.recover_invocation(path) is None
    else:
        with pytest.raises((TransitionBlocked, ValueError)):
            app.recover_invocation(path)
    assert EvidenceStore.reconcile(path)["outcome"] == "unresolved"
    assert not (path / "telemetry-report.json").exists()


def test_recovered_receipt_does_not_hide_native_or_span_tampering(recovery):
    app, path, native, _telemetry, _rows, _save = recovery
    result = app.recover_invocation(path)
    native.write_text(native.read_text().replace("last_agent_message", "different_message"))
    with pytest.raises(TransitionBlocked):
        app.validate_invocation_result(result)
    assert invocation_usage(result) is None


@pytest.mark.parametrize(
    "fault",
    [
        "late-response",
        "late-parent",
        "wrong-run",
        "missing-role",
        "malformed-span",
        "conflicting-span",
    ],
)
def test_equal_totals_do_not_hide_incomplete_coverage_or_attribution(recovery, fault):
    app, path, _native, telemetry, rows, save = recovery
    if fault == "late-response":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": []},
            },
        )
        save()
    elif fault == "late-parent":
        session = json.loads((path / "session.json").read_text())["native_session_id"]
        rows.insert(
            -1,
            {
                "type": "token_usage_record",
                "timestamp": "2026-10-01T00:00:00+00:00",
                "payload": {"session_id": session, "thread_token_usage": {"output_tokens": 7}},
            },
        )
        rows.insert(
            -1,
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "AgentMessage"},
                    "started_at_ms": 1790812801000,
                },
            },
        )
        save()
    else:
        payload = json.loads(telemetry.read_text())
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        if fault == "wrong-run":
            next(a for a in span["attributes"] if a["key"] == "harness.run.id")["value"] = {
                "stringValue": "wrong"
            }
        elif fault == "missing-role":
            span["attributes"] = [a for a in span["attributes"] if a["key"] != "harness.agent.role"]
        elif fault == "malformed-span":
            span["traceId"] = "not-a-trace"
        else:
            JsonlWriter(telemetry).append(payload)
            span["name"] = "conflicting"
        if fault != "conflicting-span":
            telemetry.write_text("")
        JsonlWriter(telemetry).append(payload)
    with pytest.raises((TransitionBlocked, ValueError)):
        app.recover_invocation(path)
    assert EvidenceStore.reconcile(path)["outcome"] == "unresolved"


@pytest.mark.parametrize("fault", ["request", "policy"])
def test_admission_recovery_rechecks_original_request_and_current_policy(
    recovery, monkeypatch, fault
):
    from backlog_harness.contracts import digest
    from backlog_harness.evidence import component

    app, _path, _native, _telemetry, _rows, _save = recovery
    authority = {"invocation_id": "authority"}
    record = {
        "item": {"item_id": "item", "revision": "revision", "content": "assignment"},
        "target": "Starting",
        "authority": authority,
        "executing_role": "coordinator",
        "stage_operation": "item:provider",
        "prompt": "original",
        "prompt_digest": digest("original"),
        "repository": str(app.config.repository),
    }
    root = app.root / "provider-agent-operations" / component(digest(record))
    atomic_json(root / "requested.json", record)
    receipt = {
        "commit": "original-commit",
        "advancement_verified": False,
        "policy": {"source": "old"},
    }
    atomic_json(root / "receipt.json", receipt)
    store = EvidenceStore(app.root, "item:item")
    path = store.begin(
        "item:provider",
        "invocation",
        app.config,
        app.config.binding("coordinator"),
        action="provider",
        item_id=None,
        request_digest="wrong"
        if fault == "request"
        else digest(["provider", record["repository"], False, digest(record), "original"]),
    )
    calls = []

    def recover(_path):
        calls.append("recover")
        return {"invocation_id": "invocation", "session": {}, "evidence_path": str(path)}

    monkeypatch.setattr(app, "recover_invocation", recover)
    monkeypatch.setattr(app, "validate_invocation_result", lambda result: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda result, limits: None)
    monkeypatch.setattr(app, "result_json", lambda result: {})
    monkeypatch.setattr(app, "verify_provider_receipt", lambda record, value: dict(receipt))

    def reject_policy(*args):
        raise TransitionBlocked("current policy changed")

    monkeypatch.setattr(app, "validate_receipt_policy", reject_policy)
    with pytest.raises(TransitionBlocked, match="original request|current policy"):
        app.verify_agent_admission("item", "revision", authority, "assignment")
    assert calls == ([] if fault == "request" else ["recover"])
    assert json.loads((root / "receipt.json").read_text()) == receipt
    assert not app._stage_path("item", "provider").exists()
