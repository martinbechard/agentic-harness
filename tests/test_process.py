"""Exercise the real command/result boundary and process-group cleanup."""

import asyncio
import json
import sys

import pytest

from backlog_harness.adapter import schema_for
from backlog_harness.engine import Item, Outcome
from backlog_harness.process import Agents, Process


async def start(tmp_path, code):
    events = []
    agent = await Process.start(
        [sys.executable, "-c", code],
        {"role": "development"},
        tmp_path,
        tmp_path / "state",
        lambda event, **kw: events.append((event, kw)),
    )
    return agent, events


def test_selected_scheduling_authority_is_supplied_to_every_agent_role(tmp_path):
    async def scenario():
        config = {
            "project": str(tmp_path),
            "state": str(tmp_path / "state"),
            **{role: [sys.executable, "agent.py"] for role in ("access", "development", "merge")},
        }
        agents = Agents(config, lambda *_args, **_fields: None)
        captured = []

        async def ask(action, **payload):
            captured.append(("access", payload))
            return {"items": []}

        async def launch(role, payload, cwd=None):
            captured.append((role, payload))
            return object()

        agents.ask = ask
        await agents.ready(
            2,
            None,
            False,
            [],
            scheduling={"capacity": 2, "mode": "parallel", "revision": 1, "request_id": "r"},
        )
        selection = captured[0][1]["instruction"]
        for boundary in (
            "publish a lifecycle-only update",
            "independently of product acceptance",
            "do not merge or cherry-pick unfinished product changes",
            "must not reopen delivered completion or undo a newer human answer",
            "not proof that a pending question is resolved",
            "Never publish dirty worktree evidence",
            "or competing revisions are ambiguous",
            "verify the published lifecycle, question and revision",
            "Unchanged repeat scans must not append duplicate history",
        ):
            assert boundary in selection
        agents.start = launch
        await agents.deliver(Item("one", str(tmp_path), "one"), False)
        await agents.integrate()

        assert [role for role, _ in captured] == ["access", "development", "merge"]
        for _, payload in captured:
            instruction = payload["instruction"]
            assert "human user selected parallel scheduling with capacity 2" in instruction
            assert (
                "supersedes project guidance that fixes Work item scheduling to SOLO" in instruction
            )
            assert "does not change claim-free restrictions" in instruction
            assert "ALL prerequisites" in instruction
            assert "Waiting is distinct from Blocked" in instruction
            assert "serial merge checks" in instruction

    asyncio.run(scenario())


def test_output_is_streamed_and_result_read_only_after_exit(tmp_path):
    async def scenario():
        agent, events = await start(
            tmp_path,
            "import os,json,time; from pathlib import Path; print('activity',flush=True); "
            "Path(os.environ['HARNESS_RESULT']).write_text(json.dumps({'status':'success'})); "
            "time.sleep(.05)",
        )
        result = await agent.wait()
        assert result == Outcome("success")
        assert agent.process.returncode == 0
        assert any(e == "agent_output" and "activity" in v["text"] for e, v in events)
        assert agent.output_path.read_text() == "activity\n"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result",
    [
        "not json",
        "[]",
        '{"status":"wrong"}',
        '{"status":"failed","transient":"yes"}',
        '{"status":"failed","detail":5}',
    ],
)
def test_bad_results_are_reported_failures(tmp_path, result):
    async def scenario():
        agent, _ = await start(
            tmp_path,
            "import os; from pathlib import Path; "
            f"Path(os.environ['HARNESS_RESULT']).write_text({result!r})",
        )
        assert (await agent.wait()).status == "failed"

    asyncio.run(scenario())


def test_nonzero_exit_cannot_claim_success(tmp_path):
    async def scenario():
        agent, _ = await start(
            tmp_path,
            "import os; from pathlib import Path; "
            "Path(os.environ['HARNESS_RESULT']).write_text('{\"status\":\"success\"}'); exit(1)",
        )
        assert (await agent.wait()).status == "failed"

    asyncio.run(scenario())


def test_stopped_parent_with_live_child_is_reaped_promptly(tmp_path):
    async def scenario():
        agent, _ = await start(
            tmp_path,
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])",
        )
        assert await asyncio.wait_for(agent.wait(), 1) is None
        assert agent.output.done()

    asyncio.run(scenario())


