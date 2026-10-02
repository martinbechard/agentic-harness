"""Retained package validity and applicability are separate integration decisions."""

import json
from hashlib import sha256

import pytest
from test_integration_authority import case  # noqa: F401

from backlog_harness import integration_authority as authority
from backlog_harness import recovery_flow
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json
from backlog_harness.provider import TransitionBlocked, git


@pytest.fixture
def scoped(request, monkeypatch):
    app, instruction, native_review = request.getfixturevalue("case")
    candidate = instruction["original_candidate"]
    repo = app.candidate_repository("item")
    root = app.root / "package"
    expected = sha256(b"candidate").hexdigest()
    judges = [{"case": "retained-case", "identity": "independent-judge", "verdict": "ACCEPT"}]
    binding = {
        "candidate": candidate,
        "scope": "Exact retained case input and expectation contract",
        "checks": [{"path": "a", "candidate_sha256": expected}],
    }
    committed = {
        "candidate": candidate,
        "all_match": True,
        "checks": [
            {"path": "a", "committed_sha256": expected, "matches_bound_candidate_bytes": True}
        ],
    }
    proof_result = {"original": "native-verified-proof"}

    def write(name, value):
        path = root / name
        atomic_json(path, value)
        return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}

    def build():
        bound = write("candidate-binding.json", binding)
        commit = write("committed-input-binding.json", committed)
        expectation = write("expectation.json", {"case": "retained-case", "expected": True})
        inventory = write(
            "proof-input-inventory.json",
            {"candidate": candidate, "artifacts": [bound, expectation]},
        )
        review = write(
            "independent-result-review.json",
            {
                "candidate": candidate,
                "verdict": "ACCEPT",
                "blockers": [],
                "sha256_by_relative_path": {
                    name: row["sha256"]
                    for name, row in (
                        ("candidate-binding.json", bound),
                        ("committed-input-binding.json", commit),
                        ("proof-input-inventory.json", inventory),
                        ("expectation.json", expectation),
                    )
                },
                "independent_judges": judges,
            },
        )
        package = write(
            "proof-result.json",
            {
                "item_id": "item",
                "candidate": candidate,
                "status": "evidence-ready",
                "blockers": [],
                "artifacts": [bound, commit, expectation, inventory, review],
                "independent_result_review": {**review, "verdict": "ACCEPT"},
                "independent_judges": judges,
            },
        )
        return {"artifacts": [package, review]}

    value = build()
    native_review["proof_result_digest"] = digest(proof_result)
    atomic_json(app._stage_path("item", "artifact-proof-request"), {"retained": True})
    atomic_json(app._stage_path("item", "proof-review"), {"reviewer_session": "reviewer"})
    monkeypatch.setattr(
        recovery_flow,
        "validated_auxiliary_proof",
        lambda *args: ({"candidate": candidate}, proof_result, value),
    )
    record = {"original_candidate": candidate, "candidate": candidate, "workspace": str(repo)}
    return app, instruction, record, root, binding, committed, value, build, native_review


def verify(scoped):
    app, instruction, record, *_ = scoped
    return authority.verify_integration_proof(app, "item", record, instruction)


def test_unrelated_merged_change_preserves_package_but_requires_adequacy_review(scoped):
    app, _, record, root, *_ = scoped
    before = {p: p.read_bytes() for p in root.glob("*.json")}
    repo = app.candidate_repository("item")
    (repo / "unrelated").write_text("other task")
    git(repo, "add", "unrelated")
    git(repo, "commit", "-m", "unrelated")
    record["candidate"] = git(repo, "rev-parse", "HEAD")
    receipt = verify(scoped)
    assert receipt["disposition"] == "review-required"
    context = receipt["context"]
    assert context["changed_dependencies"] == []
    assert context["cases"] == ["retained-case"]
    assert context["candidate"] == record["candidate"]
    assert receipt["context_digest"] == digest(context)
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("change", ["bytes", "mode", "missing"])
def test_merged_dependency_delta_requires_explicit_applicability_judgment(scoped, change):
    app, _, record, *_ = scoped
    repo = app.candidate_repository("item")
    if change == "bytes":
        (repo / "a").write_text("different behavior")
    elif change == "mode":
        (repo / "a").chmod(0o755)
    else:
        (repo / "a").unlink()
    git(repo, "commit", "-am", change)
    record["candidate"] = git(repo, "rev-parse", "HEAD")
    context = verify(scoped)["context"]
    assert context["changed_dependencies"] == ["a"]
    if change == "missing":
        assert context["dependencies"][0]["merged"] is None
    else:
        assert context["dependencies"][0]["original"] != context["dependencies"][0]["merged"]


@pytest.mark.parametrize(
    "fault",
    [
        "omit",
        "duplicate",
        "original-hash",
        "candidate",
        "native-unbound",
        "tamper",
        "missing",
        "symlink",
        "recursive-omission",
    ],
)
def test_invalid_original_package_never_reaches_applicability_review(scoped, fault):
    _, _, _, root, binding, committed, value, build, native = scoped
    if fault == "omit":
        committed["checks"] = []
    elif fault == "duplicate":
        binding["checks"].append(dict(binding["checks"][0]))
    elif fault == "original-hash":
        binding["checks"][0]["candidate_sha256"] = "0" * 64
        committed["checks"][0]["committed_sha256"] = "0" * 64
    elif fault == "candidate":
        binding["candidate"] = "wrong"
    elif fault == "native-unbound":
        native["proof_result_digest"] = "wrong"
    value.update(build())
    if fault == "tamper":
        (root / "expectation.json").write_text("{}")
    elif fault == "missing":
        (root / "expectation.json").unlink()
    elif fault == "symlink":
        expected = (root / "expectation.json").read_bytes()
        (root / "expectation.json").unlink()
        (root / "elsewhere").write_bytes(expected)
        (root / "expectation.json").symlink_to(root / "elsewhere")
    elif fault == "recursive-omission":
        path = root / "omitted.json"
        atomic_json(path, {})
        inventory = json.loads((root / "proof-input-inventory.json").read_text())
        inventory["artifacts"].append(
            {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
        )
        # Existing outer reviewed hashes must reject this post-acceptance addition.
        atomic_json(root / "proof-input-inventory.json", inventory)
    with pytest.raises(TransitionBlocked):
        verify(scoped)


def test_package_preserves_non_json_artifact_hashes(scoped):
    _app, _instruction, _record, root, _binding, _committed, value, *_ = scoped
    artifact = root / "rendered-output.txt"
    artifact.write_text("Retained visual inspection output\n")
    package_path = root / "proof-result.json"
    package = json.loads(package_path.read_text())
    package["artifacts"].append(
        {"path": str(artifact), "sha256": sha256(artifact.read_bytes()).hexdigest()}
    )
    atomic_json(package_path, package)
    value["artifacts"][0]["sha256"] = sha256(package_path.read_bytes()).hexdigest()
    result = verify(scoped)
    assert result["disposition"] == "review-required"
    artifact.write_text("Changed output\n")
    with pytest.raises(TransitionBlocked):
        verify(scoped)
