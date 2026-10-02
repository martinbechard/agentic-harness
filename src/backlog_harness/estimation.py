"""Record a Coordinator's prospective estimate without rewriting historical baselines."""

import json
from dataclasses import asdict
from pathlib import Path

from .evidence import async_operation_lock, atomic_json, component, operation_lock
from .provider import AgentProvider, Item, TransitionBlocked
from .workflow import require, validate_transition

PREPARATION_METADATA_FIELDS = {
    "check_status",
    "checks_executed",
    "required_gates",
    "scope_conditions",
    "implementation_constraints",
    "verification_limit",
}

PREPARATION_RUNTIME_OBLIGATIONS = {
    "reservation_acceptance",
    "bounded_scope",
    "authority_analysis",
    "independent_review",
    "configured_verification",
    "main_branch_delivery",
    "provider_completion",
}

PREPARATION_WORKFLOW_SCHEMA = (
    "The workflow object is closed. It requires allowed_paths (a nonempty list of exact source "
    "paths) and checks (a nonempty list selected verbatim from the configured command catalog). "
    "It may echo only persistence, completion and canonical_primary_branch with their configured "
    "values. Optional retained evidence fields are check_status (nonempty string), "
    "checks_executed (exactly false), scope_conditions (nonempty-string list), "
    "implementation_constraints (nonempty-string list), required_gates (nonempty-string list or "
    "{gate,requirement} objects with nonempty strings), and verification_limit (nonempty string). "
    "Do not return any other workflow field. Put ordinary obligations already enforced by the "
    "harness, including reservation and acceptance, bounded scope and authority analysis, "
    "independent review, configured verification, main-branch delivery and provider completion, "
    "verbatim in implementation_constraints. Put only additional acceptance proof, design or "
    "approval obligations that the configured workflow cannot enforce in required_gates; those "
    "remain blocking unless an existing explicit proof or design selection covers them."
)

PREPARATION_GATES_SCHEMA_ERROR = "Preparation contains unsupported workflow fields: gates"


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


def available_preparation_obligations(selected, config):
    """Name only runtime obligations that the selected workflow already enforces."""
    available = {"reservation_acceptance", "bounded_scope", "authority_analysis"}
    if selected.get("checks"):
        available.add("configured_verification")
    if selected.get("completion") == "main-branch":
        available.update({"independent_review", "main_branch_delivery"})
    if config.data.get("provider") == "file":
        available.add("provider_completion")
    return available


def effective_preparation_decision(app, item_id, saved, selected, config):
    """Resolve and validate an optional linked correction without replacing original evidence."""
    from .contracts import digest

    original = saved["decision"]
    path = app._stage_path(item_id, "preparation-contract-resolution")
    if not path.exists():
        return original
    require(not path.is_symlink(), "Preparation correction resolution cannot be symlinked")
    correction_path = app._stage_path(item_id, "preparation-contract-correction")
    require(
        correction_path.is_file() and not correction_path.is_symlink(),
        "Preparation correction result is missing",
    )
    corrected = json.loads(correction_path.read_text())
    resolution = json.loads(path.read_text())
    require(
        isinstance(resolution, dict)
        and resolution.get("schema_error") == PREPARATION_GATES_SCHEMA_ERROR,
        "Preparation correction schema error differs",
    )
    require(
        resolution
        == {
            "original_decision_digest": digest(original),
            "corrected_decision_digest": digest(corrected),
            "original_invocation_id": original.get("invocation_id"),
            "corrected_invocation_id": corrected.get("invocation_id"),
            "schema_error": resolution.get("schema_error"),
        },
        "Preparation correction resolution differs",
    )
    app.validate_invocation_result(corrected)
    validate_preparation_invocation(corrected, saved["invocation_config_digest"], app.root)
    require(
        original.get("role") == corrected.get("role") == "coordinator"
        and original.get("binding") == corrected.get("binding"),
        "Preparation correction changed Coordinator authority",
    )
    validate_preparation_correction(
        app.result_json(original),
        app.result_json(corrected),
        digest(original),
        resolution["schema_error"],
        available_preparation_obligations(selected, config),
    )
    return corrected


def validate_preparation_scope(parameters, selected, config):
    """Validate exact path and command selections against configured preparation authority."""
    from .contracts import safe_source_path

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
    return paths, checks


