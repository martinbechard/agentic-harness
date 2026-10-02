"""Optional overall verification binds exact existing receipts and independent attribution."""

import base64
import copy
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

import pytest

from backlog_harness import acceptance_verification as verification
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json
from backlog_harness.provider import TransitionBlocked


@pytest.fixture
def packet(tmp_path, monkeypatch):
    @dataclass
    class Binding:
        profile_name: str = "worker"

    contract = {"verification_required": True, "requirements": [{"id": "visual"}]}
    app = SimpleNamespace(
        item_workflow=lambda _: {"checks": [["check", "one"]], "proof_requirements": contract},
        _stage_path=lambda _item, stage: tmp_path / (stage + ".json"),
        native_sessions_root=lambda _: tmp_path,
        config=SimpleNamespace(binding=lambda _: Binding()),
    )
    source_review = {
        "candidate": "candidate",
        "verdict": "ACCEPT",
        "unresolved_findings": [],
        "producer_session": "source-producer",
        "reviewer_session": "source-reviewer",
        "evidence_sha256": "child-hash",
        "parent_evidence_sha256": "parent-original",
    }
    execution = {
        "item_id": "item",
        "candidate": "candidate",
        "stage": "source-checks",
        "commands": [["check", "one"]],
    }
    output = b"check passed\n"
    checks = [
        {
            "candidate": "candidate",
            "returncode": 0,
            "argv": ["check", "one"],
            "execution_digest": digest(execution),
            "output_base64": base64.b64encode(output).decode(),
            "output": output.decode(),
            "evidence_sha256": sha256(output).hexdigest(),
        }
    ]
    values = {
        "assignment": {
            "content": "All canonical acceptance requirements",
            "provider_revision": "revision",
        },
        "review": source_review,
        "source-checks": checks,
        "source-checks-execution": execution,
    }
    for name, value in values.items():
        atomic_json(app._stage_path("item", name), value)
    calls = []

    def native(*args):
        calls.append(args)
        assert args[:3] == ("source-producer", "source-reviewer", "candidate")
        return copy.deepcopy(source_review)

    monkeypatch.setattr(verification, "verify_native_review", native)
    proof_result = {"session": {"native_session_id": "proof-producer"}}
    return app, contract, values, proof_result, calls


def accepted_review(contract, bound):
    return {
        "reviewer_session": "independent-verifier",
        "verdict": "ACCEPT",
        "acceptance_verification": {
            "role": "independent-verifier",
            "candidate": "candidate",
            "requirements_digest": digest(contract),
            "inputs_digest": digest(bound),
            "inspected_receipts": bound["receipts"],
            "verdict": "ACCEPT",
            "unresolved_findings": [],
            "acceptance_coverage": "Checked every canonical criterion against captures, native source review and passing checks",
        },
    }


def test_exact_receipts_success_and_replay_are_read_only(packet):
    app, contract, _values, proof_result, _calls = packet
    before = {
        p.name: p.read_bytes() for p in app._stage_path("item", "assignment").parent.glob("*.json")
    }
    bound = verification.inputs(app, "item", "candidate")
    assert set(bound["receipts"]) == {
        "assignment",
        "review",
        "source-checks",
        "source-checks-execution",
    }
    for name, reference in bound["receipts"].items():
        assert reference["sha256"] == sha256(before[name + ".json"]).hexdigest()
    review = accepted_review(contract, bound)
    verification.validate(app, "item", "candidate", contract, proof_result, review)
    saved = app._stage_path("item", "acceptance-verification-inputs").read_bytes()
    verification.validate(app, "item", "candidate", contract, proof_result, review)
    assert app._stage_path("item", "acceptance-verification-inputs").read_bytes() == saved
    assert before == {
        p.name: p.read_bytes()
        for p in app._stage_path("item", "assignment").parent.glob("*.json")
        if p.name != "acceptance-verification-inputs.json"
    }
    # This fake deliberately has no invoke method: verification must never launch an agent.


@pytest.mark.parametrize(
    "name", ["assignment", "review", "source-checks", "source-checks-execution"]
)
def test_missing_or_tampered_receipt_blocks(packet, name):
    app, *_ = packet
    verification.inputs(app, "item", "candidate")
    path = app._stage_path("item", name)
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(TransitionBlocked, match="Missing verification receipt"):
        verification.inputs(app, "item", "candidate")
    path.write_bytes(original + b"\n")
    with pytest.raises(TransitionBlocked, match="receipts changed"):
        verification.inputs(app, "item", "candidate")


@pytest.mark.parametrize(
    "fault",
    [
        "failed",
        "wrong_candidate",
        "wrong_execution",
        "missing_check",
        "raw_hash",
        "invalid_base64",
        "wrong_command",
        "false_returncode",
        "float_returncode",
        "output_text",
    ],
)
def test_invalid_check_cannot_establish_inputs(packet, fault):
    app, _, values, *_ = packet
    checks = values["source-checks"]
    if fault == "missing_check":
        checks.clear()
    else:
        key, value = {
            "failed": ("returncode", 1),
            "wrong_candidate": ("candidate", "other"),
            "wrong_execution": ("execution_digest", "wrong"),
            "raw_hash": ("evidence_sha256", "wrong"),
            "invalid_base64": ("output_base64", "*invalid*"),
            "wrong_command": ("argv", ["other"]),
            "false_returncode": ("returncode", False),
            "float_returncode": ("returncode", 0.0),
            "output_text": ("output", "different"),
        }[fault]
        checks[0][key] = value
    atomic_json(app._stage_path("item", "source-checks"), checks)
    with pytest.raises(TransitionBlocked):
        verification.inputs(app, "item", "candidate")
    assert not app._stage_path("item", "acceptance-verification-inputs").exists()


