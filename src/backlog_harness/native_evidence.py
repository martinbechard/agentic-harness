"""Read native session evidence to verify a review arranged by its producing agent."""

from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from .contracts import digest
from .workflow import require

SOURCE_REVIEW_PACKET_MARKER = "SOURCE REVIEW PACKET\n"
COORDINATION_CONTEXT_MARKER = "COORDINATION CONTEXT\n"
COORDINATION_CONTEXT_END = "\nEND COORDINATION CONTEXT"
_COORDINATION_UNSPECIFIED = object()


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
        + " Copy the following exact block into every fresh native delegate or reviewer prompt; "
        "the block is authority context, not permission to widen source or lifecycle scope.\n"
        + COORDINATION_CONTEXT_MARKER
        + json.dumps(context, sort_keys=True, separators=(",", ":"))
        + COORDINATION_CONTEXT_END
    )


def _validate_coordination_prompt(arguments, expected):
    message = arguments.get("message", arguments.get("prompt"))
    require(isinstance(message, str), "Native reviewer prompt is missing")
    require(
        message.count(COORDINATION_CONTEXT_MARKER) == 1,
        "Native reviewer coordination context is missing or ambiguous",
    )
    tail = message.split(COORDINATION_CONTEXT_MARKER, 1)[1]
    try:
        observed, end = json.JSONDecoder().raw_decode(tail)
    except ValueError as exc:
        require(False, "Native reviewer coordination context is invalid: " + str(exc))
    require(
        tail[end:].startswith(COORDINATION_CONTEXT_END) and observed == expected,
        "Native reviewer coordination context differs",
    )


def candidate_source_evidence(repository, candidate, paths):
    """Hash the explicitly reviewed source set from the exact candidate tree."""
    from .provider import blob

    return [
        {"path": path, "sha256": sha256(blob(repository, candidate, path)).hexdigest()}
        for path in paths
    ]


def source_review_instructions(workflow, canonical_acceptance, workdir):
    """Describe the selected assessment without adding a second invocation phase."""
    contract = workflow.get("review_requirements")
    if not contract:
        return ""
    commands = [list(argv) for argv in workflow["checks"]]
    command_text = [shlex.join(argv) for argv in commands]
    return (
        "\nThis item has an explicit source-review requirement. Use the existing one fresh native "
        "reviewer; do not add a proof, browser, design, or second model phase. After committing, "
        "use direct exec_command calls in this candidate worktree. First run exactly `git rev-parse "
        "HEAD`, then run each configured command below separately and in order with cmd exactly as "
        "shown with yield_time_ms 30000, then run exactly `git rev-parse HEAD` again. Do not combine "
        "commands. Every check "
        "must finish with code 0 before spawning the reviewer. Preserve each complete native tool "
        "output string and its SHA-256. Build one SOURCE REVIEW PACKET version 1 containing the exact "
        "candidate, canonical_acceptance, preparation_evidence, review_requirements, canonical compact-"
        "JSON SHA-256 requirements_digest, candidate-tree SHA-256 for every allowed source path, and "
        "pre_review_checks. Each check receipt has candidate, argv, returncode, the complete native "
        "output string, and output_sha256; check_receipt_hashes are canonical compact-JSON SHA-256 "
        "values of those receipts in command order. Put instructions for the reviewer first and end "
        "the spawn message with the exact marker `SOURCE REVIEW PACKET\\n` followed by the packet JSON. "
        "The reviewer must inspect the packet and exact candidate, then return the ordinary candidate, "
        "verdict and unresolved_findings fields plus source_review_assessment with candidate, "
        "requirements_digest, ordered check_receipt_hashes, unresolved_findings, and one ordered "
        "conclusion per selected requirement. Each conclusion has exactly id, canonical_reference, "
        "verdict, nonempty conclusion, and nonempty evidence_paths drawn from source_evidence. A concern "
        "about evaluation weakening must be REJECT or an unresolved finding even when checks pass.\n"
        + json.dumps(
            {
                "candidate_worktree": str(Path(workdir).resolve()),
                "configured_check_commands": command_text,
                "configured_checks": commands,
                "canonical_acceptance": canonical_acceptance,
                "preparation_evidence": workflow.get("preparation_evidence"),
                "review_requirements": contract,
                "allowed_source_paths": workflow["allowed_paths"],
            },
            sort_keys=True,
        )
    )


