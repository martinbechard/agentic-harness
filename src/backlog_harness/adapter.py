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
        "decision": {
            **{
                key: {"type": "string"}
                for key in (
                    "decision_id",
                    "item_id",
                    "state",
                    "revision",
                    "workspace",
                    "locator",
                    "detail",
                )
            },
            "status": {
                "type": "string",
                "enum": ["applied", "already_applied", "already_resolved", "rejected", "unknown"],
            },
            "persisted": {"type": ["boolean", "null"]},
            "resolution": {
                "type": "string",
                "enum": ["approved", "cancelled", "answered", "retry_requested", "none"],
            },
        },
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
        "choose the work item provider and follow its conventions. When work_item_provider "
        "configuration selects file or github, use that provider and its configured scope. "
        "File paths are relative to the project root (or this item's assigned worktree); "
        "GitHub selection uses the configured repository and Ready label, with epic interpreted "
        "as an exact milestone title. Return file IDs as filename stems and GitHub IDs as issue "
        "URLs. The provider count already checks eligibility; revalidate dependencies, decisions "
        "and current lifecycle before selecting work. The harness does not "
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
