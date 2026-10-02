"""Explicit visual requirements cannot be discharged by a generic source ACCEPT."""

import asyncio
import copy
import json
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

import pytest

from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked
from backlog_harness.recovery_flow import (
    ensure_configured_proof,
    validate_requirement_results,
    validate_requirement_review,
    verify_candidate_approval,
    verify_configured_proof,
)


@pytest.fixture
def contract():
    return {
        "provider_revision": "revision",
        "preparation_digest": digest({"decision": "prepared"}),
        "requirements": [
            {
                "id": "screen",
                "canonical_reference": "item#screen",
                "evidence_kind": "browser",
                "acceptance_text": "All columns readable",
            },
            {
                "id": "paper",
                "canonical_reference": "item#print",
                "evidence_kind": "print",
                "acceptance_text": "Letter portrait within margins",
            },
        ],
    }


@pytest.fixture
def proof():
    artifact = {"path": "/artifacts/capture.png", "sha256": "hash"}
    return {
        "artifacts": [artifact],
        "requirement_results": [
            {"id": name, "outcome": "PASS", "artifacts": [artifact]} for name in ("screen", "paper")
        ],
    }


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "failed", "unbound", "empty"])
def test_requirement_results_fail_closed(contract, proof, fault):
    if fault == "missing":
        proof["requirement_results"].pop()
    elif fault == "extra":
        proof["requirement_results"].append({"id": "extra"})
    elif fault == "duplicate":
        proof["requirement_results"][1] = proof["requirement_results"][0]
    elif fault == "failed":
        proof["requirement_results"][0]["outcome"] = "FAIL"
    else:
        proof["requirement_results"][0]["artifacts"] = (
            [] if fault == "empty" else [{"path": "/other", "sha256": "hash"}]
        )
    with pytest.raises(TransitionBlocked):
        validate_requirement_results(contract, proof)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "generic",
        "digest",
        "requirements",
        "missing",
        "duplicate",
        "rejected",
        "blank",
        "unbound",
    ],
)
def test_review_requires_bound_substantive_assessments(contract, proof, fault):
    review = {
        "proof_result_digest": digest(proof),
        "requirements_digest": digest(contract),
        "requirement_assessments": [
            {
                "id": row["id"],
                "verdict": "ACCEPT",
                "assessment": "Inspected readable capture",
                "artifacts": row["artifacts"],
            }
            for row in proof["requirement_results"]
        ],
    }
    if fault is None:
        validate_requirement_results(contract, proof)
        validate_requirement_review(contract, proof, proof, review)
        return
    if fault == "generic":
        review = {"verdict": "ACCEPT"}
    elif fault in {"digest", "requirements"}:
        review["proof_result_digest" if fault == "digest" else "requirements_digest"] = "wrong"
    elif fault == "missing":
        review["requirement_assessments"].pop()
    elif fault == "duplicate":
        review["requirement_assessments"][1] = review["requirement_assessments"][0]
    else:
        review["requirement_assessments"][0][
            {"rejected": "verdict", "blank": "assessment", "unbound": "artifacts"}[fault]
        ] = {"rejected": "REJECT", "blank": " ", "unbound": [{"path": "/other"}]}[fault]
    with pytest.raises(TransitionBlocked):
        validate_requirement_review(contract, proof, proof, review)


def test_candidate_approval_exact_and_optional(tmp_path):
    path = tmp_path / "continuation.json"
    app = SimpleNamespace(
        item_workflow=lambda _: {}, recovery_record=lambda _: None, _stage_path=lambda *_: path
    )
    verify_candidate_approval(app, "item", "candidate")
    app.item_workflow = lambda _: {"candidate_approval_required": True}
    with pytest.raises(TransitionBlocked, match="Exact candidate"):
        verify_candidate_approval(app, "item", "candidate")
    approved = {
        "item_id": "item",
        "disposition": "approve",
        "question": {"candidate": "candidate"},
        "answer": {"text": "yes", "digest": digest("yes")},
    }
    atomic_json(path, {"approval": approved})
    verify_candidate_approval(app, "item", "candidate")
    with pytest.raises(TransitionBlocked, match="Exact candidate"):
        verify_candidate_approval(app, "item", "different")
    approved["answer"]["text"] = "changed"
    atomic_json(path, {"approval": approved})
    with pytest.raises(TransitionBlocked):
        verify_candidate_approval(app, "item", "candidate")


