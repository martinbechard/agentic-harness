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


def configured_workflow(config, item_id):
    from .contracts import plain

    selected = plain(config.data["workflow"])
    selected.update(selected.get("items", {}).get(item_id, {}))
    selected.pop("items", None)
    return selected


def prepared_workflow(app, item_id, config):
    """Resolve bounded item parameters without changing runtime or delivery authority."""
    from .contracts import digest, safe_source_path

    selected = configured_workflow(config, item_id)
    path = app._stage_path(item_id, "preparation")
    if not path.exists():
        selected.pop("preparation", None)
        return selected
    saved = json.loads(path.read_text())
    require(
        saved["workflow_config_digest"] == digest(selected),
        "Prepared workflow configuration changed; reconcile before dispatch",
    )
    decision = saved["decision"]
    app.validate_invocation_result(decision)
    validate_preparation_invocation(decision, saved["invocation_config_digest"], app.root)
    value = app.result_json(decision)
    require(
        decision.get("role") == "coordinator"
        and value.get("item_id") == item_id
        and value.get("provider_revision") == saved["item"]["revision"],
        "Preparation identity differs",
    )
    refusal = value.get("blocked") or (
        (value.get("blockers") or value.get("blocker") or "Coordinator reported blocked")
        if value.get("status") == "blocked"
        else None
    )
    require(not refusal, "Preparation blocked: " + json.dumps(refusal))
    parameters = value.get("workflow", {})
    echoes = {
        "persistence": config.data["provider"],
        "completion": selected["completion"],
        "canonical_primary_branch": selected["primary_branch"],
    }
    require(
        all(parameters[key] == expected for key, expected in echoes.items() if key in parameters),
        "Preparation echoed a different workflow authority",
    )
    unsupported = set(parameters) - {"allowed_paths", "checks", *echoes}
    require(
        not unsupported and {"allowed_paths", "checks"} <= set(parameters),
        "Preparation contains unsupported workflow fields: " + ", ".join(sorted(unsupported)),
    )

    policy = selected.get("preparation", {})
    paths = parameters["allowed_paths"]
    require(
        isinstance(paths, list) and paths and len(paths) == len(set(paths)),
        "Preparation needs unique exact source paths",
    )
    roots = policy.get("allowed_roots", [])
    for name in paths:
        safe_source_path(name)
        require(
            any(name == root or name.startswith(root + "/") for root in roots),
            "Prepared source path exceeds configured authority",
        )
        source = config.repository / name
        require(
            source.resolve().is_relative_to(config.repository) and not source.is_symlink(),
            "Prepared source path escapes repository",
        )
    checks = parameters["checks"]
    require(isinstance(checks, list) and checks, "Preparation needs verification commands")
    catalog = policy.get("check_commands", [])
    require(
        all(argv in catalog for argv in checks),
        "Prepared check exceeds configured command authority",
    )
    # Configured workflow checks remain mandatory even when the agent selects focused checks.
    selected.pop("preparation", None)
    selected["allowed_paths"] = paths
    selected["checks"] = selected["checks"] + [
        argv for argv in checks if argv not in selected["checks"]
    ]
    return selected


def validate_preparation_invocation(decision, config_digest, root):
    """The durable intent, not the result envelope, owns configuration attribution."""
    evidence = Path(decision["evidence_path"]).resolve()
    require(evidence.is_relative_to(root.resolve()), "Preparation intent escapes evidence root")
    intent = json.loads((evidence / "intent.json").read_text())
    require(
        intent.get("invocation_id") == decision.get("invocation_id")
        and intent.get("request_digest") == decision.get("request_digest")
        and intent.get("binding") == decision.get("binding")
        and intent.get("config_digest") == config_digest,
        "Preparation invocation configuration differs",
    )


