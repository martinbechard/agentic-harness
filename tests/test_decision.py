"""Decision CLI against a separate synthetic provider; never the real backlog."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backlog_harness.provider_lock import (
    coordinator,
)

FIXTURE = Path(__file__).parent / "fixtures/decision_provider.py"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    record_path = root / "backlog/item.json"
    record_path.parent.mkdir()
    record = {
        "id": "one",
        "status": "User Action Required",
        "question": "Approve exact candidate abc123?",
        "candidate": "abc123",
        "kind": "approval",
        "options": [],
        "free_text": False,
        "history": ["original delivery"],
        "decisions": [],
    }
    record_path.write_text(json.dumps(record, indent=2))
    (root / "authority.json").write_text(json.dumps({"workspace": str(root)}))
    (root / "candidate.txt").write_text("preserve candidate")
    (root / "unrelated.txt").write_text("preserve unrelated edits")
    config = {
        "project": str(root),
        "state": str(tmp_path / "state"),
        **{r: [sys.executable, str(FIXTURE.resolve())] for r in ("access", "development", "merge")},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    request = {
        "project": str(root),
        "item_id": "one",
        "workspace": str(root),
        "observed": {
            "locator": "backlog/item.json",
            "revision": revision(record_path),
            "question": record["question"],
            "candidate": record["candidate"],
        },
        "decision": {"kind": "allow", "answer": None},
    }
    (root / "observations.json").write_text(json.dumps({request["observed"]["revision"]: record}))
    return config, config_path, request, record_path


def revision(path):
    return hashlib.sha256(b"backlog/item.json\0" + path.read_bytes()).hexdigest()


def invoke(config_path, request, suffix=""):
    path = config_path.parent / f"decision{suffix}.json"
    path.write_text(json.dumps(request))
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backlog_harness",
            "--config",
            str(config_path),
            "--decision",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process):
    stdout, stderr = process.communicate(timeout=15)
    results = [
        json.loads(line)
        for line in stdout.splitlines()
        if json.loads(line)["event"] == "decision_result"
    ]
    assert len(results) == 1, (stdout, stderr)
    return process.returncode, results[0]


@pytest.mark.parametrize(
    "kind,answer",
    [("allow", None), ("cancel", None), ("answer", "choice B"), ("answer", "custom answer")],
)
def test_cli_persists_exact_decision_once_and_preserves_work(project, kind, answer):
    config, config_path, request, record_path = project
    if kind == "answer":
        record = json.loads(record_path.read_text())
        record.update(
            kind="choice", options=["choice A", "choice B"], free_text=answer == "custom answer"
        )
        record_path.write_text(json.dumps(record))
        request["observed"]["revision"] = revision(record_path)
    request["decision"] = {"kind": kind, "answer": answer}
    with coordinator(config):
        code, first = finish(invoke(config_path, request))
        assert code == 0 and first["status"] == "applied" and first["persisted"] is True
        before = record_path.read_bytes()
        code, replay = finish(invoke(config_path, request))
        assert code == 0 and replay["status"] == "already_applied"
        assert replay["decision_id"] == first["decision_id"]
        assert record_path.read_bytes() == before
    record = json.loads(record_path.read_text())
    assert len(record["decisions"]) == 1 and record["history"] == ["original delivery"]
    assert record["candidate"] == "abc123"
    assert first["revision"] == revision(record_path)
    assert first["state"] == record["status"] == ("Cancelled" if kind == "cancel" else "Ready")
    assert record["decisions"][0]["submission"]["decision"] == request["decision"]
    for name in ("candidate.txt", "unrelated.txt"):
        assert (Path(config["project"]) / name).read_text().startswith("preserve")


@pytest.mark.parametrize(
    "mismatch",
    ["revision", "question", "candidate", "item_id", "choice", "invalid_answer", "published"],
)
def test_stale_or_ambiguous_decisions_refused_without_writes(project, mismatch):
    config, config_path, request, path = project
    if mismatch in ("revision", "question", "candidate"):
        request["observed"][mismatch] = "different"
    elif mismatch == "item_id":
        request["item_id"] = "different"
    elif mismatch == "published":
        (Path(config["project"]) / "authority.json").write_text(
            json.dumps({"workspace": "/another/current/source"})
        )
    else:
        record = json.loads(path.read_text())
        record.update(kind="choice", options=["A", "B"])
        path.write_text(json.dumps(record))
        request["observed"]["revision"] = revision(path)
        if mismatch == "invalid_answer":
            request["decision"] = {"kind": "answer", "answer": "C"}
    before = path.read_bytes()
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    assert code == 3 and result["status"] == "rejected" and result["persisted"] is False
    assert path.read_bytes() == before


def test_current_worktree_handoff_and_duplicate_concurrent_clicks(project):
    config, config_path, request, path = project
    workspace = Path(config["project"]) / ".worktrees/item"
    target = workspace / request["observed"]["locator"]
    target.parent.mkdir(parents=True)
    target.write_bytes(path.read_bytes())
    request["workspace"] = str(workspace)
    (Path(config["project"]) / "authority.json").write_text(
        json.dumps({"workspace": str(workspace)})
    )
    (Path(config["project"]) / "behavior").write_text("slow")
    before = path.read_bytes()
    with coordinator(config):
        one = invoke(config_path, request, "one")
        two = invoke(config_path, request, "two")
        first, second = finish(one), finish(two)
    assert first[0] == second[0] == 0
    assert {first[1]["status"], second[1]["status"]} == {"applied", "already_applied"}
    assert len(json.loads(target.read_text())["decisions"]) == 1
    assert path.read_bytes() == before


def test_lost_acknowledgement_is_unknown_and_retry_does_not_duplicate(project):
    config, config_path, request, path = project
    behavior = Path(config["project"]) / "behavior"
    behavior.write_text("lost_result")
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
        assert code == 1 and result["status"] == "unknown" and result["persisted"] is None
        assert len(json.loads(path.read_text())["decisions"]) == 1
        behavior.unlink()
        code, result = finish(invoke(config_path, request))
        assert code == 0 and result["status"] == "already_applied"
        assert len(json.loads(path.read_text())["decisions"]) == 1


def test_no_upgrade_lease_refuses_before_agent_launch(project):
    config, config_path, request, path = project
    before = path.read_bytes()
    code, result = finish(invoke(config_path, request))
    assert code == 3 and result["persisted"] is False
    assert not list(Path(config["state"]).glob("*/request.json"))
    assert path.read_bytes() == before


def test_result_identity_mismatch_is_never_reported_as_persisted(project):
    config, config_path, request, _ = project
    (Path(config["project"]) / "behavior").write_text("misbound")
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    assert code == 1 and result["persisted"] is None and result["status"] == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"extra": True},
        {"project": 3},
        {"project": "relative"},
        {"project": "/different"},
        {"workspace": "/nonexistent"},
        {"item_id": " "},
        {"observed": []},
        {"observed": {"locator": "", "revision": "r", "question": "q", "candidate": None}},
        {"observed": {"locator": "p", "revision": "r", "question": "q", "candidate": False}},
        {"decision": []},
        {"decision": {"kind": "other", "answer": None}},
        {"decision": {"kind": "answer", "answer": " "}},
        {"decision": {"kind": "allow", "answer": "option A"}},
    ],
)
def test_invalid_submission_fails_before_provider(project, change):
    from backlog_harness.decision import prepare

    config, config_path, request, _ = project
    request = None if change is None else {**request, **change}
    path = config_path.parent / "invalid.json"
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError):
        prepare(path, config)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"state": None},
        {"decision_id": "wrong"},
        {"item_id": "wrong"},
        {"status": "queued"},
        {"persisted": False},
        {"resolution": "cancelled"},
        {"revision": ""},
    ],
)
def test_provider_acknowledgement_must_match_persisted_decision(project, change):
    from backlog_harness.decision import validate_result

    _, _, request, _ = project
    request["decision_id"] = "stable-id"
    result = {
        "status": "applied",
        "decision_id": "stable-id",
        "item_id": "one",
        "persisted": True,
        "resolution": "approved",
        "state": "Ready",
        "revision": "new",
        "workspace": request["workspace"],
        "locator": request["observed"]["locator"],
        "detail": "read back",
    }
    with pytest.raises(ValueError):
        validate_result({**result, **change}, request)


def test_decision_waits_for_ordinary_provider_operation(project):
    import asyncio

    from backlog_harness.process import Agents

    config, config_path, request, path = project
    root = Path(config["project"])

    async def scenario():
        agents = Agents(config, lambda *a, **kw: None)
        ordinary = asyncio.create_task(agents.ask("status", item={"id": "one"}))

        async def started():
            while not (root / "ordinary.started").exists():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(started(), 5)
        decision = invoke(config_path, request)
        try:
            await asyncio.sleep(0.15)
            assert decision.poll() is None
            assert json.loads(path.read_text())["decisions"] == []
        finally:
            (root / "ordinary.release").touch()
        assert await ordinary == {"status": "running"}
        code, result = await asyncio.to_thread(finish, decision)
        assert code == 0 and result["status"] == "applied"

    with coordinator(config):
        asyncio.run(scenario())


def test_waiting_provider_lock_can_be_cancelled_and_reused(tmp_path):
    import asyncio

    from backlog_harness.provider_lock import provider_lock

    async def scenario():
        async def acquire():
            async with provider_lock(tmp_path):
                return True

        async with provider_lock(tmp_path):
            waiting = asyncio.create_task(acquire())
            await asyncio.sleep(0.02)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        assert await asyncio.wait_for(acquire(), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "owner", ["bad json", "[]", '{"version":0}', '{"version":1,"project":"/wrong"}']
)
def test_incompatible_coordination_owner_is_rejected(project, owner):
    import fcntl

    from backlog_harness.provider_lock import CoordinationUnavailable, require_coordinator

    config, _, _, _ = project
    path = Path(config["state"]) / "provider-coordination.lock"
    path.parent.mkdir()
    with path.open("w+") as stream:
        stream.write(owner)
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_EX)
        with pytest.raises(CoordinationUnavailable):
            require_coordinator(config)


def test_stopped_lease_and_second_runner_are_rejected(project):
    from backlog_harness.provider_lock import CoordinationUnavailable, require_coordinator

    config, config_path, _, _ = project
    with coordinator(config):
        require_coordinator(config)
        with pytest.raises(CoordinationUnavailable), coordinator(config):
            pass
        result = subprocess.run(
            [sys.executable, "-m", "backlog_harness", "--config", str(config_path)],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 2 and "Another coordinated runner" in result.stderr
    with pytest.raises(CoordinationUnavailable):
        require_coordinator(config)


def test_unrelated_scan_note_preserves_human_decision_and_latest_history(project):
    from backlog_harness.decision import prepare

    config, config_path, request, path = project
    request_file = config_path.parent / "saved.json"
    request_file.write_text(json.dumps(request))
    original_id = prepare(request_file, config)["decision_id"]
    record = json.loads(path.read_text())
    record["history"].append("Merge scan: no eligible candidate; no action taken")
    path.write_text(json.dumps(record))
    assert revision(path) != request["observed"]["revision"]
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
        assert code == 0 and result["status"] == "applied"
        assert result["decision_id"] == original_id
        assert finish(invoke(config_path, request))[1]["status"] == "already_applied"
    persisted = json.loads(path.read_text())
    assert persisted["history"] == record["history"]
    assert persisted["decisions"][0]["submission"]["observed"] == request["observed"]
    assert len(persisted["decisions"]) == 1
    assert persisted["candidate"] == request["observed"]["candidate"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("question", "Approve broader work?"),
        ("candidate", "different-candidate"),
        ("conditions", ["Also authorize deployment"]),
        ("options", ["new choice"]),
        ("status", "Ready"),
        ("kind", "choice"),
    ],
)
def test_decision_relevant_changes_still_refuse_original_approval(project, field, value):
    config, config_path, request, path = project
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    assert code == 3 and result["persisted"] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("safe", [True, False])
def test_claim_free_policy_uses_native_transaction_or_reports_actual_blocker(project, safe):
    config, config_path, request, path = project
    root = Path(config["project"])
    (root / "authority.json").write_text(
        json.dumps(
            {
                "workspace": str(root),
                "claim_mode": "disabled",
                "scheduling": "SOLO",
                "native_transactions": safe,
            }
        )
    )
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    assert code == (0 if safe else 3)
    assert result["persisted"] is safe
    assert len(json.loads(path.read_text())["decisions"]) == int(safe)


def test_latest_revision_is_revalidated_before_decision_write(project):
    config, config_path, request, path = project
    (Path(config["project"]) / "behavior").write_text("concurrent_change")
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    assert code == 3 and "conditional update" in result["detail"]
    record = json.loads(path.read_text())
    assert record["question"] == "Approve a different scope?"
    assert record["decisions"] == []


def test_decision_queue_timeout_emits_waiting_without_launch_or_mutation(project):
    import fcntl

    config, config_path, request, path = project
    config["decision_timeout"] = 0.1
    config_path.write_text(json.dumps(config))
    with coordinator(config), (Path(config["state"]) / "provider.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = invoke(config_path, request)
        # Observe progress while the process is still blocked on the lock.
        first = json.loads(process.stdout.readline())
        assert first["event"] == "provider_waiting"
        assert first["item"] == request["item_id"] and first["timeout_seconds"] == 0.1
        code, result = finish(process)
    assert code == 3 and result["persisted"] is False
    assert result["decision_id"] == first["decision_id"]
    assert "waiting" in result["detail"]
    assert not list(Path(config["state"]).glob("*/request.json"))
    assert json.loads(path.read_text())["decisions"] == []


def test_decision_running_deadline_reports_unknown_preserves_id_and_reaps_agent(project):
    import os

    config, config_path, request, path = project
    config["decision_timeout"] = 0.5
    config_path.write_text(json.dumps(config))
    behavior = Path(config["project"]) / "behavior"
    behavior.write_text("sleep_after_write")
    with coordinator(config):
        process = invoke(config_path, request)
        output, error = process.communicate(timeout=10)
        assert process.returncode == 1, error
        events = [json.loads(line) for line in output.splitlines()]
        started = next(e for e in events if e["event"] == "provider_started")
        timeout = next(e for e in events if e["event"] == "provider_timeout")
        result = next(e for e in events if e["event"] == "decision_result")
        assert timeout["phase"] == "running"
        assert started["decision_id"] == result["decision_id"]
        assert result["status"] == "unknown" and result["persisted"] is None
        with pytest.raises(ProcessLookupError):
            os.kill(started["pid"], 0)
        assert len(json.loads(path.read_text())["decisions"]) == 1
        behavior.unlink()
        code, retry = finish(invoke(config_path, request))
        assert code == 0 and retry["status"] == "already_applied"
        assert retry["decision_id"] == result["decision_id"]


def test_allow_grants_named_permission_in_grant_or_withhold_question(project):
    config, config_path, request, path = project
    record = json.loads(path.read_text())
    record.update(
        question="Authorize this verification, or keep that verification paused?",
        kind="approval",
        options=["Authorize this verification", "Keep it paused"],
        candidate=None,
    )
    path.write_text(json.dumps(record))
    request["observed"].update(revision=revision(path), question=record["question"], candidate=None)
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    assert code == 0 and result["resolution"] == "approved"
    assert json.loads(path.read_text())["decisions"][0]["submission"]["decision"] == {
        "kind": "allow",
        "answer": None,
    }


@pytest.mark.parametrize("kind", ["allow", "cancel"])
def test_historical_approval_is_reported_without_reapplying_or_reconciling_silently(project, kind):
    config, config_path, request, path = project
    record = json.loads(path.read_text())
    request["decision"]["kind"] = kind
    record["historical_resolution"] = {
        "question": record["question"],
        "candidate": record["candidate"],
        "resolution": "approved",
        "date": "2026-10-01",
    }
    path.write_text(json.dumps(record))
    request["observed"]["revision"] = revision(path)
    before = path.read_bytes()
    with coordinator(config):
        code, result = finish(invoke(config_path, request))
    if kind == "allow":
        assert code == 3 and result["status"] == "already_resolved"
        assert result["persisted"] is True and result["resolution"] == "approved"
        assert result["state"] == "User Action Required" and "reconciliation" in result["detail"]
        assert path.read_bytes() == before and record["decisions"] == []
    else:
        assert code == 0 and result["status"] == "applied"
        assert result["resolution"] == "cancelled" and result["state"] == "Cancelled"
        persisted = json.loads(path.read_text())
        assert persisted["historical_resolution"] == record["historical_resolution"]
        assert len(persisted["decisions"]) == 1


@pytest.mark.parametrize("resolution,detail", [("none", "evidence"), ("approved", "")])
def test_historical_result_requires_actual_resolution_and_evidence(project, resolution, detail):
    from backlog_harness.decision import validate_result

    _, _, request, _ = project
    request["decision_id"] = "stable-id"
    result = {
        "status": "already_resolved",
        "decision_id": "stable-id",
        "item_id": "one",
        "persisted": True,
        "resolution": resolution,
        "state": "User Action Required",
        "revision": "new",
        "workspace": request["workspace"],
        "locator": request["observed"]["locator"],
        "detail": detail,
    }
    with pytest.raises(ValueError, match="Historical resolution"):
        validate_result(result, request)


def test_prior_approval_cannot_be_acknowledged_as_item_cancellation(project):
    from backlog_harness.decision import validate_result

    _, _, request, _ = project
    request["decision_id"] = "stable-id"
    request["decision"]["kind"] = "cancel"
    result = {
        "status": "already_resolved",
        "decision_id": "stable-id",
        "item_id": "one",
        "persisted": True,
        "resolution": "approved",
        "state": "User Action Required",
        "revision": "new",
        "workspace": request["workspace"],
        "locator": request["observed"]["locator"],
        "detail": "Existing approval",
    }
    with pytest.raises(ValueError, match="does not cancel"):
        validate_result(result, request)


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("commit_failure", [False, True])
def test_published_answer_reaches_preserved_workspace_before_delivery(
    project, conflict, commit_failure
):
    import asyncio

    from backlog_harness.decision import prepare, submit
    from backlog_harness.process import Agents

    config, _, request, path = project
    root = Path(config["project"])

    def git(where, *args):
        return subprocess.check_output(["git", "-C", str(where), *args], text=True).strip()

    record = json.loads(path.read_text())
    record.update(kind="choice", free_text=True, candidate=None)
    path.write_text(json.dumps(record))
    request["observed"].update(candidate=None, revision=revision(path))
    request["decision"] = {"kind": "answer", "answer": "Adopt the harness as is"}
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Decision fixture")
    git(root, "add", "backlog/item.json")
    git(root, "commit", "-m", "Pending question")
    workspace = root.parent / "preserved"
    git(root, "worktree", "add", "-b", "item/one", str(workspace))
    (workspace / "candidate.txt").write_text("preserved candidate")
    git(workspace, "add", "candidate.txt")
    git(workspace, "commit", "-m", "Candidate before human answer")
    candidate = git(workspace, "rev-parse", "HEAD")
    local = json.loads((workspace / "backlog/item.json").read_text())
    local["history"].append("local delivery evidence")
    if conflict:
        local["question"] = "Different pending question"
    (workspace / "backlog/item.json").write_text(json.dumps(local))
    (workspace / "unrelated.txt").write_text("local unrelated edit")
    git(root, "add", "unrelated.txt")  # Must not sneak into the decision commit.
    (root / "authority.json").write_text(
        json.dumps({"workspace": str(root), "delivery_workspace": str(workspace)})
    )
    request_path = root.parent / "original.json"
    request_path.write_text(json.dumps(request))
    original_bytes = request_path.read_bytes()
    prepared = prepare(request_path, config)

    async def scenario():
        agents = Agents(config, lambda *a, **kw: None)
        hook = root / ".git/hooks/pre-commit"
        if commit_failure:
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
        with coordinator(config):
            result = await submit(agents, prepared)
            if commit_failure:
                assert result["status"] == "unknown" and result["persisted"] is None
                assert json.loads(path.read_text())["decisions"][0]["submission"] == prepared
                assert json.loads(git(root, "show", "HEAD:backlog/item.json"))["decisions"] == []
                assert await agents.ready(1, None, False, []) == []
                hook.unlink()
                result = await submit(agents, prepared)
        assert result["status"] == ("already_applied" if commit_failure else "applied"), result
        published = json.loads(git(root, "show", "HEAD:backlog/item.json"))
        assert published["decisions"][0]["submission"] == prepared
        assert git(root, "diff", "--cached", "--name-only") == "unrelated.txt"
        # Simulate a provider restart: no in-memory workspace registry is available.
        agents = Agents(config, lambda *a, **kw: None)
        items = await agents.ready(1, None, False, [])
        if conflict:
            assert items == []
            assert not (workspace / "received-answer.json").exists()
            return
        assert items[0].worktree == str(workspace) and items[0].branch == "item/one"
        agent = await agents.deliver(items[0], False)
        assert (await agent.wait()).status == "success"
        received = json.loads((workspace / "received-answer.json").read_text())
        assert received["submission"]["decision_id"] == prepared["decision_id"]
        assert received["submission"]["decision"] == request["decision"]
        final = json.loads((workspace / "backlog/item.json").read_text())
        assert "local delivery evidence" in final["history"]
        git(workspace, "merge-base", "--is-ancestor", candidate, "HEAD")
        assert (workspace / "unrelated.txt").read_text() == "local unrelated edit"

    asyncio.run(scenario())
    assert request_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "persisted,resolution,detail,valid",
    [
        (None, "none", "Written but commit failed", True),
        (False, "none", "Written but commit failed", False),
        (None, "answered", "Written but commit failed", False),
        (None, "none", "", False),
    ],
)
def test_provider_can_report_partial_publication(project, persisted, resolution, detail, valid):
    from backlog_harness.decision import validate_result

    _, _, request, _ = project
    request["decision_id"] = "original-id"
    result = {
        "status": "unknown",
        "decision_id": "original-id",
        "item_id": "one",
        "persisted": persisted,
        "resolution": resolution,
        "state": "Ready",
        "revision": "partial",
        "workspace": request["workspace"],
        "locator": request["observed"]["locator"],
        "detail": detail,
    }
    if valid:
        assert validate_result(result, request) == result
    else:
        with pytest.raises(ValueError, match="Unconfirmed publication"):
            validate_result(result, request)
