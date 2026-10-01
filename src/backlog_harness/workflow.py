"""Evidence gates for the selected file/main-branch workflow, not an agent task graph."""

from .provider import TransitionBlocked, git


def require(condition, reason):
    if not condition:
        raise TransitionBlocked(reason)


def validate_transition(item, target, authority):
    actor = authority.get("role")
    require(
        authority.get("invocation_id") and authority.get("observed_result"),
        "Observed actor request is required",
    )
    require(authority.get("item_id") == item.item_id, "Authority names another item")
    if target == "Ready" and item.state in {"Running", "Ready"}:
        recovery = authority.get("recovery", {})
        require(
            actor == "coordinator"
            and authority.get("operation") == "redispatch"
            and recovery.get("previous_owner") == item.owner
            and recovery.get("ownership_ended") is True
            and recovery.get("packet_digest")
            and (recovery.get("candidate") or recovery.get("no_source_changes") is True)
            and recovery.get("runtime_evidence")
            and recovery.get("reason"),
            "Recovery requires Coordinator authority and preserved ownership/candidate evidence",
        )
        require(
            item.state == "Running" or recovery.get("repair_effect"),
            "Same-state recovery requires a verified incomplete effect",
        )
    elif (item.state, target) == ("Ready", "Starting"):
        require(
            actor == "coordinator" and authority.get("operation") == "new",
            "Only a Coordinator new decision authorizes reservation",
        )
    elif (item.state, target) == ("Starting", "Running"):
        require(
            actor == "orchestrator"
            and authority.get("accepted") is True
            and authority.get("session_id")
            and authority.get("native_session_id"),
            "Canonical Orchestrator acceptance is required",
        )
    elif target == "Holding" and item.state in {"Starting", "Running"}:
        require(
            actor == "coordinator"
            and authority.get("incident") in {"usage_unknown", "usage_limit"},
            "Holding requires Coordinator guard authority",
        )
    elif (item.state, target) == ("Running", "User Action Required"):
        require(
            (actor == "orchestrator" and authority.get("session_id") == item.owner)
            or (
                actor == "coordinator"
                and authority.get("operation") == "await-user"
                and authority.get("stopped_owner") == item.owner
                and authority.get("outcome_evidence")
            ),
            "A question requires canonical ownership or Coordinator reconciliation of its stopped result",
        )
        question = authority.get("question", {})
        require(
            question.get("question_id") and question.get("text"),
            "Exact question evidence is required",
        )
    elif item.state == "User Action Required" and target in {"User Action Required", "Running"}:
        require(
            authority.get("question", {}).get("question_id")
            and authority.get("answer", {}).get("digest"),
            "Exact persisted question and answer are required",
        )
        if target == "Running":
            require(
                actor == "orchestrator"
                and authority.get("session_id") == item.owner
                and authority.get("disposition") == "approve",
                "Only canonical approval permits question continuation",
            )
        else:
            require(actor in {"operator", "orchestrator"}, "Unauthorized answer actor")
    elif item.state == "Holding" and target in {"Starting", "Running"}:
        require(
            actor == "coordinator"
            and authority.get("operation") == "resume"
            and authority.get("retained_owner") == item.owner,
            "Hold release requires Coordinator approval for the retained owner",
        )
        usage = authority.get("usage", {})
        require(
            usage.get("status") == "below"
            and usage.get("may_generate") is True
            and type(usage.get("generated_tokens")) is int
            and usage["generated_tokens"] < usage.get("ceiling", 0),
            "Unknown or crossed usage cannot release a hold",
        )
    elif (item.state, target) == ("Running", "Completed"):
        require(
            actor == "orchestrator" and authority.get("session_id") == item.owner,
            "Only the canonical Orchestrator may request completion",
        )
        delivery = authority.get("delivery", {})
        require(
            delivery.get("disposition") == "READY" and delivery.get("verified") is True,
            "Verified Commit READY and provider handoff are required",
        )
        require(
            delivery.get("candidate")
            and delivery.get("review")
            and delivery.get("checks")
            and delivery.get("integrated_checks")
            and delivery.get("main_commit"),
            "Completion evidence is incomplete",
        )
    else:
        raise TransitionBlocked(f"Unsupported transition {item.state} -> {target}")


def validate_candidate(
    repository, candidate, base, allowed_paths, producer, review, checks, *, preserved=False
):
    require(git(repository, "rev-parse", "HEAD") == candidate, "Candidate HEAD changed")
    require(not git(repository, "status", "--porcelain"), "Candidate is dirty")
    if preserved:
        require(git(repository, "merge-base", base, candidate) == base, "Candidate base changed")
    else:
        require(git(repository, "rev-parse", candidate + "^") == base, "Candidate base changed")
    changed = git(repository, "diff", "--name-only", base, candidate).splitlines()
    require(
        changed and set(changed) <= set(allowed_paths), "Candidate changes exceed accepted scope"
    )
    require(review is not None, "Independent review is missing")
    require(
        review.get("candidate") == candidate and review.get("verdict") == "ACCEPT",
        "Review is stale, rejects, or names another candidate",
    )
    require(
        review.get("reviewer_session") and review["reviewer_session"] != producer,
        "Reviewer must be distinct from the producer",
    )
    require(
        review.get("native_verified") is True and review.get("fresh_context") is True,
        "Native evidence of fresh reviewer context is required",
    )
    require(
        review.get("evidence_sha256") and review.get("unresolved_findings") == [],
        "Review evidence or resolved findings are missing",
    )
    require(
        checks
        and all(
            c.get("candidate") == candidate
            and c.get("returncode") == 0
            and c.get("argv")
            and c.get("evidence_sha256")
            for c in checks
        ),
        "Passing checks bound to the exact candidate are required",
    )
    return changed