def source_review_context(repository, candidate, workflow, canonical_acceptance, harness_checks):
    """Build the exact post-return expectations for the pre-spawn review packet."""
    return {
        "workdir": str(Path(repository).resolve()),
        "commands": [list(argv) for argv in workflow["checks"]],
        "canonical_acceptance": canonical_acceptance,
        "preparation_evidence": workflow.get("preparation_evidence"),
        "review_requirements": workflow["review_requirements"],
        "source_evidence": candidate_source_evidence(
            repository, candidate, workflow["allowed_paths"]
        ),
        "harness_checks": harness_checks,
    }


def _call_output_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(row.get("text", "") for row in value if isinstance(row, dict))
    return ""


def _source_review_packet(arguments):
    message = arguments.get("message", arguments.get("prompt"))
    require(isinstance(message, str), "Source reviewer prompt is missing")
    require(
        SOURCE_REVIEW_PACKET_MARKER in message,
        "Source reviewer did not receive the bound review packet",
    )
    text = message.rsplit(SOURCE_REVIEW_PACKET_MARKER, 1)[1].lstrip()
    try:
        packet, end = json.JSONDecoder().raw_decode(text)
    except ValueError as exc:
        require(False, "Source review packet is invalid: " + str(exc))
    require(
        isinstance(packet, dict) and not text[end:].strip(),
        "Source review packet must be the final complete object",
    )
    return packet


def _native_pre_review_checks(records, spawn_index, candidate, source_review):
    """Bind packet receipts to completed native command records before reviewer spawn."""
    workdir = str(Path(source_review["workdir"]).resolve())
    commands = [list(argv) for argv in source_review["commands"]]
    calls, outputs = [], {}
    for index, record in enumerate(records):
        payload = record.get("payload", {})
        if record.get("type") != "response_item":
            continue
        if payload.get("type") == "function_call" and payload.get("name") == "exec_command":
            try:
                arguments = json.loads(payload.get("arguments", "{}"))
            except ValueError:
                continue
            calls.append((index, payload.get("call_id"), arguments))
        elif payload.get("type") == "function_call_output":
            outputs[payload.get("call_id")] = (
                index,
                payload.get("id"),
                _call_output_text(payload.get("output")),
            )

    def completed(call):
        index, call_id, arguments = call
        output = outputs.get(call_id)
        return (
            output
            if output is not None
            and isinstance(call_id, str)
            and bool(call_id)
            and isinstance(output[1], str)
            and bool(output[1])
            and index < output[0] < spawn_index
            and isinstance(arguments.get("workdir"), str)
            and Path(arguments.get("workdir", "")).resolve() == Path(workdir)
            else None
        )

    anchor_command = shlex.join(["git", "rev-parse", "HEAD"])
    anchors = []
    for call in calls:
        output = completed(call)
        if output and call[2].get("cmd") == anchor_command:
            lines = output[2].splitlines()
            if any(line.strip() == candidate for line in lines):
                anchors.append((call[0], output[0]))

    packet_receipts = source_review["packet"].get("pre_review_checks")
    require(
        isinstance(packet_receipts, list) and len(packet_receipts) == len(commands),
        "Source review packet check receipts are incomplete",
    )
    observed, previous = [], -1
    for command, packet in zip(commands, packet_receipts):
        expected_cmd = shlex.join(command)
        matches = []
        for call in calls:
            output = completed(call)
            if output and previous < call[0] < spawn_index and call[2].get("cmd") == expected_cmd:
                code = re.search(r"Process exited with code (-?\d+)", output[2])
                if code:
                    receipt = {
                        "candidate": candidate,
                        "argv": command,
                        "returncode": int(code.group(1)),
                        "output": output[2],
                        "output_sha256": sha256(output[2].encode()).hexdigest(),
                    }
                    if receipt == packet:
                        matches.append((call, output, receipt))
        require(len(matches) == 1, "Pre-review check receipt lacks exact native provenance")
        call, output, receipt = matches[0]
        require(receipt["returncode"] == 0, "Pre-review configured check failed")
        require(
            "truncated output" not in receipt["output"].lower(),
            "Pre-review configured check output is incomplete",
        )
        observed.append(
            {
                **receipt,
                "native_call_id": call[1],
                "native_output_id": output[1],
            }
        )
        previous = output[0]
    first_call = next(call[0] for call in calls if call[1] == observed[0]["native_call_id"])
    last_output = outputs[observed[-1]["native_call_id"]][0]
    require(
        any(end < first_call for _, end in anchors)
        and any(start > last_output and end < spawn_index for start, end in anchors),
        "Pre-review checks are not bound to the exact candidate",
    )
    return observed


