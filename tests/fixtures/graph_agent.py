"""Deterministic graph worker responses; only the external agent is simulated."""

import json
import os
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


def git(repository, *arguments):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def request_object(prompt):
    request = prompt.split("Workflow request:\n", 1)[-1]
    decoder = json.JSONDecoder()
    for line in request.splitlines():
        if line.startswith("{"):
            value, _ = decoder.raw_decode(line)
            if isinstance(value, dict):
                return value
    raise ValueError("Graph request object missing")


def canonical_digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_review_request(prompt):
    decoder = json.JSONDecoder()
    for line in reversed(prompt.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            value, _ = decoder.raw_decode(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("review_requirements"):
            return value
    return None


def native_review(
    native_home, producer, candidate, source_review=None, fault=None, coordination=None
):
    """Emit simulated native child records consumed by the unchanged real verifier."""
    root = Path(native_home) / "sessions"
    parents = list(root.rglob(f"*{producer}.jsonl"))
    if len(parents) != 1:
        raise ValueError("Expected one initialized producer log")
    reviewer = str(uuid4())
    timestamp = datetime.now(UTC).isoformat()
    call_id = "review-" + reviewer
    parent_records = []

    assessment = None
    if source_review:
        contract = source_review["review_requirements"]
        assessment = {
            "candidate": candidate,
            "requirements_digest": canonical_digest(contract),
            "unresolved_findings": ["Evaluation weakening remains unresolved"] if fault == "weakening" else [],
            "conclusions": [{
                "id": row["id"],
                "canonical_reference": row["canonical_reference"],
                "verdict": "REJECT" if fault == "weakening" else "ACCEPT",
                "conclusion": "Candidate preserves the selected acceptance requirement",
                "evidence_paths": source_review["allowed_source_paths"],
            } for row in contract["requirements"]],
        }

    message = "Review exact candidate " + candidate
    if coordination:
        message += "\n" + coordination
    parent_records += [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": call_id,
                "arguments": json.dumps(
                    {
                        "fork_turns": "none",
                        "task_name": "review",
                        "message": message,
                    }
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"agent_id": reviewer}),
            },
        },
    ]
    with parents[0].open("a") as stream:
        for record in parent_records:
            stream.write(json.dumps({"timestamp": timestamp, **record}) + "\n")
    child = root / datetime.now(UTC).strftime("%Y/%m/%d") / ("rollout-" + reviewer + ".jsonl")
    child.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": reviewer,
                "timestamp": timestamp,
                "parent_thread_id": producer,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": producer}}},
                "git": {"commit_hash": candidate},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"output_tokens": 1},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": json.dumps(
                    {
                        "candidate": candidate,
                        "verdict": "ACCEPT",
                        "unresolved_findings": [],
                        **(
                            {"source_review_assessment": assessment}
                            if assessment is not None
                            else {}
                        ),
                    }
                ),
            },
        },
    ]
    child.write_text("".join(json.dumps({"timestamp": timestamp, **r}) + "\n" for r in records))
    return reviewer


def handle_graph(prompt, cwd, native_home, session_id):
    """Return None for common observation/provider calls handled by system dispatcher."""
    if "Assess this generation guard incident" in prompt:
        request = request_object(prompt)
        scenario = os.environ.get("HARNESS_SYSTEM_SCENARIO", "")
        operation = (
            "resume"
            if scenario in {"graph-hold-resume", "graph-unknown-resume"}
            else ("rethink" if scenario == "graph-hold-rethink" else "escalate")
        )
        return {
            "item_id": request["item_id"],
            "revision": request["revision"],
            "retained_owner": request["owner"],
            "operation": operation,
            "cause": "Observed workflow output exceeded the original one-token estimate"
            if not scenario.startswith("graph-unknown")
            else "Acceptance returned without usage accounting",
            "revised_approach": "Continue from the retained graph checkpoint; preserve completed work"
            if operation == "resume"
            else "Retain the candidate and resolve the accounting or approach",
            "remaining_high": 20,
        }
    if "Decide admission only" in prompt:
        request = request_object(prompt)
        return {
            "operation": "new",
            "item_id": request["item_id"],
            "provider_revision": request["provider_revision"],
            "reason": "Authorized fixture",
        }
    if "Accept canonical ownership read-only" in prompt:
        return {"item_id": request_object(prompt)["item_id"], "accepted": True}
    if "Classify this exact answer read-only" in prompt:
        request = request_object(prompt)
        return {
            "question_id": request["question"]["question_id"],
            "answer_digest": request["answer"]["digest"],
            "disposition": "approve",
        }
    if "Implement only the assigned candidate paths" not in prompt:
        return None
    request = request_object(prompt)
    if os.environ.get("HARNESS_SYSTEM_SCENARIO") == "graph-question" and not request.get("answer"):
        return {
            "item_id": request["item_id"],
            "question": {
                "question_id": "language",
                "text": "Which language should the answer use?",
            },
        }
    repository = Path(cwd)
    allowed = request["assignment"]["workflow"]["allowed_paths"]
    if allowed != ["answer.txt"]:
        raise ValueError("Graph fixture expects the exact answer.txt scope")
    (repository / "answer.txt").write_text("done\n")
    git(repository, "add", "--", "answer.txt")
    git(repository, "commit", "-m", "Produce reviewed graph answer")
    candidate = git(repository, "rev-parse", "HEAD")
    reviewer = native_review(
        native_home,
        session_id,
        candidate,
        source_review_request(prompt),
        os.environ.get("SOURCE_REVIEW_FAULT"),
        None,
    )
    return {
        "item_id": request["item_id"],
        "candidate": candidate,
        "reviewer_session": reviewer,
        "request_completion": True,
    }


def dispatch(prompt, cwd, native_home, argv, session_id):
    import runpy

    result = handle_graph(prompt, cwd, native_home, session_id)
    if result is not None:
        return result
    common = runpy.run_path(str(Path(__file__).with_name("legacy_agent.py")))
    return common["dispatch"](prompt, cwd, native_home, argv, session_id)


def transport_usage(prompt):
    if (
        os.environ.get("HARNESS_SYSTEM_SCENARIO") in {"graph-unknown", "graph-unknown-resume"}
        and "Accept canonical ownership read-only" in prompt
    ):
        return {"output_tokens": 1, "export_usage": False, "terminal_usage": False}
    return {}
