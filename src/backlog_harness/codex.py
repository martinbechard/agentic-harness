"""Codex Python SDK implementation of the harness agent adapter."""

import argparse
import json

from openai_codex import ApprovalMode, Codex, CodexConfig
from openai_codex.types import ReasoningEffort

from .adapter import execute


class CodexAdapter:
    def __init__(self, model=None, effort="high"):
        self.model = model
        self.effort = ReasoningEffort(effort)

    def run(self, *, instructions, request, schema, cwd, emit):
        # SDK owns app-server transport. Its child inherits the invocation group,
        # allowing the existing harness timeout/exit path to reap both processes.
        with Codex(CodexConfig(cwd=str(cwd))) as client:
            thread = client.thread_start(
                cwd=str(cwd),
                model=self.model,
                developer_instructions=instructions,
                approval_mode=ApprovalMode.deny_all,
            )
            emit({"event": "thread_started", "thread": thread.id})
            turn = thread.turn(json.dumps(request), effort=self.effort, output_schema=schema)
            response = None
            completed = False
            for notification in turn.stream():
                payload = notification.payload
                body = (
                    payload.model_dump(mode="json", by_alias=True)
                    if hasattr(payload, "model_dump")
                    else payload.params
                )
                event = {"method": notification.method, "payload": body}
                emit(event)
                payload = event["payload"]
                if event["method"] == "item/completed":
                    item = payload["item"]
                    if item["type"] == "agentMessage" and item.get("phase") in (
                        None,
                        "final_answer",
                    ):
                        response = item["text"]
                if event["method"] == "turn/completed":
                    completed = payload["turn"]["status"] == "completed"
                    if not completed:
                        raise RuntimeError(f"Codex turn did not complete: {payload['turn']}")
            if not completed or response is None:
                raise RuntimeError("Codex turn ended without a completed final response")
            return json.loads(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--effort", default="high", choices=[e.value for e in ReasoningEffort])
    args = parser.parse_args()
    execute(CodexAdapter(args.model, args.effort))


if __name__ == "__main__":
    main()