def test_kill_waits_for_process_and_output_cleanup(tmp_path):
    async def scenario():
        agent, _ = await start(tmp_path, "import time; time.sleep(30)")
        waiter = asyncio.create_task(agent.wait())
        await agent.kill()
        await waiter
        assert agent.process.returncode < 0
        assert agent.output.done()
        await agent.kill()  # cleanup is idempotent

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [{}, {"updated": False}, {"updated": "true"}])
def test_provider_hold_requires_confirmed_update(tmp_path, value):
    async def scenario():
        command = [
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                f"Path(os.environ['HARNESS_RESULT']).write_text({json.dumps(value)!r})"
            ),
        ]
        agents = Agents(
            {"project": str(tmp_path), "state": str(tmp_path / "state"), "access": command},
            lambda *a, **kw: None,
        )
        with pytest.raises((RuntimeError, ValueError)):
            await agents.ask("hold")

    asyncio.run(scenario())


def test_provider_timeout_is_bounded_and_logged(tmp_path):
    async def scenario():
        events = []
        agents = Agents(
            {
                "project": str(tmp_path),
                "state": str(tmp_path / "state"),
                "access": [sys.executable, "-c", "import time; time.sleep(30)"],
                "access_timeout": 0.03,
            },
            lambda event, **kw: events.append(event),
        )
        with pytest.raises(RuntimeError, match="timed out"):
            await agents.ask("ready")
        assert "agent_group_stopped" in events

    asyncio.run(scenario())


def test_incomplete_utf8_output_is_retained_and_logged(tmp_path):
    async def scenario():
        agent, events = await start(tmp_path, "import os; os.write(1, b'\\xe2')")
        await agent.wait()
        assert agent.output_path.read_bytes() == b"\xe2"
        assert any(e == "agent_output" and v["text"] == "\ufffd" for e, v in events)

    asyncio.run(scenario())


def test_oversized_result_is_rejected(tmp_path):
    async def scenario():
        agent, _ = await start(
            tmp_path,
            "import os; from pathlib import Path; "
            "Path(os.environ['HARNESS_RESULT']).write_text('x' * 1048577)",
        )
        assert "exceeds" in (await agent.wait()).detail

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action,value", [("ready", {"items": 3}), ("status", {"status": 3}), ("ready", None)]
)
def test_provider_invalid_responses_are_rejected(tmp_path, action, value):
    async def scenario():
        code = (
            "pass"
            if value is None
            else (
                "import os; from pathlib import Path; "
                f"Path(os.environ['HARNESS_RESULT']).write_text({json.dumps(value)!r})"
            )
        )
        agents = Agents(
            {
                "project": str(tmp_path),
                "state": str(tmp_path / "state"),
                "access": [sys.executable, "-c", code],
            },
            lambda *a, **kw: None,
        )
        with pytest.raises((RuntimeError, ValueError)):
            if action == "ready":
                await agents.ready(1, None, False, [])
            else:
                await agents.ask(action)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action",
    ["ready", "status", "failure", "hold", "epic_complete", "decision", "blocked_count", "unblock"],
)
def test_codex_provider_schema_is_strict(action):
    schema = schema_for({"role": "access", "action": action})
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("mode", ["missing", "failed", "invalid", "success"])
def test_child_exit_and_result_events_distinguish_outcomes(tmp_path, mode):
    async def scenario():
        code = {
            "missing": "import sys; sys.exit(7)",
            "invalid": "import os; open(os.environ['HARNESS_RESULT'],'w').write('bad json')",
            "failed": "import os,json; open(os.environ['HARNESS_RESULT'],'w').write(json.dumps({'status':'failed'}))",
            "success": "import os,json; open(os.environ['HARNESS_RESULT'],'w').write(json.dumps({'status':'success'}))",
        }[mode]
        events = []
        agent = await Process.start(
            [sys.executable, "-c", code],
            {"role": "development", "item": {"id": "one"}},
            tmp_path,
            tmp_path / "state",
            lambda e, **kw: events.append((e, kw)),
        )
        outcome = await agent.wait()
        exit_event = next(v for e, v in events if e == "agent_exited")
        assert exit_event == {
            "invocation": agent.id,
            "role": "development",
            "item": "one",
            "pid": agent.process.pid,
            "exit_code": 7 if mode == "missing" else 0,
            "stop_requested": False,
        }
        result_event = next(v for e, v in events if e == "agent_result")
        assert result_event["result_state"] == (
            mode if mode in {"missing", "invalid"} else "reported"
        )
        assert (outcome is None) == (mode == "missing")
        await agent.kill()
        assert sum(e == "agent_exited" for e, _ in events) == 1
        assert all(v["reason"] == "cleanup" for e, v in events if e == "agent_group_stopped")

    asyncio.run(scenario())


