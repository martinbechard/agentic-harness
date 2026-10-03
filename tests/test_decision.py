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