def _validate_source_review(verdict, packet, candidate, source_review, observed):
    contract = source_review["review_requirements"]
    expected_receipts = [
        {key: row[key] for key in ("candidate", "argv", "returncode", "output", "output_sha256")}
        for row in observed
    ]
    receipt_hashes = [digest(row) for row in expected_receipts]
    expected_packet = {
        "version": 1,
        "candidate": candidate,
        "canonical_acceptance": source_review["canonical_acceptance"],
        "preparation_evidence": source_review["preparation_evidence"],
        "review_requirements": contract,
        "requirements_digest": digest(contract),
        "source_evidence": source_review["source_evidence"],
        "pre_review_checks": expected_receipts,
        "check_receipt_hashes": receipt_hashes,
    }
    require(packet == expected_packet, "Source review packet binding differs")
    assessment = verdict.get("source_review_assessment")
    require(
        isinstance(assessment, dict)
        and set(assessment)
        == {
            "candidate",
            "requirements_digest",
            "check_receipt_hashes",
            "unresolved_findings",
            "conclusions",
        }
        and assessment.get("candidate") == candidate
        and assessment.get("requirements_digest") == digest(contract)
        and assessment.get("check_receipt_hashes") == receipt_hashes
        and assessment.get("unresolved_findings") == [],
        "Source review assessment is missing, stale or unresolved",
    )
    conclusions = assessment.get("conclusions")
    requirements = contract["requirements"]
    require(
        isinstance(conclusions, list) and len(conclusions) == len(requirements),
        "Source review requirement conclusions are incomplete",
    )
    evidence_paths = {row["path"] for row in source_review["source_evidence"]}
    for requirement, conclusion in zip(requirements, conclusions):
        require(
            isinstance(conclusion, dict)
            and set(conclusion)
            == {"id", "canonical_reference", "verdict", "conclusion", "evidence_paths"}
            and conclusion.get("id") == requirement["id"]
            and conclusion.get("canonical_reference") == requirement["canonical_reference"]
            and conclusion.get("verdict") == "ACCEPT"
            and isinstance(conclusion.get("conclusion"), str)
            and bool(conclusion["conclusion"].strip())
            and isinstance(conclusion.get("evidence_paths"), list)
            and bool(conclusion["evidence_paths"])
            and len(conclusion["evidence_paths"]) == len(set(conclusion["evidence_paths"]))
            and set(conclusion["evidence_paths"]) <= evidence_paths,
            "Source review did not accept every selected requirement",
        )
    require(
        len(source_review["harness_checks"]) == len(source_review["commands"]),
        "Harness check corroboration is incomplete",
    )
    for check, command in zip(source_review["harness_checks"], source_review["commands"]):
        require(
            check.get("candidate") == candidate
            and check.get("argv") == command
            and check.get("returncode") == 0,
            "Harness checks do not corroborate the pre-review executions",
        )
    return {
        "source_review_packet_digest": digest(packet),
        "pre_review_checks": observed,
        "source_review_assessment": assessment,
    }


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
    coordination_context=_COORDINATION_UNSPECIFIED,
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
    if coordination_context is not _COORDINATION_UNSPECIFIED and coordination_context is not None:
        _validate_coordination_prompt(fresh[0][0], coordination_context)
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
        packet = _source_review_packet(fresh[0][0])
        bound = {**source_review, "packet": packet}
        observed = _native_pre_review_checks(parent, fresh[0][1], candidate, bound)
        source_evidence = _validate_source_review(verdict, packet, candidate, bound, observed)
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
