"""Only an exact terminal a53 scoped question may replay its historical prompt bytes."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backlog_harness.application import Application
from backlog_harness.contracts import AgentBinding, digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked
from backlog_harness.scope_admission import retained_scope_question_prompt
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


def test_exact_terminal_question_reconstructs_historical_bytes_without_writing(replay):
    app, stage, prompt, old_prompt, result, _evidence = replay
    before = {path: path.read_bytes() for path in app.root.rglob("*") if path.is_file()}
    assert retained_scope_question_prompt(app, "one", stage, prompt, result, None) == old_prompt
    assert all(path.read_bytes() == value for path, value in before.items())
    assert set(before) == {path for path in app.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "fault",
    [
        "suffix",
        "digest",
        "failed",
        "partial",
        "native",
        "not-question",
        "new-stage",
        "active",
        "new-answer",
    ],
)
def test_retained_question_replay_rejects_changed_or_nonterminal_evidence(
    replay, monkeypatch, fault
):
    app, stage, prompt, _old_prompt, result, evidence = replay
    if fault == "suffix":
        atomic_json(app._stage_path("one", "continuation"), {"approval": {"changed": True}})
    elif fault == "digest":
        result["request_digest"] = "changed"
    elif fault == "failed":
        result["outcome"] = "failed"
    elif fault == "partial":
        (evidence / "outcomes.jsonl").write_text('{"classification":"returned"}\n{')
    elif fault == "native":
        result["session"] = {"session_id": "owner", "native_session_id": "other"}
    elif fault == "not-question":
        result["text"] = json.dumps({"item_id": "one", "request_completion": True})
    elif fault == "new-stage":
        stage = "scope-answer-continuation-new"
    elif fault == "active":
        app.process_stopped = lambda _: False
    else:
        monkeypatch.setattr(
            "backlog_harness.scope_admission.admitted_question_continuation",
            lambda *args: {"approved": True},
        )
    with pytest.raises(TransitionBlocked):
        retained_scope_question_prompt(app, "one", stage, prompt, result, None)


def test_retained_question_replays_optional_proof_and_ordered_review_tail(replay, monkeypatch):
    app, stage, prompt, old_prompt, result, evidence = replay
    proof = app._stage_path("one", "proof-review")
    atomic_json(proof, {"candidate": "retained", "verdict": "ACCEPT"})
    suffix = (
        "\nReuse this retained fresh proof review; include its reviewer_task (or reviewer_session if absent) as "
        "proof_reviewer_session in your response. Do not repeat proof or review: "
        + proof.read_text()
    )
    monkeypatch.setattr(
        "backlog_harness.native_evidence.source_review_instructions",
        lambda *args: "\nexact review instructions",
    )
    monkeypatch.setattr(
        "backlog_harness.native_evidence.coordination_instructions",
        lambda *args: "\nexact retained coordination",
    )
    tail = "\nexact review instructions\nexact retained coordination"
    expected = old_prompt + suffix + tail
    result["request_digest"] = digest(expected)
    intent = json.loads((evidence / "intent.json").read_text())
    intent["request_digest"] = result["request_digest"]
    atomic_json(evidence / "intent.json", intent)
    assert retained_scope_question_prompt(app, "one", stage, prompt + tail, result, {}) == expected
    proof.write_text('{"candidate":"changed"}')
    with pytest.raises(TransitionBlocked, match="Stage request changed"):
        retained_scope_question_prompt(app, "one", stage, prompt + tail, result, {})


@pytest.mark.parametrize(
    "file,field,value",
    [
        ("result", "binding", None),
        ("result", "binding", {}),
        ("result", "purpose", "provider"),
        ("result", "version", 2),
        ("intent.json", "binding", {}),
        ("intent.json", "action", "different"),
        ("intent.json", "item_id", "other"),
        ("intent.json", "config_digest", "b" * 64),
        ("config.json", "digest", "b" * 64),
        ("session.json", "native_session_id", "other"),
        ("session.json", "binding", {}),
        ("execution-context.json", "read_only", True),
        ("resume-binding.json", "current_config_digest", "b" * 64),
        ("resume-binding.json", "previous_binding", {}),
        ("resume-binding.json", "current_binding", {}),
        ("resume-binding.json", "session_id", "other"),
    ],
)
def test_replay_correlates_all_retained_invocation_identity(replay, file, field, value):
    app, stage, prompt, _old_prompt, result, evidence = replay
    record = result if file == "result" else json.loads((evidence / file).read_text())
    if value is None:
        record.pop(field)
    else:
        record[field] = value
    if file != "result":
        atomic_json(evidence / file, record)
    with pytest.raises(TransitionBlocked):
        retained_scope_question_prompt(app, "one", stage, prompt, result, None)


def test_replay_accepts_recorded_launch_tuning_without_current_config_equality(replay):
    app, stage, prompt, old_prompt, result, evidence = replay
    actual = {
        **result["binding"],
        "profile_digest": "new-model-and-effort",
        "relevant_digest": "new-launch",
    }
    result["binding"] = actual
    for file in ("intent.json", "config.json", "session.json"):
        record = json.loads((evidence / file).read_text())
        record["binding"] = actual
        if file == "intent.json":
            record["config_digest"] = "b" * 64
        if file == "config.json":
            record["digest"] = "b" * 64
        atomic_json(evidence / file, record)
    audit = json.loads((evidence / "resume-binding.json").read_text())
    audit.update(current_binding=actual, current_config_digest="b" * 64)
    atomic_json(evidence / "resume-binding.json", audit)
    assert retained_scope_question_prompt(app, "one", stage, prompt, result, None) == old_prompt


@pytest.mark.parametrize(
    "filename", ["config.json", "session.json", "resume-binding.json", "requested.json"]
)
def test_replay_rejects_missing_or_redirected_identity_files(replay, filename):
    app, stage, prompt, _old_prompt, result, evidence = replay
    path = evidence / filename
    external = app.root / ("substitute-" + filename)
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(TransitionBlocked, match="evidence is incomplete"):
        retained_scope_question_prompt(app, "one", stage, prompt, result, None)


@pytest.mark.parametrize(
    "fault",
    ["text", "events", "events-file", "partial-events", "report", "path", "metadata", "extra"],
)
def test_replay_rejects_result_output_not_bound_to_terminal_records(replay, fault):
    app, stage, prompt, _old_prompt, result, evidence = replay
    if fault == "text":
        result["text"] = json.dumps(
            {"item_id": "one", "question": {"question_id": "forged", "text": "Forged question"}}
        )
    elif fault == "events":
        result["events"] = []
    elif fault == "events-file":
        (evidence / "events.jsonl").write_text("")
    elif fault == "partial-events":
        with (evidence / "events.jsonl").open("a") as stream:
            stream.write('{"partial":')
    elif fault == "report":
        report = json.loads((evidence / "telemetry-report.json").read_text())
        report["span_count"] = 2
        atomic_json(evidence / "telemetry-report.json", report)
    elif fault == "path":
        result["telemetry_path"] = str(app.root / "different.jsonl")
    elif fault == "metadata":
        atomic_json(evidence / "telemetry.json", {"path": "/outside/telemetry.jsonl"})
    else:
        result["extra"] = "not reconstructed"
    with pytest.raises(TransitionBlocked):
        retained_scope_question_prompt(app, "one", stage, prompt, result, None)


def test_missing_report_blocks_before_reconstruction_and_does_not_write(replay):
    app, stage, prompt, _old_prompt, result, evidence = replay
    (evidence / "telemetry-report.json").unlink()

    def forbidden(_path):
        raise AssertionError("Missing report must not enter reconstruction")

    app.recover_invocation = forbidden
    before = {path: path.read_bytes() for path in app.root.rglob("*") if path.is_file()}
    with pytest.raises(TransitionBlocked, match="evidence is incomplete"):
        retained_scope_question_prompt(app, "one", stage, prompt, result, None)
    assert before == {path: path.read_bytes() for path in app.root.rglob("*") if path.is_file()}
