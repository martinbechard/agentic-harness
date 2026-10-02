"""Adapt the harness file protocol to Codex exec's structured final response."""

import argparse
import json
import os
import subprocess
from pathlib import Path


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="codex")
    parser.add_argument("--model")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--profile")
    parser.add_argument(
        "--context-file", type=Path, help="Project launch instructions sent in the prompt"
    )
    args = parser.parse_args()
    request = json.loads(Path(os.environ["HARNESS_REQUEST"]).read_text())
    result = Path(os.environ["HARNESS_RESULT"])
    schema_path = result.with_name("schema.json")
    schema_path.write_text(json.dumps(schema_for(request)))
    command = [
        args.executable,
        "exec",
        "--json",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result),
        "--config",
        f"model_reasoning_effort={json.dumps(args.effort)}",
    ]
    if args.model:
        command += ["--model", args.model]
    if args.profile:
        command += ["--profile", args.profile]
    prompt = (
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
        "in the agent output. Request data follows:\n" + json.dumps(request)
    )
    if args.context_file:
        prompt = args.context_file.read_text(encoding="utf-8") + "\n\n" + prompt
    # Inherit the configured Codex profile and sandbox; never bypass them here.
    completed = subprocess.run([*command, "-"], input=prompt, text=True, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
