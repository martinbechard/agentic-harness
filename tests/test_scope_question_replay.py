"""Completed operation evidence survives harmless changes to prompt wording."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backlog_harness.application import Application
from backlog_harness.contracts import AgentBinding, digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked
from backlog_harness.telemetry import Sink


@pytest.fixture
def replay(tmp_path, monkeypatch):
    binding = asdict(
        AgentBinding(
            "orchestrator",
            "primary",
            "codex",
            "/bin/codex",
            "exe-digest",
            "default",
            "worker",
            "profile-digest",
            "relevant-digest",
            "/native/home",
            "permission-digest",
        )
    )
    admission = {
        "session": {"session_id": "owner", "native_session_id": "native", "binding": binding},
        "workflow": {},
        "input": {"amended_content": "retained scope"},
    }
    stage = "scope-continuation-" + digest(admission)
    stage_path = lambda item, name: tmp_path / "stages" / (name + ".json")
    approval = {"question": {"question_id": "older"}, "answer": {"text": "old approval"}}
    atomic_json(stage_path("one", "continuation"), {"stage": "old", "approval": approval})
    prompt = "Original scoped production prompt."
    old_prompt = (
        prompt
        + "\nPersisted canonical approval: "
        + json.dumps(json.loads(stage_path("one", "continuation").read_text())["approval"])
    )
    evidence = (
        tmp_path
        / "runs"
        / component("item:one")
        / "operations"
        / component("one:" + stage)
        / "invocations"
        / component("invocation")
    )
    result = {
        "version": 1,
        "purpose": "implementation",
        "binding": dict(binding),
        "outcome": "returned",
        "role": "orchestrator",
        "session": admission["session"],
        "evidence_path": str(evidence),
        "invocation_id": "invocation",
        "request_digest": digest(old_prompt),
        "text": json.dumps(
            {"item_id": "one", "question": {"question_id": "new", "text": "Need exact answer."}}
        ),
    }
    atomic_json(
        evidence / "intent.json",
        {
            "version": 1,
            "invocation_id": "invocation",
            "operation_id": "one:" + stage,
            "run_id": "item:one",
            "action": stage,
            "item_id": "one",
            "binding": dict(binding),
            "config_digest": "a" * 64,
            "request_digest": result["request_digest"],
        },
    )
    atomic_json(evidence / "config.json", {"version": 1, "digest": "a" * 64, "binding": binding})
    atomic_json(evidence / "session.json", {"version": 1, **admission["session"]})
    atomic_json(
        evidence / "execution-context.json",
        {"purpose": "implementation", "provider_operation": None, "read_only": False},
    )
    atomic_json(
        evidence / "resume-binding.json",
        {
            "session_id": "owner",
            "native_session_id": "native",
            "previous_binding": binding,
            "current_binding": binding,
            "current_config_digest": "a" * 64,
        },
    )
    telemetry_path = tmp_path / "telemetry.jsonl"
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "turn",
                                "attributes": [],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    telemetry_path.write_text(json.dumps(payload) + "\n")
    report = {
        "span_count": 1,
        "rejected_exports": 0,
        "evidence_sha256": Sink(telemetry_path, {}).evidence_digest(),
    }
    result["telemetry"] = report
    result["telemetry_path"] = str(telemetry_path)
    atomic_json(evidence / "telemetry.json", {"path": str(telemetry_path)})
    atomic_json(evidence / "telemetry-report.json", report)
    result["events"] = [
        {
            "version": 1,
            "invocation_id": "invocation",
            "type": "item.completed",
            "item_type": "agent_message",
            "text": result["text"],
        },
        {
            "version": 1,
            "invocation_id": "invocation",
            "type": "turn.completed",
            "usage": {"output_tokens": 12},
        },
    ]
    (evidence / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in result["events"])
    )
    atomic_json(evidence / "requested.json", {})
    (evidence / "outcomes.jsonl").write_text(json.dumps({"classification": "returned"}) + "\n")
    app = SimpleNamespace(
        root=tmp_path,
        scope_admission=lambda _: admission,
        _stage_path=stage_path,
        validate_invocation_result=lambda value: Application.validate_invocation_result(app, value),
        recover_invocation=lambda path: Application.recover_invocation(app, path),
        result_json=lambda value: Application.result_json(app, value),
        process_stopped=lambda _: True,
        candidate_repository=lambda _: tmp_path / "candidate",
    )
    monkeypatch.setattr(
        "backlog_harness.scope_admission.admitted_question_continuation", lambda *args: None
    )
    return app, stage, prompt, old_prompt, result, evidence


def validate(replay, **changes):
    app, stage, _prompt, _old_prompt, result, _evidence = replay
    from backlog_harness.runtime import SessionHandle

    binding = AgentBinding(**result["binding"])
    session = SessionHandle(
        **{**result["session"], "binding": AgentBinding(**result["session"]["binding"])}
    )
    args = {
        "item_id": "one",
        "stage": stage,
        "operation": "one:" + stage,
        "role": "orchestrator",
        "binding": binding,
        "session": session,
        "context": {"purpose": "implementation", "provider_operation": None, "read_only": False},
    }
    args.update(changes)
    Application.validate_completed_stage(app, result, **args)


def test_completed_result_reuses_recorded_output_without_prompt_reconstruction(replay):
    app, _stage, _prompt, _old_prompt, _result, _evidence = replay
    # Historical wording and an unrelated historical approval appendix are not authority.
    app._stage_path("one", "continuation").unlink()
    before = {p: p.read_bytes() for p in app.root.rglob("*") if p.is_file()}
    validate(replay)
    assert before == {p: p.read_bytes() for p in app.root.rglob("*") if p.is_file()}


def test_completed_replay_preserves_returned_work_with_empty_trace_receipt(replay):
    app, _stage, _prompt, _old_prompt, result, evidence = replay
    telemetry = app.root / "telemetry.jsonl"
    telemetry.write_text("")
    report = {
        "span_count": 0,
        "rejected_exports": 0,
        "evidence_sha256": Sink(telemetry, {}).evidence_digest(),
    }
    result["telemetry"] = report
    atomic_json(evidence / "telemetry-report.json", report)
    before = {p: p.read_bytes() for p in app.root.rglob("*") if p.is_file()}
    validate(replay)
    assert before == {p: p.read_bytes() for p in app.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "fault",
    [
        "text",
        "events",
        "events-file",
        "partial-events",
        "report",
        "path",
        "metadata",
        "extra",
        "digest",
        "failed",
        "partial",
        "missing-report",
        "missing-audit",
        "symlink",
        "role",
        "session",
        "permission",
        "context",
        "stage",
        "item",
        "operation",
    ],
)
def test_completed_replay_rejects_changed_identity_authority_or_evidence(replay, fault):
    app, _stage, _prompt, _old_prompt, result, evidence = replay
    changes = {}
    if fault == "text":
        result["text"] = "Forged result"
    elif fault == "events":
        result["events"] = []
    elif fault == "events-file":
        (evidence / "events.jsonl").write_text("")
    elif fault == "partial-events":
        with (evidence / "events.jsonl").open("a") as f:
            f.write('{"partial":')
    elif fault == "report":
        report = json.loads((evidence / "telemetry-report.json").read_text())
        report["span_count"] = 2
        atomic_json(evidence / "telemetry-report.json", report)
    elif fault == "path":
        result["telemetry_path"] = str(app.root / "other.jsonl")
    elif fault == "metadata":
        atomic_json(evidence / "telemetry.json", {"path": "/outside/telemetry"})
    elif fault == "extra":
        result["extra"] = "Not in the recorded output"
    elif fault == "digest":
        result["request_digest"] = "0" * 64
    elif fault == "failed":
        result["outcome"] = "failed"
    elif fault == "partial":
        with (evidence / "outcomes.jsonl").open("a") as f:
            f.write('{"partial":')
    elif fault == "missing-report":
        (evidence / "telemetry-report.json").unlink()
    elif fault == "missing-audit":
        (evidence / "resume-binding.json").unlink()
    elif fault == "symlink":
        path = evidence / "config.json"
        target = app.root / "other-config.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif fault == "role":
        changes["role"] = "coordinator"
    elif fault == "session":
        result["session"] = {**result["session"], "native_session_id": "other"}
    elif fault == "permission":
        changes["binding"] = AgentBinding(**{**result["binding"], "permission_digest": "widened"})
    elif fault == "context":
        changes["context"] = {"purpose": "provider", "provider_operation": None, "read_only": False}
    elif fault == "stage":
        changes["stage"] = "different-stage"
    elif fault == "item":
        changes["item_id"] = "other"
    elif fault == "operation":
        changes["operation"] = "other"
    with pytest.raises(TransitionBlocked):
        validate(replay, **changes)
