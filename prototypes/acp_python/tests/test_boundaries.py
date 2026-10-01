"""Fault-injected offline boundary tests; these do not claim live model behavior."""

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixture_runner import ScriptedClient
from invocations import Held, Invocations, read, reconcile_provider_receipt

from backlog_harness.evidence import atomic_json


@pytest.fixture
def case(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = tmp_path / "fixture-adapter"
    adapter.write_text("fixture only")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"workspace": str(workspace), "adapter": str(adapter), "model": "one"})
    )
    return config


def drive(config, command, answer=None, *, success=True):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("fixture_runner.py")),
            str(config),
            command,
            *([json.dumps(answer)] if answer is not None else []),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if success:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert result.returncode != 0
    return result.stderr


def calls(config, method):
    path = config.parent / "calls.jsonl"
    return (
        [
            row
            for line in path.read_text().splitlines()
            if (row := json.loads(line))["method"] == method
        ]
        if path.exists()
        else []
    )


def test_real_process_restart_uses_sqlite_question_and_same_native_session(case):
    first = drive(case, "start")
    assert first["__interrupt__"][0]["question_id"] == first["question_id"]
    assert (case.parent / "evidence/checkpoints.sqlite").is_file()
    marker = case.parent / "workspace/completed-work.txt"
    before = (marker.read_bytes(), marker.stat().st_mtime_ns)
    # New Python process and changed model: reload config, preserve session identity/permissions.
    config = read(case)
    config["model"] = "two"
    atomic_json(case, config)
    last = drive(case, "answer", {"question_id": first["question_id"], "text": "French"})
    assert last["outcome"] == "artifact_reviewed"
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before
    prompts = calls(case, "session/prompt")
    assert len(prompts) == 3 and [p["session_id"] for p in prompts[:2]] == ["native-producer"] * 2
    assert prompts[1]["prompt"] == "French" and prompts[1]["model"] == "two"
    assert (
        '"answer": "French"' in prompts[2]["prompt"]
        and '"question": "Which language?"' in prompts[2]["prompt"]
    )
    assert len(calls(case, "session/new")) == 2  # producer once + independently created reviewer
    assert calls(case, "session/load")[0]["session_id"] == "native-producer"
    assert prompts[0]["config_digest"] != prompts[1]["config_digest"]


def test_finished_result_before_checkpoint_is_reused_without_new_child(case):
    (case.parent / "crash-after-result").touch()
    assert "before graph checkpoint" in drive(case, "start", success=False)
    before = len(calls(case, "child-start"))
    recovered = drive(case, "resume")
    assert recovered["decision"]["state"] == "input_required"
    assert len(calls(case, "child-start")) == before and len(calls(case, "session/prompt")) == 1


def test_uncertain_submission_holds_stable_identity_without_retry(case):
    (case.parent / "uncertain").touch()
    drive(case, "start", success=False)
    before = calls(case, "child-start")
    assert "Prompt may have reached Codex" in drive(case, "resume", success=False)
    assert calls(case, "child-start") == before and len(calls(case, "session/prompt")) == 1
    hold = read(next((case.parent / "evidence").rglob("hold.json")))
    assert hold["operation_id"] == "assess:0" and hold["invocation_id"] and hold["request_digest"]


def test_wrong_question_answer_rejected_before_any_child(case):
    drive(case, "start")
    before = calls(case, "child-start")
    assert "durable pending question" in drive(
        case, "answer", {"question_id": "wrong", "text": "French"}, success=False
    )
    assert calls(case, "child-start") == before


def test_permission_change_cannot_replace_session(case):
    first = drive(case, "start")
    other = case.parent / "other-workspace"
    other.mkdir()
    config = read(case)
    config["workspace"] = str(other)
    atomic_json(case, config)
    before = calls(case, "child-start")
    assert "Session permissions changed" in drive(
        case, "answer", {"question_id": first["question_id"], "text": "French"}, success=False
    )
    assert calls(case, "child-start") == before


@pytest.mark.parametrize(
    "fault", ["wrong-candidate", "reject", "missing-evidence", "producer", "load", "assignment"]
)
def test_fresh_review_provenance_and_exact_verdict_required(case, fault):
    (case.parent / "workspace/artifact.txt").write_text("Bonjour\n")
    import hashlib

    candidate = hashlib.sha256(b"Bonjour\n").hexdigest()
    journal = Invocations(case.parent / "evidence", "review-case", case, ScriptedClient)
    reference = asyncio.run(
        journal.invoke(
            "review",
            "reviewer",
            "Review exact artifact",
            producer_id="native-producer",
            candidate=candidate,
        )
    )
    assert journal.verify_review(reference, candidate, "native-producer")["verdict"] == "ACCEPT"
    path = Path(reference["path"])
    if fault in {"wrong-candidate", "reject", "missing-evidence"}:
        value = journal.result(reference)
        verdict = json.loads(value["response"]["text"])
        verdict[
            {"wrong-candidate": "candidate", "reject": "verdict", "missing-evidence": "evidence"}[
                fault
            ]
        ] = {"wrong-candidate": "wrong", "reject": "REJECT", "missing-evidence": ""}[fault]
        value["response"]["text"] = json.dumps(verdict)
        atomic_json(path / "result.json", value)
        from backlog_harness.contracts import digest

        reference["digest"] = digest(value)
    elif fault == "producer":
        with pytest.raises(ValueError):
            journal.verify_review(reference, candidate, "native-reviewer")
        return
    elif fault == "load":
        creation = read(path / "session-requested.json")
        creation["method"] = "session/load"
        atomic_json(path / "session-requested.json", creation)
    else:
        assignment = read(path / "request.json")
        assignment["candidate"] = "wrong"
        atomic_json(path / "request.json", assignment)
    with pytest.raises(ValueError):
        journal.verify_review(reference, candidate, "native-producer")