async def prepare_item(app, item):
    """Prepare an unreserved Ready item once; caller owns item and SOLO locks."""
    from hashlib import sha256

    from .contracts import digest, load_config, plain

    current = load_config(app.config_path)
    require(
        current.repository == app.config.repository and current.operational_root == app.root,
        "Preparation storage identity changed",
    )
    app.config = current
    policy = current.data["workflow"].get("preparation")
    if not policy or item.state != "Ready" or app._stage_path(item.item_id, "assignment").exists():
        return item
    path = app._stage_path(item.item_id, "preparation")
    if not path.exists():
        decision = await app.invoke(
            item.item_id,
            "prepare",
            "coordinator",
            "Prepare this selected canonical Ready item using its COMPLETE content and applicable "
            "PROJECT.yaml, AGENTS.md, repository skills and source authority. Read only; do not "
            "implement, mutate, delegate, or invoke claim operations. Use estimate-agent-work "
            "for a prospective estimate when original_high is unknown. Historical costs remain "
            "unknown. Use the existing main-branch workflow; return exact item paths within the "
            "configured roots and focused check argv selected from the command catalog. Preserve "
            "all required independent review, acceptance and delivery gates. If authority or "
            "verification cannot be established, return blocked with the exact actionable reason. "
            "Return JSON {item_id,provider_revision,workflow:{allowed_paths:[],checks:[]},"
            "historical_original_high:null,historical_usage:unknown,prospective_high:integer,"
            "estimate:{kind:prospective_pre_execution,dated_at:ISO_date,generated_tokens:{low,high},"
            "...estimate-agent-work fields},authority_evidence:[{path,sha256,reason}]}. "
            "Do not infer approval from Ready alone when canonical content contains an explicit hold. "
            "Provider revision is SHA256(path UTF-8 + NUL + content UTF-8), not raw content SHA256. "
            "Configured bounds: "
            + json.dumps(plain(policy))
            + "\nCanonical item: "
            + json.dumps(asdict(item)),
            purpose="provider",
        )
        app.validate_invocation_result(decision)
        validate_preparation_invocation(decision, current.file_digest, app.root)
        require(
            load_config(app.config_path).file_digest == current.file_digest,
            "Preparation configuration changed during invocation",
        )
        app.validate_call_limits(decision, current.data["coordinator_limits"])
        atomic_json(
            path,
            {
                "item": asdict(item),
                "decision": decision,
                "workflow_config_digest": digest(configured_workflow(current, item.item_id)),
                "invocation_config_digest": current.file_digest,
            },
            exclusive=True,
        )
    saved = json.loads(path.read_text())
    # Validate before any provider effect, including replay of retained preparation.
    prepared_workflow(app, item.item_id, current)
    authority = app.result_json(saved["decision"]).get("authority_evidence")
    require(
        isinstance(authority, list) and bool(authority),
        "Preparation lacks supporting authority evidence",
    )
    for evidence in authority:
        source = current.repository / evidence["path"]
        require(
            not Path(evidence["path"]).is_absolute()
            and source.resolve().is_relative_to(current.repository)
            and source.is_file()
            and bool(evidence.get("reason")),
            "Preparation authority source is invalid",
        )
        require(
            sha256(source.read_bytes()).hexdigest() == evidence.get("sha256"),
            "Preparation authority source changed",
        )
    if not app._stage_path(item.item_id, "estimate-input").exists():
        require(asdict(item) == saved["item"], "Prepared item changed before provider effect")
    if (
        item.original_high is None
        and not app._stage_path(item.item_id, "prospective-estimate").exists()
    ):
        decision_path = app._stage_path(item.item_id, "estimate-decision")
        atomic_json(decision_path, saved["decision"])
        await _record_estimate(app, item.item_id, decision_path)
        item = app.provider.item(item.item_id)
    if app._stage_path(item.item_id, "prospective-estimate").exists():
        app.assignment_estimate(item)
    else:
        require(asdict(item) == saved["item"], "Prepared item changed before admission")
    return item
