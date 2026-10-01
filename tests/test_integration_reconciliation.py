import pytest

from backlog_harness.integration_reconciliation import (
    authorize_candidate,
    prepare_candidate,
    proof_reuse,
)
from backlog_harness.provider import TransitionBlocked, git


@pytest.fixture
def branches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "a").write_text("one\ntwo\nthree\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("ONE\ntwo\nthree\n")
    git(repo, "commit", "-am", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach", base)
    (repo / "b").write_text("primary")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "primary")
    return repo, {"base": base, "candidate": candidate, "primary": git(repo, "rev-parse", "HEAD")}


def test_merge_restart_and_stale_inputs(branches, tmp_path):
    repo, args = branches
    work, evidence = tmp_path / "work", tmp_path / "evidence"
    result = prepare_candidate(repo, work, evidence, **args, allowed_paths=["a"])
    assert (work / "a").read_text().startswith("ONE")
    assert (work / "b").read_text() == "primary"
    assert git(repo, "rev-parse", "HEAD") == args["primary"]
    assert prepare_candidate(repo, work, evidence, **args, allowed_paths=["a"]) == result
    with pytest.raises(TransitionBlocked, match="inputs changed"):
        prepare_candidate(repo, work, evidence, **args, allowed_paths=["a", "b"])
    (work / "a").write_text("tampered")
    with pytest.raises(TransitionBlocked, match="dirty"):
        prepare_candidate(repo, work, evidence, **args, allowed_paths=["a"])


def test_conflict_preserved_and_not_repeated(branches, tmp_path):
    repo, args = branches
    (repo / "a").write_text("different\ntwo\nthree\n")
    git(repo, "commit", "-am", "conflict")
    args["primary"] = git(repo, "rev-parse", "HEAD")
    for message in ("conflicts", "uncertain"):
        with pytest.raises(TransitionBlocked, match=message):
            prepare_candidate(repo, tmp_path / "work", tmp_path / "ev", **args, allowed_paths=["a"])
    assert (repo / "a").read_text().startswith("different")


def test_new_review_and_checks_required():
    record = {"candidate": "new", "original_candidate": "old", "primary": "primary"}
    review = {
        "candidate": "new",
        "verdict": "ACCEPT",
        "reviewer_session": "reviewer",
        "producer_session": "producer",
    }
    checks = [{"candidate": "new", "returncode": 0}]
    authority = {"source_reference": "human-message", "authorization": "fix until working"}
    assert (
        authorize_candidate(record, review, checks, authority, verify_review=lambda r: True)[
            "candidate"
        ]
        == "new"
    )
    for invalid in ({**review, "candidate": "old"}, {**review, "reviewer_session": "producer"}):
        with pytest.raises(TransitionBlocked):
            authorize_candidate(record, invalid, checks, authority, verify_review=lambda r: True)
    with pytest.raises(TransitionBlocked, match="Native"):
        authorize_candidate(record, review, checks, authority, verify_review=lambda r: False)
    with pytest.raises(TransitionBlocked, match="checks"):
        authorize_candidate(record, review, [], authority, verify_review=lambda r: True)


def test_proof_manifest_cannot_be_arbitrary_subset():
    inputs = {"source": "a" * 64}
    with pytest.raises(TransitionBlocked, match="manifest"):
        proof_reuse("receipt", inputs, inputs, manifest={}, verify_manifest=lambda m: True)
    manifest = {"receipt": "receipt", "complete": True, "scope": "fixture", "inputs": inputs}
    with pytest.raises(TransitionBlocked, match="coverage"):
        proof_reuse("receipt", inputs, inputs, manifest=manifest, verify_manifest=lambda m: False)
    with pytest.raises(TransitionBlocked, match="identity"):
        proof_reuse(
            "receipt",
            inputs,
            {"source": "b" * 64},
            manifest=manifest,
            verify_manifest=lambda m: True,
        )


def test_separate_candidate_repository_and_stale_primary(branches, tmp_path):
    repo, args = branches
    separate = tmp_path / "candidate-repo"
    git(repo, "clone", str(repo), str(separate))
    git(separate, "checkout", "--detach", args["candidate"])
    git(separate, "config", "user.email", "test@example.com")
    git(separate, "config", "user.name", "Test")
    (separate / "a").write_text("NEW\ntwo\nthree\n")
    git(separate, "commit", "-am", "separate candidate")
    args["candidate"] = git(separate, "rev-parse", "HEAD")
    result = prepare_candidate(
        repo,
        tmp_path / "work",
        tmp_path / "ev",
        **args,
        candidate_repository=separate,
        allowed_paths=["a"],
    )
    assert result["original_candidate"] == args["candidate"]
    (repo / "b").write_text("advanced")
    git(repo, "commit", "-am", "advance primary")
    with pytest.raises(TransitionBlocked, match="Primary advanced"):
        prepare_candidate(
            repo,
            tmp_path / "work",
            tmp_path / "ev",
            **args,
            candidate_repository=separate,
            allowed_paths=["a"],
        )


def test_declared_generated_conflict_regenerated(branches, tmp_path):
    import sys

    repo, args = branches
    (repo / "a").write_text("different\ntwo\nthree\n")
    git(repo, "commit", "-am", "generated conflict")
    args["primary"] = git(repo, "rev-parse", "HEAD")
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('a').write_text('regenerated\\n')",
    ]
    result = prepare_candidate(
        repo,
        tmp_path / "work",
        tmp_path / "ev",
        **args,
        allowed_paths=["a"],
        generated_paths=["a"],
        generators=[command],
    )
    assert (tmp_path / "work" / "a").read_text() == "regenerated\n"
    assert (tmp_path / "ev" / "conflicts.json").exists()
    assert result["candidate"] != args["candidate"]