@pytest.mark.parametrize("identity", ["source-producer", "source-reviewer", "proof-producer"])
def test_verifier_must_be_independent_of_all_three_roles(packet, identity):
    app, contract, _, proof_result, _ = packet
    bound = verification.inputs(app, "item", "candidate")
    review = accepted_review(contract, bound)
    review["reviewer_session"] = identity
    with pytest.raises(TransitionBlocked, match="overlaps"):
        verification.validate(app, "item", "candidate", contract, proof_result, review)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "reject",
        "candidate",
        "requirements",
        "inputs",
        "receipts",
        "coverage",
        "findings",
        "role",
    ],
)
def test_visual_accept_does_not_replace_overall_verification(packet, fault):
    app, contract, _, proof_result, _ = packet
    review = accepted_review(contract, verification.inputs(app, "item", "candidate"))
    if fault == "missing":
        del review["acceptance_verification"]
    else:
        field, value = {
            "reject": ("verdict", "REJECT"),
            "candidate": ("candidate", "other"),
            "requirements": ("requirements_digest", "wrong"),
            "inputs": ("inputs_digest", "wrong"),
            "receipts": ("inspected_receipts", {}),
            "coverage": ("acceptance_coverage", " "),
            "findings": ("unresolved_findings", ["Criterion fails"]),
            "role": ("role", "source-reviewer"),
        }[fault]
        review["acceptance_verification"][field] = value
    assert review["verdict"] == "ACCEPT"
    with pytest.raises(TransitionBlocked, match="Independent acceptance verification"):
        verification.validate(app, "item", "candidate", contract, proof_result, review)


def test_retained_review_allows_only_parent_growth(packet):
    app, _, values, *_ = packet
    verification.inputs(app, "item", "candidate")
    path = app._stage_path("item", "review")
    original = path.read_bytes()
    grown = {**values["review"], "parent_evidence_sha256": "parent-grown"}
    assert verification.retain_review(app, "item", grown) == values["review"]
    assert path.read_bytes() == original
    for field in ("evidence_sha256", "candidate", "reviewer_session", "producer_session"):
        changed = {**grown, field: "changed"}
        with pytest.raises(TransitionBlocked, match="Bound source review changed"):
            verification.retain_review(app, "item", changed)
        assert path.read_bytes() == original


def test_changed_native_child_rejects_saved_receipt(packet, monkeypatch):
    app, _, values, *_ = packet
    verification.inputs(app, "item", "candidate")
    monkeypatch.setattr(
        verification,
        "verify_native_review",
        lambda *_: {**values["review"], "evidence_sha256": "changed-child"},
    )
    with pytest.raises(TransitionBlocked, match="stale or invalid"):
        verification.inputs(app, "item", "candidate")


def test_optional_absence_does_not_require_receipts():
    verification.validate(None, "item", "candidate", {}, {}, {})


def test_selected_design_adds_exact_receipt_and_artifact(packet, monkeypatch):
    from backlog_harness import recovery_flow

    app, contract, *_ = packet
    app.item_workflow = lambda _: {
        "checks": [["check", "one"]],
        "proof_requirements": contract,
        "design_review": {"required": True},
    }
    artifact = {"path": "/bound/design.md", "sha256": "design-content-hash"}
    accepted = {"design": artifact, "source_base": "base", "attempt": 1}
    path = app._stage_path("item", "design-acceptance")
    atomic_json(path, accepted)
    calls = []

    def verify(selected_app, item):
        assert selected_app is app and item == "item"
        calls.append(item)
        return accepted

    monkeypatch.setattr(recovery_flow, "verify_design_acceptance", verify)
    bound = verification.inputs(app, "item", "candidate")
    assert bound["receipts"]["design-acceptance"] == {
        "path": str(path.resolve()),
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }
    assert bound["receipts"]["accepted-design"] == artifact
    assert verification.inputs(app, "item", "candidate") == bound
    assert calls == ["item", "item"]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(TransitionBlocked, match="receipts changed"):
        verification.inputs(app, "item", "candidate")


@pytest.mark.parametrize("reason", ["missing accepted design", "changed accepted design"])
def test_design_verifier_rejection_blocks_overall_inputs(packet, monkeypatch, reason):
    from backlog_harness import recovery_flow

    app, contract, *_ = packet
    app.item_workflow = lambda _: {
        "checks": [["check", "one"]],
        "proof_requirements": contract,
        "design_review": {"required": True},
    }

    def reject(*_):
        raise TransitionBlocked(reason)

    monkeypatch.setattr(recovery_flow, "verify_design_acceptance", reject)
    with pytest.raises(TransitionBlocked, match=reason):
        verification.inputs(app, "item", "candidate")
    assert not app._stage_path("item", "acceptance-verification-inputs").exists()
