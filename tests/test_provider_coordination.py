import pytest

from backlog_harness.provider import TransitionBlocked, git
from backlog_harness.workflow import validate_candidate, validate_transition


def authority(role, **values):
    return {
        "role": role,
        "invocation_id": "observed-invocation",
        "observed_result": True,
        "item_id": "item-one",
        **values,
    }


def test_valid_admission_and_reject_missing_actor_stale_revision(provider):
    item = provider.item("item-one")
    head = git(provider.repository, "rev-parse", "HEAD")
    with pytest.raises(TransitionBlocked):
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("orchestrator", operation="new"),
            validate=validate_transition,
        )
    assert git(provider.repository, "rev-parse", "HEAD") == head
    receipt = provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    assert receipt["state"] == "Starting"
    assert (
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("coordinator", operation="new"),
            validate=validate_transition,
        )
        == receipt
    )
    with pytest.raises(TransitionBlocked, match="Stale"):
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("coordinator", operation="new", invocation_id="another-attempt"),
            validate=validate_transition,
        )
    current = provider.item(item.item_id)
    with pytest.raises(TransitionBlocked, match="acceptance"):
        provider.transition(
            item.item_id,
            current.revision,
            "Running",
            authority("orchestrator"),
            validate=validate_transition,
        )
    provider.transition(
        item.item_id,
        current.revision,
        "Running",
        authority(
            "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
        ),
        validate=validate_transition,
    )
    assert provider.item(item.item_id).owner == "canonical"
    current = provider.item(item.item_id)
    with pytest.raises(TransitionBlocked, match="Commit READY"):
        provider.transition(
            item.item_id,
            current.revision,
            "Completed",
            authority("orchestrator", session_id="canonical"),
            validate=validate_transition,
        )
    assert provider.item(item.item_id).state == "Running"


def test_real_claim_policy_is_not_bypassed(provider):
    p = provider.repository / "PROJECT.yaml"
    p.write_text(p.read_text().replace("selected: none", "selected: resource-claim"))
    with pytest.raises(TransitionBlocked, match="resource coordination none"):
        provider.policy()


def test_candidate_bound_independent_review(provider):
    repo = provider.repository
    base = git(repo, "rev-parse", "HEAD")
    (repo / "answer.txt").write_text("42\n")
    git(repo, "add", "--", "answer.txt")
    git(repo, "commit", "-m", "Candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    review = {
        "candidate": candidate,
        "verdict": "ACCEPT",
        "reviewer_session": "reviewer",
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": "review-hash",
        "unresolved_findings": [],
    }
    checks = [
        {"candidate": candidate, "argv": ["test"], "returncode": 0, "evidence_sha256": "check-hash"}
    ]

    def check(r=review, c=checks):
        return validate_candidate(repo, candidate, base, ["answer.txt"], "producer", r, c)

    assert check() == ["answer.txt"]
    for bad in [
        None,
        {**review, "candidate": base},
        {**review, "reviewer_session": "producer"},
        {**review, "fresh_context": False},
        {**review, "verdict": "REJECT"},
        {**review, "unresolved_findings": ["bug"]},
    ]:
        with pytest.raises(TransitionBlocked):
            check(bad)
    with pytest.raises(TransitionBlocked):
        check(c=[{**checks[0], "candidate": base}])
    (repo / "answer.txt").write_text("unreviewed\n")
    with pytest.raises(TransitionBlocked, match="dirty"):
        check()


def test_hold_preserves_canonical_owner_and_unknown_cannot_release(provider):
    item = provider.item("item-one")
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    provider.transition(
        item.item_id,
        item.revision,
        "Running",
        authority(
            "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
        ),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    provider.transition(
        item.item_id,
        item.revision,
        "Holding",
        authority("coordinator", session_id="coordinator", incident="usage_limit"),
        validate=validate_transition,
    )
    held = provider.item(item.item_id)
    assert held.owner == "canonical"
    actor = authority(
        "coordinator",
        operation="resume",
        session_id="coordinator",
        retained_owner="canonical",
        usage={"status": "unknown", "may_generate": False},
    )
    with pytest.raises(TransitionBlocked, match="Unknown"):
        provider.transition(
            held.item_id, held.revision, "Running", actor, validate=validate_transition
        )
    actor["usage"] = {
        "status": "below",
        "generated_tokens": 201,
        "ceiling": 300,
        "may_generate": True,
    }
    provider.transition(held.item_id, held.revision, "Running", actor, validate=validate_transition)
    assert provider.item(item.item_id).owner == "canonical"


def test_question_gate_requires_canonical_owner_and_exact_approval(provider):
    item = provider.item("item-one")
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    owner = authority(
        "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
    )
    provider.transition(item.item_id, item.revision, "Running", owner, validate=validate_transition)
    item = provider.item(item.item_id)
    question = {"question_id": "q1", "text": "Approve?"}
    with pytest.raises(TransitionBlocked, match="canonical"):
        provider.transition(
            item.item_id,
            item.revision,
            "User Action Required",
            {**owner, "session_id": "another", "question": question},
            validate=validate_transition,
        )
    provider.transition(
        item.item_id,
        item.revision,
        "User Action Required",
        {**owner, "question": question},
        validate=validate_transition,
    )
    waiting = provider.item(item.item_id)
    assert provider.question(waiting)["text"] == "Approve?"
    answer = {"digest": "answer-hash"}
    for disposition in ["defer", "decline", "ambiguous", None]:
        with pytest.raises(TransitionBlocked, match="approval"):
            provider.transition(
                item.item_id,
                waiting.revision,
                "Running",
                {**owner, "question": question, "answer": answer, "disposition": disposition},
                validate=validate_transition,
            )
    provider.transition(
        item.item_id,
        waiting.revision,
        "Running",
        {**owner, "question": question, "answer": answer, "disposition": "approve"},
        validate=validate_transition,
    )
    assert provider.item(item.item_id).owner == "canonical"
