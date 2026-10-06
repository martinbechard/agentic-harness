"""SDK wire tests without model calls; alternate backend contract and cleanup."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from backlog_harness.adapter import execute, instructions_for
from backlog_harness.process import Process

SERVER = Path(__file__).parent / "fixtures/codex_server.py"


def test_another_adapter_uses_same_request_result_and_event_contract(tmp_path, monkeypatch, capsys):
    request = {"role": "development", "instruction": "Deliver this item"}
    source, target = tmp_path / "request.json", tmp_path / "result.json"
    source.write_text(json.dumps(request))
    monkeypatch.setenv("HARNESS_REQUEST", str(source))
    monkeypatch.setenv("HARNESS_RESULT", str(target))
    monkeypatch.chdir(tmp_path)

    class AlternateAdapter:
        def run(self, *, instructions, request, schema, cwd, emit):
            assert cwd == tmp_path
            assert request["instruction"] == "Deliver this item"
            assert "development agent" in instructions
            assert schema["properties"]["status"]["enum"] == [
                "success",
                "failed",
                "user_action_required",
            ]
            emit({"activity": "delivered"})
            return {"status": "success", "transient": False, "detail": "done"}

    execute(AlternateAdapter())
    assert json.loads(target.read_text())["status"] == "success"
    assert json.loads(capsys.readouterr().out) == {"activity": "delivered"}
    assert not target.with_suffix(".tmp").exists()


async def sdk_process(tmp_path, mode):
    capture = tmp_path / "rpc.jsonl"
    # Replace only the SDK's external app-server executable, not the SDK itself.
    runner = tmp_path / "run_adapter.py"
    runner.write_text(
        "from openai_codex import Codex, CodexConfig\n"
        "from backlog_harness import codex\n"
        f"codex.Codex = lambda config: Codex(CodexConfig(cwd=config.cwd, launch_args_override="
        f"{(sys.executable, str(SERVER.resolve()), str(capture), mode)!r}))\n"
        "codex.main()\n"
    )
    events = []
    process = await Process.start(
        [sys.executable, str(runner), "--model", "chosen-model", "--effort", "high"],
        {"role": "access", "action": "ready", "limit": 1},
        tmp_path,
        tmp_path / "state",
        lambda event, **fields: events.append((event, fields)),
    )
    return process, capture, events


def test_codex_sdk_transmits_instructions_schema_and_streams_activity(tmp_path):
    async def scenario():
        process, capture, events = await sdk_process(tmp_path, "success")
        assert await process.response() == {"items": []}, process.output_path.read_text()
        assert process.process.returncode == 0
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        start = next(c["params"] for c in calls if c["method"] == "thread/start")
        turn = next(c["params"] for c in calls if c["method"] == "turn/start")
        assert start["model"] == "chosen-model"
        assert start["cwd"] == str(tmp_path)
        assert "access agent" in start["developerInstructions"]
        assert "self-archive this chat" in start["developerInstructions"]
        assert "sandbox" not in start
        assert start["approvalPolicy"] == "never"
        assert turn["effort"] == "high"
        assert turn["outputSchema"]["required"] == ["items"]
        assert json.loads(turn["input"][0]["text"])["limit"] == 1
        output = "".join(fields["text"] for event, fields in events if event == "agent_output")
        assert '"thread": "thread-fixture"' in output
        assert '"text": "Working"' in output
        assert '"method": "turn/completed"' in output

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["failed", "interrupted", "missing", "malformed", "array", "disconnect"]
)
def test_sdk_failure_does_not_publish_a_success_result(tmp_path, mode):
    async def scenario():
        process, capture, _ = await sdk_process(tmp_path, mode)
        assert await process.response() is None
        assert process.process.returncode != 0
        expected = {
            "failed": "Codex turn did not complete",
            "interrupted": "Codex turn did not complete",
            "missing": "without a completed final response",
            "malformed": "JSONDecodeError",
            "array": "Agent result must be an object",
            "disconnect": "closed stdout",
        }
        assert expected[mode] in process.output_path.read_text()
        assert not process.result.exists()
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        assert any(c["method"] == "turn/start" for c in calls)

    asyncio.run(scenario())


def test_cancellation_reaps_sdk_app_server(tmp_path):
    async def scenario():
        process, capture, _ = await sdk_process(tmp_path, "block")
        try:

            async def ready():
                while not capture.with_suffix(".pid").exists():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(ready(), 10)
            pid = int(capture.with_suffix(".pid").read_text())
            assert os.getpgid(pid) == process.process.pid
            await process.kill()

            async def stopped():
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(stopped(), 5)
            assert not process.result.exists()
        finally:
            await process.kill()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("assignment", "expected_name"),
    [
        ({"role": "access", "action": "ready"}, "Backlog — Find ready work"),
        (
            {"role": "access", "action": "status", "item": {"id": "fix"}},
            "Backlog — Check status — fix",
        ),
        (
            {"role": "access", "action": "failure", "item": {"id": "fix"}},
            "Backlog — Record failure — fix",
        ),
        ({"role": "access", "action": "hold", "item": {"id": "fix"}}, "Backlog — Hold item — fix"),
        (
            {"role": "access", "action": "decision", "submission": {"item_id": "fix"}},
            "Backlog — Process decision — fix",
        ),
        (
            {"role": "access", "action": "epic_complete", "epic": "release"},
            "Backlog — Check epic completion — release",
        ),
        ({"role": "development", "item": {"id": "fix"}}, "Develop — fix"),
        ({"role": "merge"}, "Merge — {project}"),
        ({"instruction": "Return receipt"}, "Harness — {project}"),
    ],
)
def test_exact_instructions_are_received_by_sdk_server(
    tmp_path, monkeypatch, assignment, expected_name
):
    from openai_codex import Codex, CodexConfig

    from backlog_harness import codex

    capture = tmp_path / "received.jsonl"
    monkeypatch.setattr(
        codex,
        "Codex",
        lambda config: Codex(
            CodexConfig(
                cwd=config.cwd,
                launch_args_override=(
                    sys.executable,
                    str(SERVER.resolve()),
                    str(capture),
                    "success",
                ),
            )
        ),
    )
    instructions = 'Follow the assigned provider.\nPreserve café.txt and literal "$HOME".\n'
    schema = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}
    result = codex.CodexAdapter().run(
        instructions=instructions,
        request=assignment,
        schema=schema,
        cwd=tmp_path,
        emit=lambda e: None,
    )
    assert result == {"items": []}
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    start = next(c["params"] for c in calls if c["method"] == "thread/start")
    turn = next(c["params"] for c in calls if c["method"] == "turn/start")
    name = next(c["params"] for c in calls if c["method"] == "thread/name/set")
    assert name == {
        "threadId": "thread-fixture",
        "name": expected_name.format(project=tmp_path.name),
    }
    methods = [c["method"] for c in calls]
    assert methods.index("thread/start") < methods.index("thread/name/set")
    assert methods.index("thread/name/set") < methods.index("turn/start")
    assert start["developerInstructions"] == instructions
    assert json.loads(turn["input"][0]["text"]) == assignment
    assert turn["outputSchema"] == schema


def test_sdk_entry_point_has_no_cli_or_context_file_escape_hatches():
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "backlog_harness.codex", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--model" in result.stdout
    assert "--effort" in result.stdout
    for removed in ("--executable", "--profile", "--context-file"):
        assert removed not in result.stdout


@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_LIVE") != "1", reason="Opt-in authenticated model call"
)
def test_live_codex_receives_instructions(tmp_path):
    import secrets
    import subprocess

    from backlog_harness.codex import CodexAdapter

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nonce = secrets.token_hex(16)
    instructions = (
        "Isolated instruction-delivery test. Do not use tools or read files. "
        f"Return a JSON object whose receipt field is exactly {nonce}."
    )
    schema = {
        "type": "object",
        "properties": {"receipt": {"type": "string"}},
        "required": ["receipt"],
        "additionalProperties": False,
    }
    events = []
    result = CodexAdapter(model="gpt-6-luna").run(
        instructions=instructions,
        request={"instruction": "Return the receipt required by your developer instructions."},
        schema=schema,
        cwd=tmp_path,
        emit=events.append,
    )
    (tmp_path / "sdk-events.json").write_text(json.dumps(events, indent=2))
    assert result == {"receipt": nonce}
    assert any(e.get("method") == "turn/completed" for e in events)


@pytest.mark.parametrize("role", ["access", "merge", "development", "unblock"])
def test_self_archive_prompt_targets_disposable_roles(role):
    instructions = instructions_for({"role": role})
    assert ("self-archive this chat" in instructions) == (role in ("access", "merge"))
    if role in ("access", "merge"):
        assert "20 minutes of inactivity" in instructions
        assert "Preserve the required structured final result" in instructions
        assert "If the tool is unavailable or archiving fails" in instructions
