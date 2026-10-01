"""Graph preparation with real hold-release validator; scripted Coordinator evidence."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from backlog_harness.application import Application
from backlog_harness.evidence import atomic_json
from backlog_harness.graph_hold import prepare_graph_hold_review
from backlog_harness.provider import TransitionBlocked
from backlog_harness.workflow import require, validate_transition


class App:
    validate_call_limits = staticmethod(Application.validate_call_limits)

    def __init__(self, root):
        self.root = root
        self.item = SimpleNamespace(
            item_id="item", state="Holding", revision="revision", owner="worker", original_high=100
        )
        self.provider = SimpleNamespace(item=lambda _: self.item)
        self.config = SimpleNamespace(
            data={
                "generation_guard_multiplier": 2.0,
                "administrative_review_limits": {"turns": 1, "generated_tokens": 100},
            }
        )
        self.usage = {
            "status": "crossed",
            "generated_tokens": 220,
            "original_high": 100,
            "ceiling": 200,
            "may_generate": False,
            "overshoot": 20,
        }
        self.value = {
            "item_id": "item",
            "revision": "revision",
            "retained_owner": "worker",
            "operation": "resume",
            "cause": "Unexpected failing integration check",
            "revised_approach": "Correct the isolated fixture and rerun its check",
            "remaining_high": 30,
        }
        self.calls = 0
        self.quiescent = True
        self.valid = True
        self.after_invoke = None
        atomic_json(
            self._stage_path("item", "incident-guard"),
            {
                "incident": "usage_limit",
                "item_id": "item",
                "owner": "worker",
                "resume_state": "Running",
                "usage": self.usage,
                "at": "2026-10-01T00:00:00Z",
            },
        )

    def _stage_path(self, item, stage):
        return self.root / (stage + ".json")

    def item_quiescent(self, item):
        return self.quiescent

    def usage_view(self, item):
        value = dict(self.usage)
        path = self._stage_path(item, "allowance")
        if path.exists() and value["generated_tokens"] is not None:
            allowance = json.loads(path.read_text())
            value["ceiling"] = max(200, allowance["ceiling"])
            value["may_generate"] = value["generated_tokens"] < value["ceiling"]
            value["status"] = "below" if value["may_generate"] else "crossed"
            value["overshoot"] = max(0, value["generated_tokens"] - value["ceiling"])
        return value

    async def invoke(self, item, stage, role, prompt):
        self.calls += 1
        self.prompt = prompt
        if self.after_invoke:
            self.after_invoke()
        return {
            "invocation_id": "review-1",
            "role": "coordinator",
            "outcome": "returned",
            "events": [{"type": "turn.completed", "usage": {"output_tokens": 25}}],
            "text": json.dumps(self.value),
            "evidence_path": str(self.root / "native"),
        }

    def validate_invocation_result(self, result):
        require(self.valid and result["outcome"] == "returned", "Invalid native result")

    def result_json(self, result):
        return json.loads(result["text"])

    def authority(self, result, item, **facts):
        return {
            "role": result["role"],
            "invocation_id": result["invocation_id"],
            "observed_result": True,
            "item_id": item,
            **facts,
        }


def run(app):
    return asyncio.run(prepare_graph_hold_review(app, "item"))


def test_known_resume_preserves_baseline_and_returns_valid_transition(tmp_path):
    app = App(tmp_path)
    packet = run(app)
    assert packet["target"] == "Running" and packet["revision"] == "revision"
    validate_transition(app.item, packet["target"], packet["authority"])
    allowance = packet["decision"]["allowance"]
    assert allowance["ceiling"] == 280 and allowance["original_high"] == 100
    assert allowance["remaining_high"] == 30 and allowance["measured_at_review"] == 220
    assert app.item.state == "Holding"  # graph must checkpoint, then transition separately
    assert "requested_ceiling" not in app.prompt and "operator_reference" not in app.prompt


def test_replay_uses_immutable_result_without_invoke(tmp_path):
    app = App(tmp_path)
    first = run(app)
    assert run(app) == first
    assert app.calls == 1
    # Recover crash after immutable decision persisted but allowance projection was lost.
    app._stage_path("item", "allowance").unlink()
    assert run(app) == first and app.calls == 1


@pytest.mark.parametrize("coverage", ["unknown", "incomplete"])
def test_unknown_usage_assessed_but_never_released(tmp_path, coverage):
    app = App(tmp_path)
    app.usage.update(
        status="unknown", generated_tokens=None, coverage=coverage, generated_tokens_lower_bound=220
    )
    packet = run(app)
    assert packet["held"] and not packet["decision"]["release_permitted"]
    assert packet["decision"]["usage"]["generated_tokens"] is None
    assert not app._stage_path("item", "allowance").exists()
    assert run(app) == packet and app.calls == 1


@pytest.mark.parametrize("operation", ["rethink", "escalate"])
def test_nonresume_decisions_are_checkpointable_holds(tmp_path, operation):
    app = App(tmp_path)
    app.value["operation"] = operation
    app.value.pop("remaining_high")
    assert run(app)["held"] is True
    assert not app._stage_path("item", "allowance").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("remaining_high", 0),
        ("remaining_high", True),
        ("remaining_high", 1.2),
        ("remaining_high", None),
        ("retained_owner", "other"),
        ("revision", "stale"),
        ("cause", " "),
        ("revised_approach", ""),
        ("operation", "approve"),
    ],
)
def test_invalid_assessment_blocks_release(tmp_path, field, value):
    app = App(tmp_path)
    app.value[field] = value
    with pytest.raises(TransitionBlocked):
        run(app)
    assert not app._stage_path("item", "allowance").exists()


def test_owner_or_usage_change_during_review_cannot_release(tmp_path):
    app = App(tmp_path)
    app.after_invoke = lambda: setattr(app.item, "owner", "other")
    with pytest.raises(TransitionBlocked):
        run(app)
    assert not app._stage_path("item", "allowance").exists()


def test_invalid_native_evidence_rejected_on_replay(tmp_path):
    app = App(tmp_path)
    run(app)
    app.valid = False
    with pytest.raises(TransitionBlocked, match="native"):
        run(app)
    assert app.calls == 1


@pytest.mark.parametrize("status", ["configuration_invalid", "crossed"])
def test_configuration_fence_is_never_converted_to_release(tmp_path, status):
    app = App(tmp_path)
    app.usage["status"] = status
    app.config.data["generation_configuration_error"] = "native configuration is invalid"
    packet = run(app)
    assert packet["held"] is True
    assert packet["decision"]["release_permitted"] is False
    assert not app._stage_path("item", "allowance").exists()
    assert run(app) == packet and app.calls == 1


def test_configuration_invalid_status_alone_cannot_release(tmp_path):
    app = App(tmp_path)
    app.usage["status"] = "configuration_invalid"
    assert run(app)["held"] is True
    assert not app._stage_path("item", "allowance").exists()