def prepared_workflow(app, item_id, config):
    """Resolve bounded item parameters without changing runtime or delivery authority."""
    from .contracts import digest

    selected = configured_workflow(config, item_id)
    path = app._stage_path(item_id, "preparation")
    if not path.exists():
        require(
            not selected.get("proof_requirements"),
            "Explicit proof selection requires retained preparation evidence",
        )
        require(
            not selected.get("design_review"),
            "Explicit design selection requires retained preparation evidence",
        )
        selected.pop("preparation", None)
        return selected
    saved = json.loads(path.read_text())
    proof_selection = selected.get("proof_requirements")
    design_selection = selected.get("design_review")
    original_selection = {
        key: value
        for key, value in selected.items()
        if key not in {"proof_requirements", "candidate_approval_required", "design_review"}
    }
    require(
        saved["workflow_config_digest"] == digest(selected)
        or (
            bool(proof_selection or design_selection)
            and saved["workflow_config_digest"] == digest(original_selection)
        ),
        "Prepared workflow configuration changed; reconcile before dispatch",
    )
    original_decision = saved["decision"]
    app.validate_invocation_result(original_decision)
    validate_preparation_invocation(original_decision, saved["invocation_config_digest"], app.root)
    decision = effective_preparation_decision(app, item_id, saved, selected, config)
    value = app.result_json(decision)
    require(
        decision.get("role") == "coordinator"
        and value.get("item_id") == item_id
        and value.get("provider_revision") == saved["item"]["revision"],
        "Preparation identity differs",
    )
    if proof_selection:
        require(
            isinstance(proof_selection, dict)
            and proof_selection.get("provider_revision") == saved["item"]["revision"]
            and proof_selection.get("preparation_digest") == digest(decision)
            and isinstance(proof_selection.get("requirements"), list)
            and bool(proof_selection["requirements"]),
            "Explicit proof selection differs from retained preparation authority",
        )
    if design_selection:
        require(
            isinstance(design_selection, dict)
            and design_selection.get("provider_revision") == saved["item"]["revision"]
            and design_selection.get("preparation_digest") == digest(decision),
            "Explicit design selection differs from retained preparation authority",
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
    parameters, metadata = normalize_preparation_workflow(parameters, echoes)
    # A reviewed operator selection names the supported requirements for this
    # exact packet. Free-form gates themselves never select an execution route.
    require(
        not metadata.get("required_gates") or bool(proof_selection or design_selection),
        "Preparation requires unsupported acceptance gates before dispatch: "
        + json.dumps(metadata.get("required_gates", [])),
    )

    paths, checks = validate_preparation_scope(parameters, selected, config)
    # Configured workflow checks remain mandatory even when the agent selects focused checks.
    selected.pop("preparation", None)
    selected["allowed_paths"] = paths
    selected["checks"] = selected["checks"] + [
        argv for argv in checks if argv not in selected["checks"]
    ]
    if metadata or proof_selection or design_selection:
        selected["preparation_evidence"] = {
            "item_id": item_id,
            "provider_revision": saved["item"]["revision"],
            "decision_digest": digest(decision),
            "metadata": metadata,
        }
        if decision != original_decision:
            selected["preparation_evidence"]["original_decision_digest"] = digest(original_decision)
    return selected


def normalize_preparation_workflow(parameters, echoes):
    """Separate bounded executable parameters from retained Coordinator commentary."""
    require(isinstance(parameters, dict), "Preparation workflow must be an object")
    for key, expected in echoes.items():
        require(
            key not in parameters or parameters[key] == expected,
            "Preparation echoed a different workflow authority: " + key,
        )
    unsupported = set(parameters) - {
        "allowed_paths",
        "checks",
        *echoes,
        *PREPARATION_METADATA_FIELDS,
    }
    require(
        not unsupported,
        "Preparation contains unsupported workflow fields: " + ", ".join(sorted(unsupported)),
    )
    require(
        {"allowed_paths", "checks"} <= set(parameters),
        "Preparation requires allowed_paths and checks",
    )
    metadata = {
        key: parameters[key] for key in sorted(PREPARATION_METADATA_FIELDS) if key in parameters
    }
    for key, value in metadata.items():
        if key == "checks_executed":
            valid = value is False
        elif key in {"check_status", "verification_limit"}:
            valid = isinstance(value, str) and bool(value.strip())
        elif key == "required_gates":
            valid = isinstance(value, list) and all(
                (isinstance(gate, str) and bool(gate.strip()))
                or (
                    isinstance(gate, dict)
                    and set(gate) == {"gate", "requirement"}
                    and all(isinstance(v, str) and bool(v.strip()) for v in gate.values())
                )
                for gate in value
            )
        else:
            valid = isinstance(value, list) and all(
                isinstance(entry, str) and bool(entry.strip()) for entry in value
            )
        require(valid, "Preparation has invalid evidence field: " + key)
    return {key: parameters[key] for key in ("allowed_paths", "checks")}, metadata


def validate_preparation_correction(
    original, corrected, original_decision_digest, schema_error, available_obligations
):
    """Accept only a complete, indexed reclassification of one rejected ``gates`` list."""
    original_workflow = original.get("workflow")
    corrected_workflow = corrected.get("workflow")
    require(
        isinstance(original_workflow, dict) and isinstance(corrected_workflow, dict),
        "Preparation correction requires workflow objects",
    )
    gates = original_workflow.get("gates")
    require(
        isinstance(gates, list)
        and bool(gates)
        and all(isinstance(gate, str) and gate.strip() for gate in gates),
        "Preparation correction requires one nonempty gates string list",
    )
    evidence = corrected.get("preparation_correction")
    require(
        isinstance(evidence, dict)
        and set(evidence) == {"original_decision_digest", "schema_error", "classifications"}
        and evidence.get("original_decision_digest") == original_decision_digest
        and evidence.get("schema_error") == schema_error,
        "Preparation correction does not bind the original decision and schema error",
    )
    classifications = evidence.get("classifications")
    require(
        isinstance(classifications, list)
        and len(classifications) == len(gates)
        and [row.get("index") for row in classifications if isinstance(row, dict)]
        == list(range(len(gates))),
        "Preparation correction must classify every original gates entry exactly once",
    )
    constraint_gates = []
    required_gates = []
    classification_fields = {
        "index",
        "text",
        "destination",
        "runtime_obligation",
        "no_additional_proof",
        "rationale",
    }
    for index, row in enumerate(classifications):
        require(
            set(row) == classification_fields
            and row.get("index") == index
            and row.get("text") == gates[index],
            "Preparation correction classification differs from original gates",
        )
        require(
            isinstance(row.get("rationale"), str) and row["rationale"].strip(),
            "Preparation correction classification lacks a rationale",
        )
        if row.get("destination") == "implementation_constraints":
            require(
                row.get("runtime_obligation") in available_obligations
                and row.get("runtime_obligation") in PREPARATION_RUNTIME_OBLIGATIONS,
                "Preparation correction names an unsupported runtime obligation",
            )
            require(
                row.get("no_additional_proof") is True,
                "Preparation correction must attest that an existing obligation needs no additional proof",
            )
            constraint_gates.append(gates[index])
        else:
            require(
                row.get("destination") == "required_gates"
                and row.get("runtime_obligation") is None
                and row.get("no_additional_proof") is False,
                "Preparation correction required-gate classification is invalid",
            )
            required_gates.append(gates[index])

    original_constraints = original_workflow.get("implementation_constraints", [])
    original_required = original_workflow.get("required_gates", [])
    corrected_required = corrected_workflow.get("required_gates", [])
    require(
        isinstance(original_constraints, list)
        and isinstance(original_required, list)
        and isinstance(corrected_required, list)
        and corrected_required[: len(original_required)] == original_required,
        "Preparation correction existing required_gates changed",
    )
    expected_workflow = {
        key: value
        for key, value in original_workflow.items()
        if key not in {"gates", "implementation_constraints", "required_gates"}
    }
    constraints = [*original_constraints, *constraint_gates]
    required = [*original_required, *required_gates]
    if constraints or "implementation_constraints" in original_workflow:
        expected_workflow["implementation_constraints"] = constraints
    if required or "required_gates" in original_workflow:
        expected_workflow["required_gates"] = required
    require(
        corrected_workflow == expected_workflow,
        "Preparation correction changed preparation authority",
    )
    original_authority = {key: value for key, value in original.items() if key != "workflow"}
    corrected_authority = {
        key: value
        for key, value in corrected.items()
        if key not in {"workflow", "preparation_correction"}
    }
    require(
        corrected_authority == original_authority,
        "Preparation correction changed preparation authority",
    )


async def correct_preparation_contract(app, item, saved, config, schema_error):
    """Run the single retained correction for the exact rejected ``gates`` representation."""
    from .contracts import digest, load_config

    require(
        schema_error == PREPARATION_GATES_SCHEMA_ERROR,
        "Preparation failure is not eligible for contract correction",
    )
    original = saved["decision"]
    original_value = app.result_json(original)
    parameters = original_value.get("workflow")
    require(isinstance(parameters, dict), "Preparation workflow must be an object")
    gates = parameters.get("gates")
    require(
        set(parameters)
        - {
            "allowed_paths",
            "checks",
            "persistence",
            "completion",
            "canonical_primary_branch",
            *PREPARATION_METADATA_FIELDS,
        }
        == {"gates"}
        and isinstance(gates, list)
        and bool(gates)
        and all(isinstance(gate, str) and gate.strip() for gate in gates),
        "Preparation failure is not eligible for contract correction",
    )
    selected = configured_workflow(config, item.item_id)
    echoes = {
        "persistence": config.data["provider"],
        "completion": selected["completion"],
        "canonical_primary_branch": selected["primary_branch"],
    }
    without_gates = {key: value for key, value in parameters.items() if key != "gates"}
    executable, _ = normalize_preparation_workflow(without_gates, echoes)
    validate_preparation_scope(executable, selected, config)
    require(asdict(item) == saved["item"], "Prepared item changed before correction")
    require(
        config.file_digest == saved["invocation_config_digest"],
        "Preparation configuration changed before correction",
    )
    validate_preparation_invocation(original, saved["invocation_config_digest"], app.root)
    binding = asdict(app._provider_invocation_snapshot(config).binding("coordinator"))
    require(
        original.get("binding") == binding,
        "Preparation Coordinator binding changed before correction",
    )
    authority = original_value.get("authority_evidence")
    require(
        isinstance(authority, list) and bool(authority),
        "Preparation lacks supporting authority evidence",
    )
    validate_preparation_sources(app, config, item, authority)
    available = available_preparation_obligations(selected, config)
    prompt = (
        "Correct one retained preparation response representation. Read only; do not implement, "
        "mutate, delegate, invoke claims, change authority, or delete an obligation. Return the "
        "complete original JSON result with only workflow.gates reclassified. Preserve every "
        "other top-level and workflow value exactly. Preserve every pre-existing required_gates "
        "entry exactly and first. "
        + PREPARATION_WORKFLOW_SCHEMA
        + " For every original gates entry, add one preparation_correction.classifications row in "
        "original order with exact fields {index,text,destination,runtime_obligation,"
        "no_additional_proof,rationale}. destination is implementation_constraints only when the "
        "Coordinator determines that one configured runtime obligation fully enforces the exact "
        "text and no additional acceptance proof is required; then runtime_obligation is one of "
        + json.dumps(sorted(available))
        + " and no_additional_proof is true. Otherwise destination is required_gates, "
        "runtime_obligation is null and no_additional_proof is false. Unknown or extra proof must "
        "remain required_gates. preparation_correction also contains original_decision_digest, "
        "schema_error and classifications. Original decision digest: "
        + digest(original)
        + "\nExact schema error: "
        + schema_error
        + "\nCanonical item and requirements: "
        + json.dumps(asdict(item), sort_keys=True)
        + "\nImmutable original response envelope: "
        + json.dumps(original, sort_keys=True)
    )
    corrected = await app.invoke(
        item.item_id,
        "preparation-contract-correction",
        "coordinator",
        prompt,
        read_only=True,
        purpose="provider",
    )
    app.validate_invocation_result(corrected)
    validate_preparation_invocation(corrected, saved["invocation_config_digest"], app.root)
    app.validate_call_limits(corrected, config.data["coordinator_limits"])
    require(
        original.get("role") == corrected.get("role") == "coordinator"
        and original.get("binding") == corrected.get("binding"),
        "Preparation correction changed Coordinator authority",
    )
    corrected_value = app.result_json(corrected)
    validate_preparation_correction(
        original_value,
        corrected_value,
        digest(original),
        schema_error,
        available,
    )
    corrected_parameters, _ = normalize_preparation_workflow(
        corrected_value.get("workflow"), echoes
    )
    validate_preparation_scope(corrected_parameters, selected, config)
    require(
        load_config(app.config_path).file_digest == config.file_digest,
        "Preparation configuration changed during correction",
    )
    validate_preparation_sources(app, config, item, authority)
    correction_path = app._stage_path(item.item_id, "preparation-contract-correction")
    require(
        correction_path.is_file()
        and not correction_path.is_symlink()
        and json.loads(correction_path.read_text()) == corrected,
        "Preparation correction result changed",
    )
    resolution = {
        "original_decision_digest": digest(original),
        "corrected_decision_digest": digest(corrected),
        "original_invocation_id": original.get("invocation_id"),
        "corrected_invocation_id": corrected.get("invocation_id"),
        "schema_error": schema_error,
    }
    resolution_path = app._stage_path(item.item_id, "preparation-contract-resolution")
    if resolution_path.exists():
        require(
            json.loads(resolution_path.read_text()) == resolution,
            "Preparation correction resolution changed",
        )
    else:
        atomic_json(resolution_path, resolution, exclusive=True)
    return corrected


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
            + PREPARATION_WORKFLOW_SCHEMA
            + " "
            "Do not infer approval from Ready alone when canonical content contains an explicit hold. "
            "Preparation selects bounded scope and checks; it does not authorize implementation. "
            "The harness separately obtains Coordinator Ready-to-Starting reservation and native "
            "Orchestrator Starting-to-Running acceptance afterward. Their absence at this phase "
            "is not itself a preparation blocker. Respect actual holds and recovery evidence; "
            "a verified stopped owner with no source changes or candidate does not require "
            "preserved-candidate recovery. Keep historical identity and unknown usage unchanged. "
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
    try:
        prepared_workflow(app, item.item_id, current)
    except TransitionBlocked as failure:
        if str(failure) != PREPARATION_GATES_SCHEMA_ERROR:
            raise
        await correct_preparation_contract(app, item, saved, current, str(failure))
        prepared_workflow(app, item.item_id, current)
    decision = effective_preparation_decision(
        app, item.item_id, saved, configured_workflow(current, item.item_id), current
    )
    authority = app.result_json(decision).get("authority_evidence")
    require(
        isinstance(authority, list) and bool(authority),
        "Preparation lacks supporting authority evidence",
    )
    validate_preparation_sources(app, current, item, authority)
    if not app._stage_path(item.item_id, "estimate-input").exists():
        require(asdict(item) == saved["item"], "Prepared item changed before provider effect")
    if (
        item.original_high is None
        and not app._stage_path(item.item_id, "prospective-estimate").exists()
    ):
        decision_path = app._stage_path(item.item_id, "estimate-decision")
        atomic_json(decision_path, decision)
        await _record_estimate(app, item.item_id, decision_path)
        item = app.provider.item(item.item_id)
    if app._stage_path(item.item_id, "prospective-estimate").exists():
        app.assignment_estimate(item)
    else:
        require(asdict(item) == saved["item"], "Prepared item changed before admission")
    return item


def validate_preparation_sources(app, config, item, authority):
    """Check local sources and exact previously bound stopped-runtime evidence."""
    from hashlib import sha256

    for evidence in authority:
        source = config.repository / evidence["path"]
        if Path(evidence["path"]).is_absolute():
            from .recovery import _validate_runtime_record

            saved = app._stage_path(item.item_id, "stopped-owner-input")
            request = json.loads(saved.read_text()) if saved.exists() else {}
            records = request.get("supplied", {}).get("runtime_records", [])
            matches = [
                r
                for r in records
                if r.get("path") == str(source) and r.get("sha256") == evidence.get("sha256")
            ]
            require(
                len(matches) == 1
                and str(source) in item.content
                and evidence.get("sha256") in item.content,
                "External preparation authority is not bound to stopped-owner evidence",
            )
            _validate_runtime_record(matches[0])
        else:
            require(
                source.resolve().is_relative_to(config.repository) and source.is_file(),
                "Preparation authority source is invalid",
            )
        require(bool(evidence.get("reason")), "Preparation authority reason is missing")
        require(
            sha256(source.read_bytes()).hexdigest() == evidence.get("sha256"),
            "Preparation authority source changed",
        )
