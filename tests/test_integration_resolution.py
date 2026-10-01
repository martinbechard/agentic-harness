"""Semantic conflict effects in real isolated Git workspaces; no model invocation."""

import pytest

from backlog_harness.integration_reconciliation import finalize_resolution, prepare_candidate
from backlog_harness.provider import TransitionBlocked, git


@pytest.fixture
def conflict(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "a").write_text("base\n")
    (repo / "unrelated").write_text("preserve\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("candidate\n")
    git(repo, "commit", "-am", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach", base)
    (repo / "a").write_text("primary\n")
    git(repo, "commit", "-am", "primary")
    primary = git(repo, "rev-parse", "HEAD")
    work, evidence = tmp_path / "workspace", tmp_path / "evidence"
    args = {
        "primary": primary,
        "candidate": candidate,
        "base": base,
        "allowed_paths": ["a"],
        "allow_resolution": True,
    }
    record = prepare_candidate(repo, work, evidence, **args)
    return repo, work, evidence, args, record


def test_semantic_conflict_retains_parents_and_resumes_exact_effect(conflict):
    repo, work, evidence, args, record = conflict
    assert record["status"] == "resolution-required" and record["conflict_paths"] == ["a"]
    before = (work / "a").read_bytes()
    assert prepare_candidate(repo, work, evidence, **args) == record
    assert (work / "a").read_bytes() == before
    (work / "a").write_text("resolved meaning\n")
    git(work, "add", "a")
    result = finalize_resolution(repo, work, evidence, record, "verified-result-digest")
    assert git(repo, "rev-parse", "HEAD") == args["primary"]
    assert git(work, "rev-list", "--parents", "-n", "1", result["candidate"]).split()[1:] == [
        args["primary"],
        args["candidate"],
    ]
    assert (work / "unrelated").read_text() == "preserve\n"
    assert finalize_resolution(repo, work, evidence, record, "verified-result-digest") == result
    assert prepare_candidate(repo, work, evidence, **args) == result
    with pytest.raises(TransitionBlocked, match="result changed"):
        finalize_resolution(repo, work, evidence, record, "different-result")


@pytest.mark.parametrize(
    "fault", ["unresolved", "markers", "worktree", "index", "parent", "record"]
)
def test_resolution_rejects_unproven_or_outside_scope_changes(conflict, fault):
    repo, work, evidence, _args, record = conflict
    if fault != "unresolved":
        (work / "a").write_text("<<<<<<< retained\n" if fault == "markers" else "resolved\n")
        git(work, "add", "a")
    if fault == "worktree":
        (work / "unrelated").write_text("unapproved\n")
    elif fault == "index":
        (work / "unrelated").write_text("unapproved\n")
        git(work, "add", "unrelated")
        (work / "unrelated").write_text("preserve\n")
    elif fault == "parent":
        git(work, "config", "user.email", "fixture@example.invalid")
        git(work, "config", "user.name", "Fixture")
        git(work, "commit", "-m", "unauthorized commit")
    elif fault == "record":
        record = {**record, "allowed_paths": ["a", "unrelated"]}
    with pytest.raises(TransitionBlocked):
        finalize_resolution(repo, work, evidence, record, "verified-result")
    assert not (evidence / "candidate.json").exists()


@pytest.mark.parametrize("failure", [None, "blocked", "unbound", "accounting", "active"])
def test_flow_scoped_resolver_preserves_attempt_and_enforces_gates(conflict, failure):
    import asyncio
    import json
    from types import SimpleNamespace

    from backlog_harness.contracts import digest
    from backlog_harness.evidence import atomic_json
    from backlog_harness.integration_flow import resolve_conflict

    repo, work, evidence, _args, record = conflict
    calls = []
    stage_root = evidence.parent / "stages"

    class App:
        config = SimpleNamespace(repository=repo)

        def _stage_path(self, item, stage):
            return stage_root / (stage + ".json")

        def item_quiescent(self, item):
            return failure != "active" or not calls

        def validate_integration_stage(self, result, item):
            if failure == "accounting":
                raise TransitionBlocked("Unknown integration accounting")

        def result_json(self, result):
            return json.loads(result["text"])

        async def invoke(self, item, stage, role, prompt, **kwargs):
            saved = self._stage_path(item, stage)
            if saved.exists():
                return json.loads(saved.read_text())
            calls.append(stage)
            assert role == "orchestrator" and kwargs["read_only"] is False
            assert "session" not in kwargs
            request = kwargs["integration_resolution"]
            assert request["allowed_paths"] == ["a"]
            (work / "a").write_text("resolved\n")
            git(work, "add", "a")
            result = {
                "text": json.dumps(
                    {
                        "item_id": item,
                        "resolution": "blocked" if failure == "blocked" else "resolved",
                        "resolution_digest": "wrong" if failure == "unbound" else digest(request),
                        "evidence": "retained semantic resolution",
                    }
                )
            }
            atomic_json(saved, result)
            return result

    app = App()
    context = {
        "workspace": work,
        "authority": {"authorization": "existing task", "source_reference": "operator"},
        "prospective_estimate": {"remaining_high": 100},
    }
    if failure:
        with pytest.raises(TransitionBlocked):
            asyncio.run(resolve_conflict(app, "item", record, context, evidence))
        assert not (evidence / "candidate.json").exists()
        with pytest.raises(TransitionBlocked):
            asyncio.run(resolve_conflict(app, "item", record, context, evidence))
    else:
        result = asyncio.run(resolve_conflict(app, "item", record, context, evidence))
        assert asyncio.run(resolve_conflict(app, "item", record, context, evidence)) == result
    assert calls == ["integration-resolve"]


@pytest.mark.parametrize(
    "first,second",
    [
        ("blocked", "resolved"),
        ("reject", "resolved"),
        ("blocked", "blocked"),
        ("reject", "reject"),
        ("unknown", "resolved"),
        ("checks-failed", "resolved"),
        ("checks-failed", "checks-failed"),
    ],
)
def test_bounded_correction_retains_each_attempt_and_replays(conflict, monkeypatch, first, second):
    import asyncio
    import json
    from hashlib import sha256
    from pathlib import Path
    from types import SimpleNamespace

    from backlog_harness.contracts import digest
    from backlog_harness.evidence import atomic_json
    from backlog_harness.integration_flow import reconcile_integration
    from backlog_harness.integration_reconciliation import integration_attempt

    repo, _, evidence, args, _ = conflict
    root = evidence.parent / "flow"
    context = {
        **args,
        "workspace": root / "workspace",
        "candidate_repository": repo,
        "original_candidate": args["candidate"],
        "generated_paths": [],
        "generators": [],
        "authority": {"authorization": "existing task", "source_reference": "operator"},
        "prospective_estimate": {"remaining_high": 1000},
        "original_evidence": {"retained": True},
    }
    instruction = root / "instruction.json"
    atomic_json(instruction, {"original": args["candidate"]})
    calls = []
    monkeypatch.setattr("backlog_harness.integration_flow.validate_candidate", lambda *a: None)

    class App:
        config = SimpleNamespace(repository=repo)

        def _stage_path(self, item, stage):
            return root / "stages" / (stage + ".json")

        def validate_integration_instruction(self, item, instruction):
            return context

        def verify_integration_proof(self, *args):
            return {"not_required": True}

        def item_quiescent(self, item):
            return True

        def item_workflow(self, item):
            return {"checks": [["check"]]}

        def validate_check_execution(self, item, stage, candidate, checks):
            assert json.loads(self._stage_path(item, stage).read_text()) == checks

        def validate_integration_stage(self, result, item):
            if first == "unknown" and result is not None:
                raise TransitionBlocked("Unknown complete usage")

        def result_json(self, result):
            return result["value"]

        def checks(self, workspace, item, candidate, stage):
            mode = first if stage == "integration-checks" else second
            rows = [
                {
                    "argv": ["check"],
                    "candidate": candidate,
                    "returncode": 1 if mode == "checks-failed" else 0,
                    "output": "",
                    "evidence_sha256": sha256(b"").hexdigest(),
                }
            ]
            atomic_json(self._stage_path(item, stage), rows, exclusive=True)
            if mode == "checks-failed":
                raise TransitionBlocked("Required checks failed")
            return rows

        async def invoke(self, item, stage, role, prompt, **kwargs):
            path = self._stage_path(item, stage)
            if path.exists():
                return json.loads(path.read_text())
            calls.append(stage)
            resolving = "integration_resolution" in kwargs
            attempt = integration_attempt(
                stage, "integration-resolve" if resolving else "integration-review"
            )
            mode = first if attempt == 1 else second
            if resolving:
                request = kwargs["integration_resolution"]
                work = Path(request["workspace"])
                assert git(work, "rev-parse", "HEAD") == args["primary"]
                if attempt == 2:
                    assert request["previous_feedback"]["kind"] in {
                        "resolver-blocked",
                        "review-rejected",
                        "checks-failed",
                    }
                (work / "a").write_text(f"resolved attempt {attempt}\n")
                git(work, "add", "a")
                value = {
                    "item_id": item,
                    "resolution": "blocked" if mode == "blocked" else "resolved",
                    "resolution_digest": digest(request),
                    "evidence": f"attempt {attempt}",
                }
            else:
                value = {
                    "candidate": kwargs["review_candidate"]["candidate"],
                    "verdict": "REJECT" if mode == "reject" else "ACCEPT",
                    "producer_session": "producer",
                    "reviewer_session": f"review-{attempt}",
                    "unresolved_findings": ["fix meaning"] if mode == "reject" else [],
                    "native_verified": True,
                    "fresh_context": True,
                    "evidence_sha256": "receipt",
                }
            result = {"value": value}
            atomic_json(path, result, exclusive=True)
            return result

        def verify_integration_review(self, item, record, result):
            return result["value"]

    app = App()
    if second in {"blocked", "reject", "checks-failed"} or first == "unknown":
        for _ in range(2):
            with pytest.raises(TransitionBlocked, match="exhausted|Unknown"):
                asyncio.run(reconcile_integration(app, "item", instruction))
        assert not app._stage_path("item", "superseding-delivery-authorization").exists()
    else:
        result = asyncio.run(reconcile_integration(app, "item", instruction))
        assert result["attempt"] == 2
        snapshot = {p: p.read_bytes() for p in (root / "stages").glob("*.json")}
        assert asyncio.run(reconcile_integration(app, "item", instruction)) == result
        assert all(p.read_bytes() == data for p, data in snapshot.items())
        assert Path(result["candidate_record"]["workspace"]).name.endswith("-2")
        prior = app._stage_path(
            "item",
            "integration-resolve"
            if first == "blocked"
            else ("integration-checks" if first == "checks-failed" else "integration-review"),
        )
        changed = json.loads(prior.read_text())
        if first == "checks-failed":
            changed[0]["candidate"] = "substituted candidate"
        else:
            changed["value"]["evidence"] = "substituted prior feedback"
        atomic_json(prior, changed)
        with pytest.raises(
            TransitionBlocked, match="feedback changed|rejection changed|candidate evidence differs"
        ):
            asyncio.run(reconcile_integration(app, "item", instruction))
    assert len(calls) == len(set(calls))
    assert not any(name.endswith("-3") for name in calls)
    if first == "unknown":
        assert calls == ["integration-resolve"]
