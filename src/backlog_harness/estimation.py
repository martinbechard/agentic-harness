"""Record a Coordinator's prospective estimate without rewriting historical baselines."""

import json
from dataclasses import asdict
from pathlib import Path

from .evidence import async_operation_lock, atomic_json, component, operation_lock
from .provider import AgentProvider, Item
from .workflow import require, validate_transition


async def record_estimate(app, item_id, decision_path):
    async with async_operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        with operation_lock(app.root / "solo-execution.lock"):
            return await _record_estimate(app, item_id, decision_path)


async def _record_estimate(app, item_id, decision_path):
    require(isinstance(app.provider, AgentProvider), "Estimate recording requires agent management")
    decision = json.loads(Path(decision_path).read_text())
    app.validate_invocation_result(decision)
    value = app.result_json(decision)
    input_path = app._stage_path(item_id, "estimate-input")
    if input_path.exists():
        saved = json.loads(input_path.read_text())
        require(saved["decision"] == decision, "Estimate input changed")
        item = Item(**saved["item"])
    else:
        item = app.provider.item(item_id)
    require(
        decision.get("role") == "coordinator"
        and value.get("item_id") == item_id
        and value.get("provider_revision") == item.revision
        and value.get("historical_original_high") is None
        and value.get("historical_usage") == "unknown"
        and value.get("estimate", {}).get("kind") == "prospective_pre_execution",
        "Prospective estimate must bind the current item and preserve historical unknowns",
    )
    require(
        value.get("prospective_high") == value["estimate"].get("generated_tokens", {}).get("high"),
        "Prospective high differs from the estimate",
    )
    if not input_path.exists():
        atomic_json(input_path, {"item": asdict(item), "decision": decision}, exclusive=True)
    path = app._stage_path(item_id, "estimate-decision")
    if path.exists():
        require(json.loads(path.read_text()) == decision, "Estimate decision changed")
    else:
        atomic_json(path, decision, exclusive=True)
    receipt = await app.transition(
        item_id,
        item.revision,
        "Ready",
        app.authority(
            decision,
            item_id,
            operation="record-estimate",
            estimate=value["estimate"],
            prospective_high=value.get("prospective_high"),
        ),
        validate=validate_transition,
    )
    atomic_json(
        app._stage_path(item_id, "prospective-estimate"),
        {
            "provider_revision": receipt["after"]["revision"],
            "prospective_high": value["prospective_high"],
            "historical_original_high": None,
            "historical_usage": "unknown",
            "receipt": receipt,
        },
    )
    return receipt
