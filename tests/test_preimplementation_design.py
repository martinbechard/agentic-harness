"""Optional design acceptance precedes implementation and reuses immutable native evidence."""

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from types import SimpleNamespace

import pytest

from backlog_harness import recovery_flow
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked


@pytest.fixture
def design_app(tmp_path, monkeypatch):
    from backlog_harness import native_evidence

    @dataclass
    class Binding:
        profile_name: str = "worker"
        permission_digest: str = "permissions"

    acceptance = {"session": {"session_id": "owner", "native_session_id": "owner-native"}}
    decision = {"prepared": True}
    selected = {
        "provider_revision": "revision",
        "preparation_digest": digest(decision),
        "canonical_reference": "item#design",
        "acceptance_text": "Review proposed layout before source changes",
    }

    class App:
        root = tmp_path
        config = SimpleNamespace(
            binding=lambda _: Binding(), data={"profiles": {"worker": {"artifact_output": True}}}
        )
        head = "base"

        def __init__(self):
            self.calls = []
            self.reviews = {}

        reject_first = False
        reject_all = False
        invalid_review = None
        interrupt_once = False
        mutate_source = False

        def item_workflow(self, _):
            return {"design_review": selected}

        def _stage_path(self, item, stage):
            return tmp_path / "stages" / (stage + ".json")

        def candidate_repository(self, _):
            return tmp_path

        def result_json(self, result):
            return json.loads(result["text"])

        def validate_invocation_result(self, result):
            assert result["invocation_id"]

        def native_sessions_root(self, _):
            return tmp_path

        async def enforce_guard(self, _):
            pass

        async def invoke(self, item, stage, role, prompt, **kwargs):
            path = self._stage_path(item, stage)
            if path.exists():
                return json.loads(path.read_text())
            assert kwargs == {"read_only": False, "purpose": "proof"}
            self.calls.append(stage)
            request = json.loads(self._stage_path(item, "design-request").read_text())
            operation = item + ":" + stage
            invocation = "design-" + str(len(self.calls))
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
            design = output / "design.md"
            design.write_text("Proposed readable layout " + str(len(self.calls)))
            artifact = {"path": str(design), "sha256": sha256(design.read_bytes()).hexdigest()}
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
                "candidate": "base",
                "status": "evidence-ready",
                "artifacts": [artifact],
                "design": artifact,
                "reviewer_session": invocation + "-review",
            }
            result = {
                "invocation_id": invocation,
                "binding": {"profile_name": "worker", "permission_digest": "permissions"},
                "role": role,
                "purpose": "proof",
                "session": {"session_id": invocation, "native_session_id": invocation},
                "evidence_path": str(evidence),
                "text": json.dumps(value),
            }
            rejection = self.reject_all or (self.reject_first and len(self.calls) == 1)
            review = {
                "candidate": "base",
                "verdict": "REJECT" if rejection else "ACCEPT",
                "unresolved_findings": ["Unreadable column"] if rejection else [],
                "design_digest": artifact["sha256"],
                "request_digest": digest(request),
            }
            if self.invalid_review:
                review[self.invalid_review] = "wrong"
            self.reviews[invocation + "-review"] = review
            atomic_json(path, result)
            if self.mutate_source:
                self.head = "unauthorized-code"
            if self.interrupt_once:
                self.interrupt_once = False
                raise RuntimeError("interrupted after persisted result")
            return result

    app = App()
    atomic_json(app._stage_path("item", "accept"), acceptance)
    atomic_json(
        app._stage_path("item", "preparation"),
        {"item": {"revision": "revision"}, "decision": decision},
    )
    monkeypatch.setattr(
        recovery_flow, "git", lambda _, *args: app.head if args[0] == "rev-parse" else ""
    )

    def native(producer, reviewer, candidate, *_args, **kwargs):
        assert kwargs == {"accepted_verdicts": ("ACCEPT", "REJECT")}
        assert producer != reviewer
        return app.reviews[reviewer]

    monkeypatch.setattr(native_evidence, "verify_native_review", native)
    return app, acceptance, selected


def test_design_acceptance_precedes_code_and_replay(design_app):
    app, acceptance, _ = design_app
    with pytest.raises(TransitionBlocked):
        recovery_flow.verify_design_acceptance(app, "item", "base")
    handoff = asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    assert handoff["design"]["sha256"] and len(app.calls) == 1
    before = {str(p): p.read_bytes() for p in app.root.rglob("*.json")}
    assert (
        asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
        == handoff
    )
    app.head = "produced-candidate"
    assert recovery_flow.verify_design_acceptance(app, "item", "base") == handoff
    assert len(app.calls) == 1 and before == {
        str(p): p.read_bytes() for p in app.root.rglob("*.json")
    }
    with pytest.raises(TransitionBlocked):
        recovery_flow.verify_design_acceptance(app, "item", "other-base")


