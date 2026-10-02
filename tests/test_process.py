"""Exercise the real command/result boundary and process-group cleanup."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from backlog_harness.codex import schema_for
from backlog_harness.engine import Outcome
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


@pytest.mark.parametrize("configured", [False, True])
def test_codex_bridge_preserves_profile_and_uses_structured_final_message(tmp_path, configured):
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\n" + "import json,sys\nfrom pathlib import Path\n"
        "Path('arguments.json').write_text(json.dumps(sys.argv[1:]))\n"
        "Path('prompt.txt').write_text(sys.stdin.read())\n"
        "target=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
        "target.write_text(json.dumps({'items':[]}))\n"
    )
    fake.chmod(0o755)

    async def scenario():
        process = await Process.start(
            [
                sys.executable,
                "-m",
                "backlog_harness.codex",
                "--executable",
                str(fake),
                *(["--profile", "test", "--model", "chosen-model"] if configured else []),
            ],
            {"role": "access", "action": "ready"},
            tmp_path,
            tmp_path / "state",
            lambda *a, **kw: None,
        )
        assert await process.response() == {"items": []}
        arguments = json.loads((tmp_path / "arguments.json").read_text())
        if configured:
            assert arguments[arguments.index("--profile") + 1] == "test"
            assert arguments[arguments.index("--model") + 1] == "chosen-model"
        assert "--dangerously-bypass-approvals-and-sandbox" not in arguments
        assert "--sandbox" not in arguments
        schema = json.loads(Path(arguments[arguments.index("--output-schema") + 1]).read_text())
        assert schema["required"] == ["items"]
        assert "installed provider/delivery skills" in (tmp_path / "prompt.txt").read_text()

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


@pytest.mark.parametrize("action", ["ready", "status", "failure", "hold", "epic_complete"])
def test_codex_provider_schema_is_strict(action):
    schema = schema_for({"role": "access", "action": action})
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
