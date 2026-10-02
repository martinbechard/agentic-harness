"""Operator commands preserve exact identities and keep active-run controls responsive."""

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backlog_harness import terminal as ui


@pytest.fixture
def operator(monkeypatch, tmp_path):
    item = SimpleNamespace(item_id="one", revision="rev", state="Running", content="source")
    app = SimpleNamespace(
        root=tmp_path,
        provider=SimpleNamespace(
            item=Mock(return_value=item),
            question=Mock(return_value={"question_id": "question", "text": "Proceed?"}),
        ),
        usage_view=Mock(return_value={"generated_tokens": 12}),
        reconcile=Mock(return_value=[]),
        resume_answer=AsyncMock(return_value={"resumed": True}),
        recover_provider=Mock(return_value={"recovered": True}),
        answer=AsyncMock(return_value={"answered": True}),
        review_hold=AsyncMock(return_value={"held": True}),
    )
    controller = SimpleNamespace(
        run=AsyncMock(return_value="finished"),
        pause=Mock(),
        resume=Mock(),
        stop=AsyncMock(return_value={"stopped": True}),
        mode=None,
        state=None,
    )
    monkeypatch.setattr(ui, "RunController", lambda app, publish: controller)
    monkeypatch.setattr(ui, "patch_stdout", nullcontext)

    def execute(commands):
        commands = iter(commands)

        async def prompt(_):
            await asyncio.sleep(0)
            command = next(commands, EOFError())
            if isinstance(command, BaseException):
                raise command
            return command

        monkeypatch.setattr(ui, "PromptSession", lambda: SimpleNamespace(prompt_async=prompt))
        return asyncio.run(ui.terminal(app))

    return app, controller, execute


def test_operator_commands_use_exact_item_and_question_identity(operator, monkeypatch, capsys):
    app, controller, execute = operator
    keys = (
        "revision",
        "as_of",
        "counts",
        "run_observation",
        "capability_blockers",
        "telemetry_receipt_binding",
        "items",
        "runtime_freshness",
        "trace_span_count",
        "uncertainty",
        "generation_configuration_error",
    )
    monkeypatch.setattr(ui, "snapshot", lambda app: dict.fromkeys(keys, None))
    assert (
        execute(
            [
                "",
                "help",
                "status",
                "pause",
                "reconcile",
                "item show one",
                "item answer one question",
                "approve",
                "resume-answer one",
                "recover-provider op",
                "review-hold one",
                "review-hold one 100 approval",
                "stop",
                "quit",
            ]
        )
        == 0
    )
    app.answer.assert_awaited_once_with("one", "question", "rev", "approve")
    app.resume_answer.assert_awaited_once_with("one")
    app.recover_provider.assert_called_once_with("op")
    assert app.review_hold.await_args_list[-1].kwargs == {
        "requested_ceiling": 100.0,
        "reference": "approval",
    }
    controller.pause.assert_called_once()
    assert '"generated_tokens": 12' in capsys.readouterr().out


@pytest.mark.parametrize("question", [None, {"question_id": "another"}])
def test_stale_question_cannot_submit_answer(operator, question, capsys):
    app, _, execute = operator
    app.provider.question.return_value = question
    execute(["item answer one question", "quit"])
    app.answer.assert_not_awaited()
    assert "Current question identity differs" in capsys.readouterr().out


def test_bad_input_and_interrupt_leave_terminal_usable(operator, capsys):
    _, _, execute = operator
    assert (
        execute(
            [
                KeyboardInterrupt(),
                "unknown",
                "'unterminated",
                "review-hold one bad ref",
                "session show missing",
                EOFError(),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Input cancelled" in output
    assert "Unknown command" in output
    assert "Portable session is absent" in output


def test_session_inspection_reads_matching_evidence(operator, capsys):
    app, _, execute = operator
    for name in ("matching", "other"):
        directory = app.root / "runs/run/operations/op/invocations" / name
        directory.mkdir(parents=True)
        (directory / "session.json").write_text(json.dumps({"session_id": name}))
        (directory / "events.jsonl").write_text('{"event_id":"event"}\n')
    execute(["session show matching", "quit"])
    output = capsys.readouterr().out
    assert '"session_id": "matching"' in output
    assert '"session_id": "other"' not in output


@pytest.mark.parametrize("command", ["run --until-terminal", "run --watch", "resume"])
@pytest.mark.parametrize("failure", [False, True])
def test_run_completion_and_failure_are_reported(operator, command, failure, capsys):
    _, controller, execute = operator
    if failure:
        controller.run.side_effect = RuntimeError("run failed")
    assert execute([command, "help", "quit"]) == 0
    output = capsys.readouterr().out
    assert ("run failed" if failure else '"run_result": "finished"') in output


@pytest.mark.parametrize("ending", ["stop", EOFError()])
def test_active_run_cannot_duplicate_or_quit_and_is_stopped_on_exit(operator, ending, capsys):
    _, controller, execute = operator
    event = None

    async def run(mode):
        nonlocal event
        event = asyncio.Event()
        await event.wait()
        return "finished"

    async def stop():
        event.set()
        return {"stopped": True}

    controller.run.side_effect = run
    controller.stop.side_effect = stop
    assert execute(["run --watch", "run --watch", "quit", "resume", ending, "quit"]) == 0
    controller.run.assert_awaited_once_with("watch")
    controller.stop.assert_awaited_once()
    output = capsys.readouterr().out
    assert "A run already exists" in output
    assert "Stop the active run before quitting" in output