def test_agent_registry_and_requested_stop_events(tmp_path):
    async def scenario():
        events = []
        agents = Agents(
            {
                "project": str(tmp_path),
                "state": str(tmp_path / "state"),
                "development": [sys.executable, "-c", "import time; time.sleep(60)"],
            },
            lambda e, **kw: events.append((e, kw)),
        )
        agent = await agents.start("development", {"item": {"id": "one"}})
        assert agents.running_invocations() == [agent.identity]
        await agent.kill()
        assert agents.running_invocations() == []
        assert agents.invocations == {}
        event = next(v for e, v in events if e == "agent_exited")
        assert event["stop_requested"] is True
        assert event["exit_code"] != 0
        assert event["item"] == "one"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action,value",
    [
        ("blocked_count", {"blocked_count": True}),
        ("blocked_count", {"blocked_count": -1}),
        ("unblock", {"status": "invalid", "detail": ""}),
        ("unblock", {"status": "success"}),
        ("unblock", {"status": "success", "detail": "", "extra": 1}),
    ],
)
def test_invalid_blocked_responses_are_rejected(tmp_path, action, value):
    async def scenario():
        code = (
            "import os; from pathlib import Path; "
            f"Path(os.environ['HARNESS_RESULT']).write_text({json.dumps(value)!r})"
        )
        agents = Agents(
            {
                "project": str(tmp_path),
                "state": str(tmp_path / "state"),
                "access": [sys.executable, "-c", code],
            },
            lambda *a, **kw: None,
        )
        with pytest.raises(ValueError):
            await agents.ask(action)

    asyncio.run(scenario())


def test_blocked_agent_flow_and_timeout_reap_real_process(tmp_path):
    from backlog_harness.engine import Harness

    async def scenario():
        code = """import json,os,time
from pathlib import Path
request = json.loads(Path(os.environ['HARNESS_REQUEST']).read_text())
assert request['epic'] == 'release' and request['excluded'] == []
if request['action'] == 'blocked_count':
    result = {'blocked_count': 1}
else:
    assert request['action'] == 'unblock'
    assert 'Do not invent approvals or answers' in request['instruction']
    if Path('hang').exists():
        time.sleep(60)
    result = {'status': 'success', 'detail': 'blocker resolved'}
Path(os.environ['HARNESS_RESULT']).write_text(json.dumps(result))
"""
        events = []
        emit = lambda event, **kw: events.append((event, kw))
        agents = Agents(
            {
                "project": str(tmp_path),
                "state": str(tmp_path / "state"),
                "access": [sys.executable, "-c", code],
            },
            emit,
        )
        h = Harness(
            agents, agents.deliver, agents.integrate, emit, epic="release", unblock_timeout=5
        )
        await h.check_blocked()
        assert ("unblock_finished", {"status": "success", "detail": "blocker resolved"}) in events
        assert not agents.running_invocations()
        (tmp_path / "hang").touch()
        h.unblock_timeout = 0.2
        await h.check_blocked()
        assert events[-1][0] == "unblock_error"
        assert not agents.running_invocations()
        assert any(
            e == "agent_group_stopped" and f["reason"] == "stop_requested" for e, f in events
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["waiting", "blocked"])
def test_dependency_and_blocker_outcomes_survive_process_boundary(tmp_path, status):
    async def scenario():
        result = json.dumps({"status": status, "detail": "Published provider checkpoint"})
        agent, _ = await start(
            tmp_path,
            "import os; from pathlib import Path; "
            f"Path(os.environ['HARNESS_RESULT']).write_text({result!r})",
        )
        assert await agent.wait() == Outcome(status, detail="Published provider checkpoint")

    asyncio.run(scenario())
