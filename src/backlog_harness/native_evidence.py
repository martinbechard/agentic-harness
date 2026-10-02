"""Read native session evidence to verify a review arranged by its producing agent."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from .contracts import digest
from .workflow import require


def coordination_instructions(context):
    """Tell an agent and its native delegates which validated claim rule applies."""
    behavior = (
        "Claims are required: manage them through the configured helper when the selected workflow "
        "requires an operation."
        if context["claims_required"]
        else "Claims are prohibited: do not acquire, release, renew, inspect, restore, or substitute claims."
    )
    return (
        "\nCurrent claim coordination authority is bound to this invocation. "
        + behavior
        + " Pass the applicable coordination rules to native delegates and reviewers; "
        "the block is authority context, not permission to widen source or lifecycle scope.\n"
        + json.dumps(context, sort_keys=True, separators=(",", ":"))
    )


def source_review_instructions(workflow, canonical_acceptance, workdir):
    """Describe substantive review requirements; agents arrange their own review."""
    contract = workflow.get("review_requirements")
    if not contract:
        return ""
    return (
        "\nThis item has an explicit source-review requirement. Arrange one fresh native reviewer "
        "for the exact candidate. Pass the canonical acceptance and selected requirements below. "
        "The reviewer returns candidate, verdict, unresolved_findings, and source_review_assessment "
        "containing candidate, requirements_digest, unresolved_findings, and conclusions. Each "
        "conclusion identifies the requirement id and canonical_reference, verdict, a substantive "
        "conclusion, and supporting evidence_paths. Do not accept evaluation weakening merely "
        "because tests pass. The harness runs the configured checks and binds their results to "
        "the candidate; no duplicated command execution or copied output packet is required.\n"
        + json.dumps(
            {
                "candidate_worktree": str(Path(workdir).resolve()),
                "canonical_acceptance": canonical_acceptance,
                "review_requirements": contract,
                "requirements_digest": digest(contract),
                "allowed_source_paths": workflow["allowed_paths"],
            },
            sort_keys=True,
        )
    )


def source_review_context(workflow, harness_checks):
    """Use the selected requirements and harness-observed candidate check results."""
    return {
        "commands": [list(argv) for argv in workflow["checks"]],
        "review_requirements": workflow["review_requirements"],
        "allowed_source_paths": workflow["allowed_paths"],
        "harness_checks": harness_checks,
    }


def _validate_source_review(verdict, candidate, source_review):
    contract = source_review["review_requirements"]
    assessment = verdict.get("source_review_assessment")
    require(
        isinstance(assessment, dict)
        and assessment.get("candidate") == candidate
        and assessment.get("requirements_digest") == digest(contract)
        and assessment.get("unresolved_findings") == [],
        "Source review assessment is missing, stale or unresolved",
    )
    conclusions = assessment.get("conclusions")
    requirements = {row["id"]: row for row in contract["requirements"]}
    require(
        isinstance(conclusions, list)
        and len(conclusions) == len(requirements)
        and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in conclusions)
        and {row["id"] for row in conclusions} == set(requirements),
        "Source review requirement conclusions are incomplete or duplicated",
    )
    for conclusion in conclusions:
        requirement = requirements[conclusion["id"]]
        paths = conclusion.get("evidence_paths")
        require(
            conclusion.get("canonical_reference") == requirement["canonical_reference"]
            and conclusion.get("verdict") == "ACCEPT"
            and isinstance(conclusion.get("conclusion"), str)
            and bool(conclusion["conclusion"].strip())
            and isinstance(paths, list)
            and bool(paths)
            and all(
                isinstance(path, str) and path in source_review["allowed_source_paths"]
                for path in paths
            ),
            "Source review did not accept every selected requirement",
        )
    checks, commands = source_review["harness_checks"], source_review["commands"]
    require(len(checks) == len(commands), "Harness check evidence is incomplete")
    for check, command in zip(checks, commands):
        require(
            check.get("candidate") == candidate
            and check.get("argv") == command
            and check.get("returncode") == 0,
            "Harness checks do not accept the reviewed candidate",
        )
    return {"source_review_assessment": assessment}


def native_records(session_id, sessions_root=None):
    UUID(session_id)
    root = sessions_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    paths = list(Path(root).glob(f"*/*/*/*{session_id}.jsonl"))
    require(len(paths) == 1, "Exact native session evidence is absent or ambiguous")
    path = paths[0]
    require(not path.is_symlink(), "Native evidence cannot be a symlink")
    data = path.read_bytes()
    require(data.endswith(b"\n"), "Native session evidence has an incomplete final line")
    records = [json.loads(line) for line in data.splitlines()]
    return records, sha256(data).hexdigest()


def verify_native_review(
    producer,
    reviewer,
    candidate,
    sessions_root=None,
    *,
    accepted_verdicts=("ACCEPT",),
    source_review=None,
):
    require(reviewer and reviewer != producer, "Distinct native reviewer session is required")
    parent, parent_hash = native_records(producer, sessions_root)
    native_reviewer = reviewer
    if reviewer.startswith("/"):
        root = (
            sessions_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
        )
        matches = []
        stamps = [
            r.get("timestamp")
            for r in parent
            if r.get("timestamp") and r.get("payload", {}).get("name") == "spawn_agent"
        ]
        days = {
            (datetime.fromisoformat(stamp) + timedelta(days=offset)).strftime("%Y/%m/%d")
            for stamp in stamps
            for offset in (-1, 0, 1)
        }
        paths = (
            (p for day in sorted(days) for p in (Path(root) / day).glob("*.jsonl"))
            if days
            else Path(root).glob("*/*/*/*.jsonl")
        )
        for path in paths:
            with path.open("rb") as stream:
                first = stream.readline()
            try:
                metadata = json.loads(first).get("payload", {})
            except ValueError:
                continue
            if (
                metadata.get("parent_thread_id") == producer
                and metadata.get("agent_path") == reviewer
            ):
                matches.append(metadata.get("id"))
        require(
            len(matches) == 1,
            "Native reviewer task path is absent or ambiguous within this producer",
        )
        native_reviewer = matches[0]
    child, child_hash = native_records(native_reviewer, sessions_root)
    meta = next((r["payload"] for r in child if r["type"] == "session_meta"), {})
    require(
        meta.get("id", meta.get("session_id")) == native_reviewer,
        "Native reviewer identity differs",
    )
    source = meta.get("source", {})
    source_text = json.dumps(source)
    require(
        producer in source_text and ("subagent" in source_text or "thread_spawn" in source_text),
        "Reviewer is not an observed native child of this producer",
    )
    spawns = []
    calls = {}
    for index, record in enumerate(parent):
        payload = record.get("payload", {})
        if (
            record["type"] == "response_item"
            and payload.get("type") == "function_call"
            and payload.get("name", "").endswith("spawn_agent")
        ):
            arguments = json.loads(payload.get("arguments", "{}"))
            calls[payload.get("call_id")] = (arguments, index)
        if (
            record["type"] == "response_item"
            and payload.get("type") == "function_call_output"
            and reviewer in str(payload.get("output", ""))
            and payload.get("call_id") in calls
        ):
            spawns.append(calls[payload["call_id"]])
    fresh = [
        row
        for row in spawns
        if row[0].get("fork_context") is False or row[0].get("fork_turns") == "none"
    ]
    require(
        len(fresh) == 1 and meta.get("git", {}).get("commit_hash") == candidate,
        "Fresh native reviewer context bound to this candidate is not proven",
    )
    # A copied prompt block does not prove behavior. Coordination instructions remain
    # agent-managed; acceptance depends on actual reviewer identity and result below.
    completed = [
        r["payload"]
        for r in child
        if r["type"] == "event_msg" and r["payload"].get("type") == "task_complete"
    ]
    require(completed and not completed[-1].get("error"), "Reviewer has not completed successfully")
    messages = []
    for record in child:
        payload = record.get("payload", {})
        if record["type"] == "event_msg" and payload.get("type") == "agent_message":
            messages.append(payload.get("message", ""))
    if not messages and completed[-1].get("last_agent_message"):
        messages.append(completed[-1]["last_agent_message"])
    require(messages, "Native reviewer verdict is missing")
    text = messages[-1].strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    verdict = json.loads(text)
    require(
        verdict.get("candidate") == candidate
        and verdict.get("verdict") in accepted_verdicts
        and (
            verdict.get("unresolved_findings") == []
            if verdict.get("verdict") == "ACCEPT"
            else isinstance(verdict.get("unresolved_findings"), list)
            and bool(verdict["unresolved_findings"])
        ),
        "Native review did not accept the exact candidate",
    )
    source_evidence = {}
    if source_review is not None:
        source_evidence = _validate_source_review(verdict, candidate, source_review)
    # Parent-provided verdicts never substitute for the independently recorded child result.
    return {
        **verdict,
        "reviewer_session": native_reviewer,
        "reviewer_task": reviewer,
        "producer_session": producer,
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": child_hash,
        "parent_evidence_sha256": parent_hash,
        **source_evidence,
    }


def child_usage(producer, start, end, sessions_root=None):
    """Return observed completed native-child output totals for one invocation window."""
    root = sessions_root or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    days = {(first + timedelta(days=offset)).strftime("%Y/%m/%d") for offset in (-1, 0, 1)}
    days.add(last.strftime("%Y/%m/%d"))
    total = 0
    identities = []
    for day in sorted(days):
        for path in (Path(root) / day).glob("*.jsonl"):
            with path.open("rb") as stream:
                line = stream.readline()
            try:
                meta = json.loads(line).get("payload", {})
                timestamp = datetime.fromisoformat(meta.get("timestamp", ""))
            except ValueError:
                continue
            if meta.get("parent_thread_id") != producer or not first <= timestamp <= last:
                continue
            records, _ = native_records(meta["id"], root)
            outcomes = [
                r["payload"]
                for r in records
                if r["type"] == "event_msg" and r["payload"].get("type") == "task_complete"
            ]
            counts = [
                r["payload"]["info"]["total_token_usage"].get("output_tokens")
                for r in records
                if r["type"] == "event_msg"
                and r["payload"].get("type") == "token_count"
                and r["payload"].get("info")
            ]
            if (
                not outcomes
                or outcomes[-1].get("error")
                or not counts
                or type(counts[-1]) is not int
            ):
                return None, identities
            total += counts[-1]
            identities.append(meta["id"])
    return total, identities
