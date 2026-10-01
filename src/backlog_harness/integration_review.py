"""Verify a fresh harness-launched review without weakening agent-child review rules."""

import json
from pathlib import Path

from .contracts import digest
from .native_evidence import native_records
from .workflow import require


def verify_integration_review(
    producer, reviewer, record, result_value, sessions_root, *, proof_context=None
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
    messages = [e.get("message", "") for e in events if e.get("type") == "user_message"]
    marker = "Integration review identity: " + digest(record)
    require(len(messages) == 1 and marker in messages[0], "Integration review prompt is unbound")
    if proof_context is not None:
        require(
            "Proof applicability identity: " + digest(proof_context) in messages[0],
            "Integration proof applicability prompt is unbound",
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
        and verdict.get("evidence"),
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
