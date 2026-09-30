"""Asynchronous operator terminal, sharing the dashboard's evidence projection."""

import asyncio
import json
import shlex

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from .coordination import RunController
from .projections import snapshot
from .provider import TransitionBlocked

HELP = """run --until-terminal | run --watch
pause | resume | stop | reconcile | status
item show ITEM_ID | item answer ITEM_ID QUESTION_ID | session show SESSION_ID
resume-answer ITEM_ID | recover-provider OPERATION_ID
review-hold ITEM_ID CEILING APPROVAL_REFERENCE
help | quit"""


async def terminal(app):
    def publish(value):
        print(json.dumps(value, ensure_ascii=False))

    controller = RunController(app, publish)
    prompt = PromptSession()
    runner = None
    print("Agentic Harness — type help for commands")
    with patch_stdout():
        while True:
            if runner is not None and runner.done():
                try:
                    publish({"run_result": runner.result()})
                except (ValueError, OSError, RuntimeError) as exc:
                    publish({"error": str(exc)})
                runner = None
            try:
                command = await prompt.prompt_async("harness> ")
                parts = shlex.split(command)
                if not parts:
                    continue
                if parts == ["help"]:
                    print(HELP)
                elif parts in (["run", "--until-terminal"], ["run", "--watch"]):
                    if runner and not runner.done():
                        raise TransitionBlocked("A run already exists")
                    runner = asyncio.create_task(controller.run(parts[1][2:]))
                elif parts == ["pause"]:
                    controller.pause()
                elif parts == ["resume"]:
                    controller.resume()
                    if not runner or runner.done():
                        # Explicit resume retains the previous mode and all item identities.
                        controller.state = "AdmissionPaused"
                        runner = asyncio.create_task(
                            controller.run(controller.mode or "until-terminal")
                        )
                elif parts == ["stop"]:
                    publish(await controller.stop())
                    if runner:
                        await runner
                        runner = None
                elif parts == ["reconcile"]:
                    publish({"reconciliation": app.reconcile()})
                elif parts == ["status"]:
                    value = await asyncio.to_thread(snapshot, app)
                    publish(
                        {
                            key: value[key]
                            for key in (
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
                        }
                    )
                elif len(parts) == 2 and parts[0] == "resume-answer":
                    publish(await app.resume_answer(parts[1]))
                elif len(parts) == 2 and parts[0] == "recover-provider":
                    publish(app.recover_provider(parts[1]))
                elif len(parts) == 3 and parts[:2] == ["item", "show"]:
                    item = app.provider.item(parts[2])
                    publish(
                        {
                            "item_id": item.item_id,
                            "revision": item.revision,
                            "state": item.state,
                            "content": item.content,
                            "usage": app.usage_view(item.item_id),
                        }
                    )
                elif len(parts) == 4 and parts[:2] == ["item", "answer"]:
                    item = app.provider.item(parts[2])
                    question = app.provider.question(item)
                    if not question or question["question_id"] != parts[3]:
                        raise TransitionBlocked("Current question identity differs")
                    print(question["text"])
                    text = await prompt.prompt_async("answer> ")
                    publish(
                        await app.answer(item.item_id, question["question_id"], item.revision, text)
                    )
                elif len(parts) == 3 and parts[:2] == ["session", "show"]:
                    from .evidence import read_jsonl

                    found = []
                    for path in (app.root / "runs").glob(
                        "*/operations/*/invocations/*/session.json"
                    ):
                        session = json.loads(path.read_text())
                        if session["session_id"] == parts[2]:
                            events, _, partial = read_jsonl(path.parent / "events.jsonl")
                            found.append(
                                {"session": session, "events": events[-20:], "partial": partial}
                            )
                    if not found:
                        raise TransitionBlocked("Portable session is absent")
                    publish({"session_evidence": found})
                elif len(parts) == 4 and parts[0] == "review-hold":
                    publish(
                        await app.review_hold(
                            parts[1], requested_ceiling=float(parts[2]), reference=parts[3]
                        )
                    )
                elif parts == ["quit"]:
                    if runner and not runner.done():
                        raise TransitionBlocked("Stop the active run before quitting")
                    return 0
                else:
                    raise TransitionBlocked("Unknown command; type help")
            except EOFError:
                if runner and not runner.done():
                    await controller.stop()
                    await runner
                return 0
            except KeyboardInterrupt:
                print("Input cancelled; use stop to end the run")
            except (ValueError, OSError, RuntimeError) as exc:
                publish({"error": str(exc)})
