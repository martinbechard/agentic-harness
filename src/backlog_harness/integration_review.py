"""Verify a fresh harness-launched review without weakening agent-child review rules."""

import json
import re
from pathlib import Path

from .contracts import digest
from .native_evidence import native_records
from .workflow import require


def verify_integration_review(
    producer,
    reviewer,
    record,
    result_value,
    sessions_root,
    *,
    proof_context=None,
    native_binding=None,
):
    require(reviewer != producer, "Integration reviewer must be independent")
    records, evidence_hash = native_records(reviewer, sessions_root)
    metadata = [r["payload"] for r in records if r.get("type") == "session_meta"]
    require(len(metadata) == 1, "Integration reviewer metadata is ambiguous")
    meta = metadata[0]
    require(
        meta.get("id") == reviewer
        and meta.get("source") == "exec"
        and not meta.get("parent_thread_id")
        and not meta.get("forked_from_id")
        and meta.get("git", {}).get("commit_hash") == record["candidate"]
        and Path(meta.get("cwd", "")).resolve() == Path(record["workspace"]).resolve(),
        "Fresh integration reviewer candidate context is not proven",
    )
    events = [r["payload"] for r in records if r.get("type") == "event_msg"]
    starts = [e for e in events if e.get("type") == "task_started"]
    ends = [e for e in events if e.get("type") == "task_complete"]
    require(
        len(starts) == len(ends) == 1
        and starts[0].get("turn_id")
        and starts[0]["turn_id"] == ends[0].get("turn_id")
        and not ends[0].get("error"),
        "Integration review requires one successful native turn",
    )
    # CLI versions emit the submitted prompt as an event, a user response item,
    # or both. Injected AGENTS guidance is another user message, not another turn.
    messages = []
    active_turn = False
    for row in records:
        payload = row.get("payload", {})
        if row.get("type") == "event_msg" and payload.get("type") == "task_started":
            active_turn = True
        if row.get("type") == "turn_context":
            require(
                payload.get("turn_id") == starts[0]["turn_id"],
                "Integration review context belongs to another turn",
            )
        message = None
        if row.get("type") == "event_msg" and payload.get("type") == "user_message":
            message = payload.get("message", "")
        elif (
            row.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            message = "\n".join(
                part.get("text", "")
                for part in payload.get("content", [])
                if part.get("type") == "input_text" and isinstance(part.get("text"), str)
            )
        if message is not None and "[harness-invocation " in message:
            require(
                native_binding is not None
                and native_binding.get("native_session_id") == reviewer
                and native_binding.get("native_turn_id") == starts[0]["turn_id"]
                and native_binding.get("native_evidence_sha256") == evidence_hash,
                "Marked integration review requires exact retained native request evidence",
            )
        if message is not None and (
            "Integration review identity:" in message or "Proof applicability identity:" in message
        ):
            require(
                active_turn
                and payload.get("turn_id", starts[0]["turn_id"]) == starts[0]["turn_id"],
                "Integration review prompt belongs to another turn",
            )
            messages.append(message)
        if row.get("type") == "event_msg" and payload.get("type") == "task_complete":
            active_turn = False
    messages = list(dict.fromkeys(messages))
    require(len(messages) == 1, "Integration review prompt is unbound or ambiguous")
    prompt = messages[0]
    for label, expected in (
        ("Integration review identity", digest(record)),
        (
            "Proof applicability identity",
            digest(proof_context) if proof_context is not None else None,
        ),
    ):
        identities = re.findall(r"^" + label + r": ([^\r\n]+)$", prompt, re.MULTILINE)
        require(
            identities == ([expected] if expected is not None else []),
            "Integration review prompt identity is unbound or conflicting",
        )
    verdict = json.loads(ends[0].get("last_agent_message", "null"))
    require(
        isinstance(verdict, dict)
        and verdict == result_value
        and verdict.get("candidate") == record["candidate"]
        and verdict.get("verdict") in {"ACCEPT", "REJECT"}
        and isinstance(verdict.get("unresolved_findings"), list)
        and (
            verdict["unresolved_findings"] == []
            if verdict["verdict"] == "ACCEPT"
            else bool(verdict["unresolved_findings"])
        )
        and _supporting_evidence(verdict),
        "Integration review lacks exact verdict and supporting evidence",
    )
    return {
        **verdict,
        "producer_session": producer,
        "reviewer_session": reviewer,
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": evidence_hash,
        "provenance": "harness-launched-independent-integration-review",
    }


def _supporting_evidence(verdict):
    # Retain the exact native verdict. Older prompts allowed the descriptive alias.
    present = [verdict[key] for key in ("evidence", "supporting_evidence") if key in verdict]
    return bool(present) and all(
        (isinstance(value, str) and bool(value.strip()))
        or (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and bool(item.strip()) for item in value)
        )
        for value in present
    )


def validate_proof_applicability(proof, review):
    """Require a bound semantic decision; the independent agent judges applicability."""
    if proof.get("disposition") != "review-required":
        return proof
    context = proof["context"]
    decision = review.get("proof_applicability")
    require(
        proof.get("context_digest") == digest(context)
        and isinstance(decision, dict)
        and decision.get("context_digest") == digest(context)
        and decision.get("candidate") == context["candidate"]
        and decision.get("verdict") in {"ACCEPT", "FRESH_PROOF_REQUIRED"}
        and isinstance(decision.get("scope_assessment"), str)
        and bool(decision["scope_assessment"].strip()),
        "Proof applicability requires an exact candidate and reasoned scope decision",
    )
    for key, identity, expected, outcome in (
        ("dependency_assessments", "path", context["changed_dependencies"], "equivalent"),
        ("case_assessments", "case", context["cases"], "retained"),
    ):
        rows = decision.get(key)
        require(
            isinstance(rows, list)
            and all(isinstance(row, dict) and isinstance(row.get(identity), str) for row in rows)
            and len(rows) == len(expected)
            and {row.get(identity) for row in rows} == set(expected),
            "Proof applicability omits or duplicates required assessments",
        )
        require(
            all(
                type(row.get(outcome)) is bool
                and isinstance(row.get("reason"), str)
                and row["reason"].strip()
                and isinstance(row.get("evidence"), str)
                and row["evidence"].strip()
                for row in rows
            ),
            "Proof applicability assessments lack supporting evidence",
        )
        require(
            all(row[outcome] for row in rows),
            "Retained proof does not cover affected obligations; fresh verified proof required",
        )
    require(
        decision["verdict"] == "ACCEPT",
        "Retained proof applicability rejected; fresh verified proof required",
    )
    return {
        "disposition": "reviewed-applicability",
        "context": context,
        "context_digest": digest(context),
        "decision": decision,
        "reviewer_session": review["reviewer_session"],
        "review_evidence_sha256": review["evidence_sha256"],
    }