def test_configured_proof_replay_and_delivery_revalidation(tmp_path, contract, monkeypatch):
    from backlog_harness import native_evidence, recovery_flow

    @dataclass
    class Binding:
        profile_name: str = "worker"
        permission_digest: str = "permissions"

    binding = Binding()
    calls = []
    review_values = {}
    acceptance = {"session": {"session_id": "owner", "native_session_id": "owner-native"}}

    class App:
        root = tmp_path
        config = SimpleNamespace(
            binding=lambda _: binding, data={"profiles": {"worker": {"artifact_output": True}}}
        )

        def _stage_path(self, item, stage):
            return tmp_path / "stages" / (stage + ".json")

        def item_workflow(self, _):
            return {"proof_requirements": contract}

        def candidate_repository(self, _):
            return tmp_path

        def result_json(self, result):
            return json.loads(result["text"])

        def validate_invocation_result(self, result):
            assert result["invocation_id"]

        def native_sessions_root(self, _):
            return tmp_path

        def session(self, result):
            return result["session"]

        async def enforce_guard(self, _):
            pass

        async def invoke(self, item, stage, role, prompt, **kwargs):
            path = self._stage_path(item, stage)
            if path.exists():
                return json.loads(path.read_text())
            calls.append(stage)
            if kwargs.get("purpose") == "proof":
                invocation = "proof-invocation"
                operation = item + ":" + stage
                evidence = (
                    tmp_path
                    / "runs"
                    / component("item:" + item)
                    / "operations"
                    / component(operation)
                    / "invocations"
                    / component(invocation)
                )
                output = evidence / "artifacts"
                output.mkdir(parents=True)
                capture = output / "capture.txt"
                capture.write_text("visible captured evidence")
                artifact = {
                    "path": str(capture),
                    "sha256": sha256(capture.read_bytes()).hexdigest(),
                }
                atomic_json(
                    evidence / "artifact-output.json",
                    {
                        "version": 1,
                        "invocation_id": invocation,
                        "operation_id": operation,
                        "path": str(output),
                        "writer": "invoked agent",
                        "reviewer_access": "read",
                        "permission_digest": "permissions",
                    },
                )
                value = {
                    "item_id": item,
                    "candidate": "candidate",
                    "status": "evidence-ready",
                    "artifacts": [artifact],
                    "requirement_results": [
                        {"id": row["id"], "outcome": "PASS", "artifacts": [artifact]}
                        for row in contract["requirements"]
                    ],
                }
                result = {
                    "invocation_id": invocation,
                    "binding": {"profile_name": "worker", "permission_digest": "permissions"},
                    "role": role,
                    "purpose": "proof",
                    "session": {"session_id": "proof-owner", "native_session_id": "proof-native"},
                    "evidence_path": str(evidence),
                    "text": json.dumps(value),
                }
                review_values.update(
                    proof_result_digest=digest(result),
                    requirements_digest=digest(contract),
                    requirement_assessments=[
                        {
                            "id": row["id"],
                            "verdict": "ACCEPT",
                            "assessment": "Capture satisfies acceptance text",
                            "artifacts": [artifact],
                        }
                        for row in contract["requirements"]
                    ],
                )
            else:
                assert kwargs["session"] == acceptance["session"] and kwargs["read_only"] is True
                assert "Original preparation receipt" in prompt
                result = {
                    "invocation_id": "review-invocation",
                    "binding": {},
                    "session": acceptance["session"],
                    "text": json.dumps(
                        {
                            "item_id": item,
                            "candidate": "candidate",
                            "proof_reviewer_session": "child",
                        }
                    ),
                }
            atomic_json(path, result)
            return result

    app = App()
    atomic_json(app._stage_path("item", "accept"), acceptance)
    atomic_json(
        app._stage_path("item", "preparation"),
        {"item": {"revision": "revision"}, "decision": {"decision": "prepared"}},
    )
    monkeypatch.setattr(
        recovery_flow, "git", lambda _, *args: "candidate" if args[0] == "rev-parse" else ""
    )
    monkeypatch.setattr(
        native_evidence, "verify_native_review", lambda *args: copy.deepcopy(review_values)
    )
    with pytest.raises(TransitionBlocked, match="missing or stale"):
        verify_configured_proof(app, "item", "candidate")
    asyncio.run(ensure_configured_proof(app, "item", "candidate", acceptance))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}
    asyncio.run(ensure_configured_proof(app, "item", "candidate", acceptance))
    verify_configured_proof(app, "item", "candidate")
    assert len(calls) == 2
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(TransitionBlocked, match="missing or stale"):
        verify_configured_proof(app, "item", "wrong-candidate")
    next(tmp_path.rglob("capture.txt")).write_text("tampered")
    with pytest.raises(TransitionBlocked, match="hash differs"):
        verify_configured_proof(app, "item", "candidate")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "fault", [None, "duplicate", "kind", "empty", "unknown", "missing_binding", "approval_type"]
)
def test_explicit_contract_configuration(config_file, contract, fault):
    import yaml

    from backlog_harness.contracts import ConfigError, load_config

    path, data = config_file
    selected = {
        "allowed_paths": ["answer.py"],
        "proof_requirements": contract,
        "candidate_approval_required": True,
    }
    data["workflow"]["items"] = {"item": selected}
    if fault == "duplicate":
        contract["requirements"][1]["id"] = "screen"
    elif fault == "kind":
        contract["requirements"][0]["evidence_kind"] = "arbitrary-tool"
    elif fault == "empty":
        contract["requirements"] = []
    elif fault == "unknown":
        contract["permissions"] = ["all"]
    elif fault == "missing_binding":
        del contract["preparation_digest"]
    elif fault == "approval_type":
        selected["candidate_approval_required"] = "yes"
    path.write_text(yaml.safe_dump(data))
    if fault:
        with pytest.raises(ConfigError):
            load_config(path)
    else:
        assert (
            load_config(path).data["workflow"]["items"]["item"]["candidate_approval_required"]
            is True
        )
