"""Bind an optional independent acceptance assessment to existing source evidence."""

import base64
import json
from dataclasses import asdict
from hashlib import sha256

from .contracts import digest
from .evidence import atomic_json
from .native_evidence import verify_native_review
from .workflow import require


def required(app, item_id):
    return (
        app.item_workflow(item_id).get("proof_requirements", {}).get("verification_required", False)
    )


def retain_review(app, item_id, review):
    """A resumed parent may grow; preserve the exact independently reviewed child receipt."""
    path = app._stage_path(item_id, "review")
    if (
        required(app, item_id)
        and app._stage_path(item_id, "acceptance-verification-inputs").exists()
    ):
        require(path.exists(), "Bound source review receipt is missing")
        original = json.loads(path.read_text())
        stable = lambda value: {k: v for k, v in value.items() if k != "parent_evidence_sha256"}
        require(stable(original) == stable(review), "Bound source review changed")
        return original
    atomic_json(path, review)
    return review


def inputs(app, item_id, candidate):
    """Freeze exact existing receipt files, then validate their contents on every reuse."""
    path = app._stage_path(item_id, "acceptance-verification-inputs")
    names = ("assignment", "review", "source-checks", "source-checks-execution")
    design = None
    if app.item_workflow(item_id).get("design_review"):
        from .recovery_flow import verify_design_acceptance

        design = verify_design_acceptance(app, item_id)
        names += ("design-acceptance",)
    references, values = {}, {}
    for name in names:
        receipt = app._stage_path(item_id, name)
        require(
            receipt.is_file() and not receipt.is_symlink(), "Missing verification receipt: " + name
        )
        raw = receipt.read_bytes()
        references[name] = {"path": str(receipt.resolve()), "sha256": sha256(raw).hexdigest()}
        values[name] = json.loads(raw)
    if design:
        references["accepted-design"] = design["design"]
    value = {"candidate": candidate, "receipts": references}
    if path.exists():
        require(json.loads(path.read_text()) == value, "Acceptance verification receipts changed")
    review = values["review"]
    current = verify_native_review(
        review.get("producer_session"),
        review.get("reviewer_session"),
        candidate,
        app.native_sessions_root(asdict(app.config.binding("orchestrator"))),
    )
    require(
        current["evidence_sha256"] == review.get("evidence_sha256")
        and review.get("candidate") == candidate
        and review.get("verdict") == "ACCEPT"
        and review.get("unresolved_findings") == [],
        "Acceptance verification source review is stale or invalid",
    )
    checks, execution = values["source-checks"], values["source-checks-execution"]
    commands = [list(argv) for argv in app.item_workflow(item_id)["checks"]]
    require(
        isinstance(checks, list)
        and len(checks) == len(commands)
        and bool(checks)
        and execution.get("item_id") == item_id
        and execution.get("candidate") == candidate
        and execution.get("stage") == "source-checks"
        and execution.get("commands") == commands,
        "Acceptance verification check execution differs",
    )
    for check, command in zip(checks, commands):
        require(
            check.get("candidate") == candidate
            and type(check.get("returncode")) is int
            and check.get("returncode") == 0
            and check.get("argv") == command
            and check.get("execution_digest") == digest(execution)
            and isinstance(check.get("output_base64"), str),
            "Acceptance verification requires passing candidate-bound checks",
        )
        try:
            output = base64.b64decode(check["output_base64"], validate=True)
        except ValueError:
            require(False, "Acceptance verification check output is invalid")
        require(
            sha256(output).hexdigest() == check.get("evidence_sha256"), "Check output hash differs"
        )
        require(
            check.get("output") == output.decode(errors="replace"),
            "Check output text differs from bound bytes",
        )
    assignment = values["assignment"]
    require(
        isinstance(assignment.get("content"), str) and bool(assignment["content"].strip()),
        "Canonical acceptance content is missing",
    )
    if not path.exists():
        atomic_json(path, value, exclusive=True)
    return value


def validate(app, item_id, candidate, contract, proof_result, review):
    if not contract.get("verification_required"):
        return
    bound = inputs(app, item_id, candidate)
    source_review = json.loads(app._stage_path(item_id, "review").read_text())
    require(
        review.get("reviewer_session")
        not in {
            source_review.get("reviewer_session"),
            source_review.get("producer_session"),
            proof_result["session"]["native_session_id"],
        },
        "Acceptance verifier overlaps producer or source reviewer identity",
    )
    assessment = review.get("acceptance_verification")
    require(
        isinstance(assessment, dict)
        and assessment.get("role") == "independent-verifier"
        and assessment.get("candidate") == candidate
        and assessment.get("requirements_digest") == digest(contract)
        and assessment.get("inputs_digest") == digest(bound)
        and assessment.get("inspected_receipts") == bound["receipts"]
        and assessment.get("verdict") == "ACCEPT"
        and assessment.get("unresolved_findings") == []
        and isinstance(assessment.get("acceptance_coverage"), str)
        and bool(assessment["acceptance_coverage"].strip()),
        "Independent acceptance verification is missing, stale or rejected",
    )