def test_design_rejection_permits_only_bounded_correction(design_app):
    app, acceptance, _ = design_app
    app.reject_first = True
    handoff = asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    assert len(app.calls) == 2 and handoff["attempt"] == 2
    assert (
        asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
        == handoff
    )
    assert len(app.calls) == 2


@pytest.mark.parametrize("field", ["request_digest", "design_digest", "candidate"])
def test_invalid_design_evidence_does_not_buy_another_attempt(design_app, field):
    app, acceptance, _ = design_app
    app.invalid_review = field
    with pytest.raises(TransitionBlocked):
        asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    assert len(app.calls) == 1


def test_design_interruption_reuses_result(design_app):
    app, acceptance, _ = design_app
    app.interrupt_once = True
    with pytest.raises(RuntimeError):
        asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    assert len(app.calls) == 1


def test_source_edit_before_design_acceptance_blocks(design_app):
    app, acceptance, _ = design_app
    app.mutate_source = True
    with pytest.raises(TransitionBlocked):
        asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    assert not app._stage_path("item", "design-acceptance").exists()


@pytest.mark.parametrize("fault", ["tamper", "revision", "decision"])
def test_design_stale_or_tampered_evidence_blocks(design_app, fault):
    app, acceptance, selected = design_app
    asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    if fault == "tamper":
        next(app.root.rglob("design.md")).write_text("altered")
    else:
        selected["provider_revision" if fault == "revision" else "preparation_digest"] = "changed"
    with pytest.raises(TransitionBlocked):
        recovery_flow.verify_design_acceptance(app, "item", "base")


@pytest.mark.parametrize(
    "optional,findings,allowed",
    [(False, ["bad layout"], False), (True, [], False), (True, ["bad layout"], True)],
)
def test_native_rejection_only_explicit_design_caller(monkeypatch, optional, findings, allowed):
    from backlog_harness import native_evidence

    parent = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "c",
                "arguments": json.dumps({"fork_turns": "none"}),
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c", "output": "reviewer"},
        },
    ]
    child = [
        {
            "type": "session_meta",
            "payload": {
                "id": "reviewer",
                "source": {"subagent": "producer"},
                "git": {"commit_hash": "base"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": json.dumps(
                    {"candidate": "base", "verdict": "REJECT", "unresolved_findings": findings}
                ),
            },
        },
    ]
    monkeypatch.setattr(
        native_evidence,
        "native_records",
        lambda identity, *_: (parent if identity == "producer" else child, "hash"),
    )
    kwargs = {"accepted_verdicts": ("ACCEPT", "REJECT")} if optional else {}
    if allowed:
        assert (
            native_evidence.verify_native_review("producer", "reviewer", "base", **kwargs)[
                "verdict"
            ]
            == "REJECT"
        )
    else:
        with pytest.raises(TransitionBlocked):
            native_evidence.verify_native_review("producer", "reviewer", "base", **kwargs)
    with pytest.raises(TransitionBlocked, match="Distinct"):
        native_evidence.verify_native_review("producer", "producer", "base", **kwargs)


def test_source_invocation_cannot_bypass_design(config_file, monkeypatch):
    from backlog_harness import estimation
    from backlog_harness.application import Application
    from backlog_harness.contracts import load_config

    path, _ = config_file
    config = load_config(path)
    workflow = {"design_review": {"required": True}}
    app = SimpleNamespace(
        config=config,
        config_path=path,
        root=config.operational_root,
        recovery_record=lambda _: None,
        item_workflow=lambda _: workflow,
    )
    app._stage_path = lambda item, stage: app.root / "stages" / (stage + ".json")
    atomic_json(app._stage_path("item", "assignment"), {"workflow": workflow})
    monkeypatch.setattr(estimation, "prepared_workflow", lambda *_: workflow)
    with pytest.raises(TransitionBlocked, match="before source implementation"):
        asyncio.run(
            Application._invoke(
                app, "item", "produce-review", "orchestrator", "write source", read_only=False
            )
        )
    assert not (app.root / "runs").exists()


def test_two_rejections_exhaust_without_reexecution(design_app):
    app, acceptance, _ = design_app
    app.reject_all = True
    for _ in range(2):
        with pytest.raises(TransitionBlocked, match="both bounded attempts"):
            asyncio.run(recovery_flow.ensure_design_acceptance(app, "item", "base", acceptance))
    assert len(app.calls) == 2
    assert not app._stage_path("item", "design-acceptance").exists()