def test_uncertain_provider_effect_reuses_existing_commit_verifier_without_mutation(case):
    from hashlib import sha256
    from types import MethodType, SimpleNamespace

    from backlog_harness.application import Application
    from backlog_harness.provider import Item, TransitionBlocked, git

    repo = case.parent / "provider"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    path = "item.md"
    (repo / path).write_text("Status: Ready\n")
    git(repo, "add", path)
    git(repo, "commit", "-m", "before")
    head = git(repo, "rev-parse", "HEAD")
    content = (repo / path).read_text()
    before = Item(
        "item",
        path,
        sha256(path.encode() + b"\0" + content.encode()).hexdigest(),
        "Ready",
        "Unowned",
        100,
        content,
    )
    requested = {
        "stage_operation": "same-provider-operation",
        "item": asdict(before),
        "target": "Starting",
        "head": head,
        "paths": [path],
        "authority": {},
    }
    request_path = case.parent / "requested.json"
    observed_path = case.parent / "observed.json"
    atomic_json(request_path, requested)
    (repo / path).write_text("Status: Starting\n")
    git(repo, "add", path)
    git(repo, "commit", "-m", "effect committed before response lost")
    committed = git(repo, "rev-parse", "HEAD")
    app = SimpleNamespace(config=SimpleNamespace(repository=repo))
    app.verify_provider_receipt = MethodType(Application.verify_provider_receipt, app)
    with pytest.raises(Held, match="same-provider-operation"):
        reconcile_provider_receipt(app, request_path, observed_path)
    observed = {
        "operation_id": "same-provider-operation",
        "before_revision": before.revision,
        "commit": committed,
        "after": {
            "item_id": "item",
            "path": path,
            "state": "Starting",
            "owner": "Unowned",
            "original_high": 100,
        },
    }
    atomic_json(observed_path, observed)
    receipt = reconcile_provider_receipt(app, request_path, observed_path)
    assert receipt["commit"] == committed and receipt["after"]["state"] == "Starting"
    assert reconcile_provider_receipt(app, request_path, observed_path) == receipt
    assert git(repo, "rev-parse", "HEAD") == committed
    observed["commit"] = head
    atomic_json(observed_path, observed)
    with pytest.raises(TransitionBlocked):
        reconcile_provider_receipt(app, request_path, observed_path)
    assert git(repo, "rev-parse", "HEAD") == committed


def test_transport_completed_observation_reconciles_without_another_prompt(case):
    (case.parent / "crash-after-result").touch()
    drive(case, "start", success=False)
    root = case.parent / "evidence"
    result = next(root.rglob("result.json"))
    # Simulate loss between the retained transport response and result promotion.
    result.unlink()
    before = calls(case, "child-start")
    recovered = drive(case, "resume")
    assert recovered["decision"]["state"] == "input_required"
    assert calls(case, "child-start") == before and len(calls(case, "session/prompt")) == 1
    assert result.exists()


def test_wrong_transport_observation_cannot_clear_uncertain_hold(case):
    (case.parent / "crash-after-result").touch()
    drive(case, "start", success=False)
    root = case.parent / "evidence"
    result = next(root.rglob("result.json"))
    result.unlink()
    observation = result.with_name("observed-response.json")
    value = read(observation)
    value["session_id"] = "unrelated"
    atomic_json(observation, value)
    before = calls(case, "session/prompt")
    assert "does not prove" in drive(case, "resume", success=False)
    assert calls(case, "session/prompt") == before


def test_never_submitted_session_cannot_reload_under_changed_auth_context(case, monkeypatch):
    from backlog_harness.evidence import EvidenceStore

    journal = Invocations(case.parent / "evidence", "auth-retry", case, ScriptedClient)
    original = EvidenceStore.requested

    def crash(path):
        raise RuntimeError("before prompt submission")

    monkeypatch.setattr(EvidenceStore, "requested", crash)
    with pytest.raises(RuntimeError, match="before prompt"):
        asyncio.run(journal.invoke("assess:0", "producer", "Prepare"))
    monkeypatch.setattr(EvidenceStore, "requested", original)
    before = calls(case, "child-start")
    monkeypatch.setenv("CODEX_HOME", str(case.parent / "other-home"))
    with pytest.raises(Held, match="binding changed"):
        asyncio.run(journal.invoke("assess:0", "producer", "Prepare"))
    assert calls(case, "child-start") == before and not calls(case, "session/prompt")


def test_reviewer_new_session_response_cannot_be_producer_identity(case):
    class InvalidNewSession(ScriptedClient):
        async def new_session(self):
            await super().new_session()
            self.session_exchange["response"]["sessionId"] = "native-producer"
            return "native-producer"

    journal = Invocations(case.parent / "evidence", "bad-fresh", case, InvalidNewSession)
    with pytest.raises(Held, match="producer session"):
        asyncio.run(
            journal.invoke(
                "review", "reviewer", "Review", producer_id="native-producer", candidate="exact"
            )
        )
    assert not calls(case, "session/prompt")
