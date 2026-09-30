"""Read native session evidence to verify a review arranged by its producing agent."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from .workflow import require


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


def verify_native_review(producer, reviewer, candidate, sessions_root=None):
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
    for record in parent:
        payload = record.get("payload", {})
        if (
            record["type"] == "response_item"
            and payload.get("type") == "function_call"
            and payload.get("name", "").endswith("spawn_agent")
        ):
            arguments = json.loads(payload.get("arguments", "{}"))
            calls[payload.get("call_id")] = arguments
        if (
            record["type"] == "response_item"
            and payload.get("type") == "function_call_output"
            and reviewer in str(payload.get("output", ""))
            and payload.get("call_id") in calls
        ):
            spawns.append(calls[payload["call_id"]])
    fresh = [s for s in spawns if s.get("fork_context") is False or s.get("fork_turns") == "none"]
    require(
        len(fresh) == 1 and meta.get("git", {}).get("commit_hash") == candidate,
        "Fresh native reviewer context bound to this candidate is not proven",
    )
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
        and verdict.get("verdict") == "ACCEPT"
        and verdict.get("unresolved_findings") == [],
        "Native review did not accept the exact candidate",
    )
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