def test_flow_requires_verified_new_review_and_reuses_result(branches, tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace

    from backlog_harness.evidence import atomic_json
    from backlog_harness.integration_flow import load_integration, reconcile_integration

    repo, args = branches
    stage = tmp_path / "stages"
    stage.mkdir()
    context = {
        **args,
        "original_candidate": args["candidate"],
        "workspace": tmp_path / "work",
        "candidate_repository": repo,
        "allowed_paths": ["a"],
        "generated_paths": [],
        "generators": [],
        "original_evidence": {"receipt": "retained"},
        "authority": {"source_reference": "human", "authorization": "fix until working"},
        "prospective_estimate": {"remaining_high": 1000},
    }

    class App:
        config = SimpleNamespace(repository=repo)
        calls = 0
        valid_review = False

        def _stage_path(self, item, name):
            return stage / (name + ".json")

        def validate_integration_instruction(self, item, instruction):
            return context

        def verify_integration_proof(self, item, record, instruction):
            return {"original_receipt": "retained", "verified": True}

        def validate_integration_stage(self, result, item):
            return {"generated_tokens": 1}

        def validate_check_execution(self, item, stage, candidate, receipts):
            assert receipts[0]["candidate"] == candidate

        def item_workflow(self, item):
            return {"checks": [["check"]]}

        def checks(self, repository, item, candidate, name):
            value = [
                {
                    "candidate": candidate,
                    "returncode": 0,
                    "argv": ["check"],
                    "output": "",
                    "evidence_sha256": __import__("hashlib").sha256(b"").hexdigest(),
                }
            ]
            atomic_json(self._stage_path(item, name), value)
            return value

        async def invoke(self, item, name, role, prompt, **kwargs):
            path = self._stage_path(item, name)
            if path.exists():
                return json.loads(path.read_text())
            self.calls += 1
            assert kwargs["read_only"] and "Integration review identity: " in prompt
            value = {
                "fresh_session": "reviewer",
                "candidate": kwargs["review_candidate"]["candidate"],
            }
            atomic_json(path, value)
            return value

        def verify_integration_review(self, item, record, result):
            if not self.valid_review:
                raise TransitionBlocked("Native evidence missing")
            return {
                "candidate": record["candidate"],
                "verdict": "ACCEPT",
                "producer_session": "producer",
                "reviewer_session": result["fresh_session"],
                "native_verified": True,
                "fresh_context": True,
                "evidence_sha256": "abc",
                "unresolved_findings": [],
            }

    instruction = tmp_path / "instruction.json"
    instruction.write_text("{}")
    app = App()
    with pytest.raises(TransitionBlocked, match="Native evidence"):
        asyncio.run(reconcile_integration(app, "item", instruction))
    assert not app._stage_path("item", "superseding-delivery-authorization").exists()
    app.valid_review = True
    result = asyncio.run(reconcile_integration(app, "item", instruction))
    assert result == load_integration(app, "item", args["candidate"])
    assert app.calls == 1
    assert asyncio.run(reconcile_integration(app, "item", instruction)) == result
    assert app.calls == 1


@pytest.mark.parametrize("fault", ["commands", "output", "candidate"])
def test_retained_integration_checks_require_exact_coverage_and_content(fault):
    from hashlib import sha256

    from backlog_harness.integration_flow import validate_checks

    row = {
        "argv": ["required-check"],
        "candidate": "candidate",
        "returncode": 0,
        "output": "passed",
        "evidence_sha256": sha256(b"passed").hexdigest(),
    }
    if fault == "commands":
        row["argv"] = ["true"]
    if fault == "output":
        row["output"] = "rewritten"
    if fault == "candidate":
        row["candidate"] = "other"
    with pytest.raises(TransitionBlocked):
        validate_checks([row], "candidate", [["required-check"]])
