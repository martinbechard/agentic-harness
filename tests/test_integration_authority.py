"""Authority boundaries with real Git candidates and explicit native verifier fixtures."""

import json
from types import SimpleNamespace

import pytest

from backlog_harness import integration_authority as authority
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked, git


@pytest.fixture
def case(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "a").write_text("base")
    git(repo, "add", "a")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    candidate_repo = tmp_path / "candidate"
    git(repo, "clone", str(repo), str(candidate_repo))
    git(candidate_repo, "config", "user.name", "Fixture")
    git(candidate_repo, "config", "user.email", "fixture@example.invalid")
    (candidate_repo / "a").write_text("candidate")
    git(candidate_repo, "commit", "-am", "candidate")
    candidate = git(candidate_repo, "rev-parse", "HEAD")
    root = tmp_path / "evidence"
    workflow = {"allowed_paths": ["a"], "checks": [["check"]]}
    session = {"session_id": "owner", "native_session_id": "native-owner"}
    stage = lambda item, name: root / "stages" / (name + ".json")
    acceptance = {
        "role": "orchestrator",
        "session": session,
        "binding": {},
        "text": json.dumps({"item_id": "item", "accepted": True}),
    }
    produced = {
        "role": "orchestrator",
        "session": session,
        "binding": {},
        "text": json.dumps(
            {
                "item_id": "item",
                "candidate": candidate,
                "request_completion": True,
                "reviewer_session": "reviewer",
            }
        ),
    }
    checks = [
        {
            "candidate": candidate,
            "argv": ["check"],
            "returncode": 0,
            "evidence_sha256": "check-evidence",
        }
    ]
    for name, value in {
        "accept": acceptance,
        "produce-review": produced,
        "assignment": {"workflow": workflow},
        "base": {"commit": base},
        "source-checks": checks,
    }.items():
        atomic_json(stage("item", name), value)
    review = {
        "candidate": candidate,
        "verdict": "ACCEPT",
        "unresolved_findings": [],
        "reviewer_session": "reviewer",
        "producer_session": "native-owner",
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": "native",
    }
    monkeypatch.setattr(authority, "verify_native_review", lambda *args: review)
    app = SimpleNamespace(
        root=root,
        provider=SimpleNamespace(item=lambda item: SimpleNamespace(owner="owner")),
        config=SimpleNamespace(repository=repo),
        _stage_path=stage,
        validate_invocation_result=lambda result: None,
        result_json=lambda result: json.loads(result["text"]),
        item_workflow=lambda item: workflow,
        recovery_record=lambda item: None,
        candidate_repository=lambda item: candidate_repo,
        native_sessions_root=lambda binding: root,
    )
    instruction = {
        "item_id": "item",
        "original_candidate": candidate,
        "primary": base,
        "source_reference": "existing task",
        "authorization": "finish integration",
        "prospective_estimate": {"remaining_high": 100},
    }
    return app, instruction, review


def test_exact_original_authority_and_plain_check_contract(case):
    app, instruction, _ = case
    context = authority.validate_integration_instruction(app, "item", instruction)
    assert context["workspace"] == app.candidate_repository(
        "item"
    ).parent / ".integration-workspaces" / component("item")
    assert context["allowed_paths"] == ["a"] and context["generators"] == []
    proof = authority.verify_integration_proof(
        app, "item", {"original_candidate": instruction["original_candidate"]}, instruction
    )
    assert proof["disposition"] == "not-required"
    assert proof["assignment_digest"] == digest(
        json.loads(app._stage_path("item", "assignment").read_text())
    )


@pytest.mark.parametrize(
    "fault", ["estimate", "generators", "candidate", "review", "checks", "ambiguous"]
)
def test_changed_or_incomplete_source_authority_rejected(case, fault):
    app, instruction, review = case
    if fault == "estimate":
        instruction["prospective_estimate"]["remaining_high"] = True
    elif fault == "generators":
        instruction["generators"] = [["arbitrary-command"]]
    elif fault == "candidate":
        instruction["original_candidate"] = instruction["primary"]
    elif fault == "review":
        review["verdict"] = "REJECT"
    elif fault == "checks":
        path = app._stage_path("item", "source-checks")
        value = json.loads(path.read_text())
        value[0]["argv"] = ["other"]
        atomic_json(path, value)
    else:
        value = json.loads(app._stage_path("item", "produce-review").read_text())
        value["invocation_id"] = "different"
        atomic_json(app._stage_path("item", "another-result"), value)
    with pytest.raises(TransitionBlocked):
        authority.validate_integration_instruction(app, "item", instruction)


@pytest.mark.parametrize("trace", ["recovery", "proof-continuation", "proof-review"])
def test_missing_proof_request_cannot_erase_other_proof_requirements(case, trace):
    app, instruction, _ = case
    if trace == "recovery":
        app.recovery_record = lambda item: {"packet": {"remaining_work": "proof required"}}
    else:
        atomic_json(app._stage_path("item", trace), {"retained": True})
    with pytest.raises(TransitionBlocked, match="ambiguous"):
        authority.verify_integration_proof(
            app, "item", {"original_candidate": instruction["original_candidate"]}, instruction
        )


def test_tree_dependency_identity_includes_mode_and_all_paths(case):
    app, instruction, _ = case
    repo = app.candidate_repository("item")
    original = authority._tree_inputs(repo, instruction["original_candidate"])
    assert set(original) == {"a"}
    (repo / "a").chmod(0o755)
    git(repo, "commit", "-am", "mode")
    assert authority._tree_inputs(repo, "HEAD") != original


@pytest.mark.parametrize("fault", [None, "absent", "subset", "changed"])
def test_auxiliary_proof_requires_bound_complete_unchanged_tree(case, monkeypatch, fault):
    from backlog_harness import recovery_flow

    app, instruction, review = case
    candidate = instruction["original_candidate"]
    proof_result = {"original": "verified-proof"}
    review["proof_result_digest"] = digest(proof_result)
    atomic_json(app._stage_path("item", "artifact-proof-request"), {"retained": True})
    atomic_json(app._stage_path("item", "proof-review"), {"reviewer_session": "reviewer"})
    declaration = {
        "scope": "all-tracked-files",
        "complete": True,
        "inputs": authority._tree_inputs(app.candidate_repository("item"), candidate),
    }
    if fault == "subset":
        declaration["inputs"] = {}
    artifact = app.root / "bound-manifest.json"
    atomic_json(artifact, {} if fault == "absent" else {"proof_dependencies": declaration})
    monkeypatch.setattr(
        recovery_flow,
        "validated_auxiliary_proof",
        lambda *args: (
            {"candidate": candidate},
            proof_result,
            {"artifacts": [{"path": str(artifact)}]},
        ),
    )
    repo = app.candidate_repository("item")
    merged = candidate
    if fault == "changed":
        (repo / "other").write_text("new dependency")
        git(repo, "add", "other")
        git(repo, "commit", "-m", "advance")
        merged = git(repo, "rev-parse", "HEAD")
    record = {"original_candidate": candidate, "candidate": merged, "workspace": str(repo)}
    if fault:
        with pytest.raises(TransitionBlocked):
            authority.verify_integration_proof(app, "item", record, instruction)
    else:
        assert (
            authority.verify_integration_proof(app, "item", record, instruction)["disposition"]
            == "reused"
        )
