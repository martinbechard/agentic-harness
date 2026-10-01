"""Prepare a graph-owned hold assessment; never mutate provider lifecycle state.

The graph caller owns the item lock and checkpoints the returned release packet
before invoking the existing provider transition. Receipts record evidence and
allowances, not a second workflow position.
"""

import json
import math

from .contracts import digest
from .evidence import atomic_json
from .workflow import require, validate_transition


def _measurement(usage):
    # A saved allowance changes these derived fields, not the measured evidence.
    return {
        key: value
        for key, value in usage.items()
        if key
        not in {
            "status",
            "ceiling",
            "may_generate",
            "overshoot",
        }
    }


def _known(usage):
    return (
        usage.get("status") in {"below", "crossed"}
        and usage.get("coverage") != "incomplete"
        and type(usage.get("generated_tokens")) is int
        and usage["generated_tokens"] >= 0
        and type(usage.get("original_high")) is int
        and usage["original_high"] > 0
    )


async def prepare_graph_hold_review(app, item_id):
    """Assess once per incident/measurement; preserve unknown usage and original baseline."""
    item = app.provider.item(item_id)
    require(item.state == "Holding", "Item is not Holding")
    require(app.item_quiescent(item_id), "Item execution is not proven quiescent")
    incidents = [
        (json.loads(path.read_text()), path)
        for path in app._stage_path(item_id, "unused").parent.glob("incident-*.json")
    ]
    require(incidents, "Guard incident is missing")
    incident, _ = max(incidents, key=lambda entry: entry[0]["at"])
    require(
        incident.get("item_id") == item_id and incident.get("owner") == item.owner,
        "Guard incident differs from the retained item owner",
    )
    require(incident.get("resume_state") in {"Starting", "Running"}, "Invalid hold resume state")
    usage = app.usage_view(item_id)
    multiplier = app.config.data.get("generation_guard_multiplier", 2.0)
    require(
        type(multiplier) in {int, float} and math.isfinite(multiplier) and multiplier >= 1,
        "Invalid generation guard multiplier",
    )
    evidence = {
        "item_id": item_id,
        "owner": item.owner,
        "incident": incident,
        "measurement": _measurement(usage),
        "accounting_known": _known(usage),
        "configuration_fence": app.config.data.get("generation_configuration_error"),
        "guard_multiplier": multiplier,
    }
    stage = "graph-hold-review-" + digest(evidence)
    request_path = app._stage_path(item_id, stage + "-request")
    request = {**evidence, "revision": item.revision, "usage": usage}
    if request_path.exists():
        request = json.loads(request_path.read_text())
        require(
            request.get("revision") == item.revision
            and all(request.get(key) == value for key, value in evidence.items()),
            "Retained hold assessment identity or revision changed",
        )
    else:
        atomic_json(request_path, request, exclusive=True)
    result_path = app._stage_path(item_id, stage + "-result")
    if result_path.exists():
        result = json.loads(result_path.read_text())
    else:
        result = await app.invoke(
            item_id,
            stage,
            "coordinator",
            "Assess this generation guard incident within the already authorized task. "
            "Do not implement, delegate, mutate files or request a blanket new human approval. "
            "Explain the cause of the overrun or unknown usage and a revised approach. "
            "Recommend resume, rethink or escalate; resume needs a positive integer remaining_high "
            "estimate for remaining generated tokens. Preserve the original estimate and measured "
            "usage; unknown or incomplete accounting cannot authorize release. No preselected "
            "ceiling is supplied. Return only JSON {item_id,revision,retained_owner,operation:"
            "resume|rethink|escalate,cause,revised_approach,remaining_high}.\n"
            + json.dumps(request, sort_keys=True),
        )
        atomic_json(result_path, result, exclusive=True)
    app.validate_invocation_result(result)
    require(result.get("role") == "coordinator", "Hold assessment actor is not Coordinator")
    app.validate_call_limits(result, app.config.data["administrative_review_limits"])
    counters = [
        event["usage"].get("output_tokens") for event in result["events"] if event.get("usage")
    ]
    require(
        counters
        and type(counters[-1]) is int
        and 0
        <= counters[-1]
        <= app.config.data["administrative_review_limits"]["generated_tokens"],
        "Administrative assessment usage is missing or exceeds its budget",
    )
    value = app.result_json(result)
    require(
        value.get("item_id") == item_id
        and value.get("revision") == request["revision"]
        and value.get("retained_owner") == item.owner,
        "Coordinator assessment differs from retained owner or revision",
    )
    require(
        value.get("operation") in {"resume", "rethink", "escalate"}, "Invalid assessment operation"
    )
    for key in ("cause", "revised_approach"):
        require(
            isinstance(value.get(key), str) and bool(value[key].strip()),
            "Assessment requires " + key,
        )
    if value["operation"] == "resume":
        require(
            type(value.get("remaining_high")) is int and value["remaining_high"] > 0,
            "Resume requires a positive remaining-work estimate",
        )
    current = app.provider.item(item_id)
    require(
        current.revision == item.revision
        and current.owner == item.owner
        and current.state == "Holding",
        "Item changed during hold assessment",
    )
    observed = app.usage_view(item_id)
    require(_measurement(observed) == evidence["measurement"], "Usage changed during assessment")
    require(
        _known(observed) == evidence["accounting_known"],
        "Accounting coverage changed during assessment",
    )
    require(app.item_quiescent(item_id), "Item became active during assessment")
    decision = {
        "assessment": value,
        "invocation_id": result["invocation_id"],
        "request_digest": digest(request),
        "evidence_path": result.get("evidence_path"),
        "usage": request["usage"],
        "original_high": request["usage"].get("original_high"),
    }
    if (
        value["operation"] != "resume"
        or not _known(observed)
        or evidence["configuration_fence"]
        or app.config.data.get("generation_configuration_error")
    ):
        packet = {"held": True, "decision": {**decision, "release_permitted": False}}
    else:
        ceiling = observed["generated_tokens"] + value["remaining_high"] * multiplier
        require(math.isfinite(ceiling), "Prospective allowance is not finite")
        allowance = {
            "ceiling": ceiling,
            "original_high": observed["original_high"],
            "decision": result["invocation_id"],
            "reference": stage,
            "measured_at_review": observed["generated_tokens"],
            "remaining_high": value["remaining_high"],
            "guard_multiplier": multiplier,
        }
        # Existing usage_view preserves the historical baseline with max(original guard,
        # allowance). Neither the estimate nor any recorded consumption is reset.
        effective_ceiling = max(observed["original_high"] * multiplier, ceiling)
        release_usage = {
            **observed,
            "status": "below",
            "ceiling": effective_ceiling,
            "may_generate": True,
            "overshoot": 0,
        }
        authority = app.authority(
            result,
            item_id,
            operation="resume",
            retained_owner=item.owner,
            usage=release_usage,
            approval=allowance,
        )
        validate_transition(current, incident["resume_state"], authority)
        packet = {
            "revision": item.revision,
            "target": incident["resume_state"],
            "authority": authority,
            "decision": {**decision, "allowance": allowance},
        }
    decision_path = app._stage_path(item_id, stage + "-decision")
    if decision_path.exists():
        require(json.loads(decision_path.read_text()) == packet, "Retained hold decision changed")
    else:
        atomic_json(decision_path, packet, exclusive=True)
    if not packet.get("held"):
        atomic_json(app._stage_path(item_id, "allowance"), packet["decision"]["allowance"])
    return packet
