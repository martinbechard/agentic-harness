"""Backend-neutral invocation contract and harness request/result boundary."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class AgentAdapter(Protocol):
    def run(
        self,
        *,
        instructions: str,
        request: dict,
        schema: dict,
        cwd: Path,
        emit: Callable[[dict], None],
    ) -> dict:
        """Run one assignment, stream activity, and return its structured result.

        Raise on runtime failure or interruption. Backend resources must be closed
        before returning; child processes must stay in the invocation process group.
        """


def schema_for(request):
    action = request.get("action") if request["role"] == "access" else None
    item = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ("id", "worktree", "branch")},
        "required": ["id", "worktree", "branch"],
        "additionalProperties": False,
    }
    properties = {
        "ready": {"items": {"type": "array", "items": item}},
        "status": {"status": {"type": "string"}},
        "failure": {"transient": {"type": "boolean"}},
        "hold": {"updated": {"type": "boolean"}},
        "epic_complete": {"complete": {"type": "boolean"}},
        None: {
            "status": {"type": "string", "enum": ["success", "failed", "user_action_required"]},
            "transient": {"type": "boolean"},
            "detail": {"type": "string"},
        },
    }[action]
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def instructions_for(request):
    return (
        "You are the " + request["role"] + " agent for a backlog delivery harness. "
        "Use the current project's instructions and installed provider/delivery skills to "
        "choose the work item provider and follow its conventions. The harness does not "
        "implement provider transactions. Perform the requested operation and return the "
        "structured final result. Do not ask the harness to implement project conventions. "
        "For access status, normalize active delivery to 'running'. For access failure, "
        "record the failure on the item and classify whether it is transient. For access "
        "hold, move the item to holding and return updated=true only after success. "
        "For epic_complete, return true only when all epic items are delivered. "
        "Operate on filesystem work items in the current worktree. Report your activities "
        "in the agent output."
    )


def execute(adapter: AgentAdapter):
    request = json.loads(Path(os.environ["HARNESS_REQUEST"]).read_text())
    result = adapter.run(
        instructions=instructions_for(request),
        request=request,
        schema=schema_for(request),
        cwd=Path.cwd(),
        emit=lambda event: print(json.dumps(event), flush=True),
    )
    if not isinstance(result, dict):
        raise TypeError("Agent result must be an object")
    destination = Path(os.environ["HARNESS_RESULT"])
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(destination)
