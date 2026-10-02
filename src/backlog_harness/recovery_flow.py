"""Explicit Coordinator recovery of preserved work from an ended external execution."""

import asyncio
import json
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

from .contracts import digest, freeze, plain
from .evidence import EvidenceStore, async_operation_lock, atomic_json, component, operation_lock
from .provider import AgentProvider, Item, TransitionBlocked, git
from .workflow import require, validate_transition


async def recover_item(app, item_id, evidence_path):
    from .recovery import validate_packet

    require(isinstance(app.provider, AgentProvider), "Recovery requires agent provider management")
    supplied = json.loads(Path(evidence_path).read_text())
    request_path = app._stage_path(item_id, "recovery-input")
    async with async_operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        with operation_lock(app.root / "solo-execution.lock"):
            if request_path.exists():
                request = json.loads(request_path.read_text())
                require(request["evidence"] == supplied, "Recovery input changed")
            else:
                await app.refresh_provider()
                item = app.provider.item(item_id)
                require(
                    item.state == "Running"
                    or (item.state == "Ready" and item.owner in {None, "Unowned"}),
                    "Recovery requires stopped Running or unowned Ready",
                )
                request = {"item": asdict(item), "evidence": supplied}
                atomic_json(request_path, request, exclusive=True)
            original = Item(**request["item"])
            recovery = app.recovery_record(item_id)
            if not recovery:
                stage = "recover-decision-" + digest(request)
                decision = await app.invoke(
                    item_id,
                    stage,
                    "coordinator",
                    "You are the Dev Backlog Coordinator. Reconcile this one preserved item "
                    "whose earlier execution has ended. Ready recovery imports the unchanged candidate "
                    "without another Ready transition; later reservation and native acceptance remain separate. "
                    "Read supplied runtime, candidate and supporting "
                    "evidence plus selected management/recovery skills. No mutation, claims, dispatch or "
                    "delegation in this assessment. Preserve existing permission denials. "
                    "Martin authorizes moving backlog execution into the "
                    "harness CLI; the desktop identity remains historical. Decide whether ordinary "
                    "redispatch of this SAME item is safe under the existing serial crisis reservation. "
                    "Historical usage and original estimate stay unknown. Estimate only remaining "
                    "verification/corrections/review/delivery work; do not redefine lifetime accounting. "
                    "Return JSON {operation:redispatch|assess,item_id,provider_revision,previous_owner,"
                    "ownership_ended:boolean,continuation_authorized:boolean,reason:string,"
                    "remaining_high:positive_integer,scope:{allowed_paths:[exact_paths],checks:[[argv]]},"
                    "dependencies:[exact_required_ids]}. Establish all required predecessor states. "
                    "Preserve the entire supplied base..candidate lineage; do not select another item. "
                    "Choose executable checks for the actual remaining work in the complete canonical item. "
                    "Preserve its semantic, review, approval and delivery gates; do not inject unrelated "
                    "browser work. For Ready recovery echo preserved_execution exactly, including "
                    "remaining_work and candidate_approval_required. Do not authorize new source "
                    "production, candidate changes or attempt resets.\n"
                    + json.dumps(request, sort_keys=True),
                    purpose="provider",
                )
                app.validate_call_limits(decision, app.config.data["coordinator_limits"])
                answer = app.result_json(decision)
                require(
                    answer.get("operation") == "redispatch"
                    and answer.get("item_id") == item_id
                    and answer.get("provider_revision") == original.revision
                    and (
                        answer.get("previous_owner") == original.owner
                        or (
                            original.state == "Ready"
                            and supplied.get("preserved_execution", {}).get("execution_id")
                            and answer.get("previous_owner")
                            == supplied["preserved_execution"]["execution_id"]
                        )
                    )
                    and answer.get("ownership_ended") is True
                    and answer.get("continuation_authorized") is True
                    and isinstance(answer.get("reason"), str)
                    and answer["reason"].strip(),
                    "Coordinator did not authorize this ownership recovery",
                )
                packet = {
                    "runtime_records": supplied["runtime_records"],
                    "candidate": supplied["candidate"],
                    "version": 1,
                    "item_id": item_id,
                    "revision": original.revision,
                    "previous_owner": original.owner,
                    "remaining_high": answer["remaining_high"],
                    "scope": answer["scope"],
                    "historical_usage": "unknown",
                }
                if original.state == "Ready":
                    require(
                        answer.get("preserved_execution") == supplied.get("preserved_execution")
                        and supplied.get("preserved_execution"),
                        "Coordinator changed preserved execution or remaining gates",
                    )
                    packet["preserved_execution"] = supplied["preserved_execution"]
                packet["candidate"] = {
                    **supplied["candidate"],
                    "allowed_paths": answer["scope"]["allowed_paths"],
                }
                verified = validate_packet(packet, original, Path(packet["candidate"]["checkout"]))
                dependencies = answer.get("dependencies")
                require(
                    isinstance(dependencies, list)
                    and all(
                        isinstance(x, str) and app.provider.item(x).state == "Completed"
                        for x in dependencies
                    ),
                    "Recovery dependencies are not completed",
                )
                workflow = plain(app.config.data["workflow"])
                workflow.pop("items", None)
                workflow.update(answer["scope"])
                observation = app.provider.observation()
                recovery = {
                    **verified,
                    "workflow": workflow,
                    "config_workflow": plain(app.config.data["workflow"]),
                    "decision_stage": stage,
                    "decision_invocation": decision["invocation_id"],
                    "historical_original_high": original.original_high,
                    "policy_sources": {
                        e["path"]: e["sha256"]
                        for e in observation["policy"]["evidence"]
                        if e["path"] != original.path
                    },
                    "dependencies": dependencies,
                    "reason": answer["reason"],
                }
                atomic_json(app._stage_path(item_id, "recovery"), recovery, exclusive=True)
            else:
                decision = json.loads(
                    app._stage_path(item_id, recovery["decision_stage"]).read_text()
                )
                app.validate_invocation_result(decision)
            packet = recovery["packet"]
            if not app._stage_path(item_id, "assignment").exists():
                validate_packet(packet, original, Path(packet["candidate"]["checkout"]))
                if original.state == "Running":
                    await app.transition(
                        item_id,
                        original.revision,
                        "Ready",
                        app.authority(
                            decision,
                            item_id,
                            operation="redispatch",
                            recovery={
                                "previous_owner": original.owner,
                                "ownership_ended": True,
                                "packet_digest": recovery["digest"],
                                "candidate": packet["candidate"]["head"],
                                "runtime_evidence": packet["runtime_records"],
                                "reason": recovery["reason"],
                            },
                        ),
                        validate=validate_transition,
                    )
                else:
                    require(
                        app.provider.item(item_id) == original, "Ready recovery revision changed"
                    )
                item = app.provider.item(item_id)
                observation = app.provider.observation()
                observation["dependencies"][item_id] = recovery["dependencies"]
                atomic_json(app.provider.cache_path, observation)
                candidate = packet["candidate"]
                destination = Path(app.config.data["candidate_root"]) / component(item_id)
                if not destination.exists():
                    await asyncio.to_thread(
                        subprocess.run,
                        ["git", "clone", "--no-hardlinks", candidate["checkout"], str(destination)],
                        check=True,
                        capture_output=True,
                    )
                require(
                    git(destination, "rev-parse", "HEAD") == candidate["head"]
                    and not git(destination, "status", "--porcelain"),
                    "Recovery clone differs",
                )
                for entry in candidate["evidence"]:
                    source = Path(candidate["checkout"]) / entry["path"]
                    target = destination / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        require(
                            target.read_bytes() == source.read_bytes(), "Preserved evidence differs"
                        )
                    else:
                        shutil.copyfile(source, target)
                require(
                    not git(destination, "status", "--porcelain"),
                    "Recovery evidence must remain untracked and ignored",
                )
                base_path = app._stage_path(item_id, "base")
                if base_path.exists():
                    require(
                        json.loads(base_path.read_text()) == {"commit": candidate["base"]},
                        "Recovery base differs",
                    )
                else:
                    atomic_json(base_path, {"commit": candidate["base"]}, exclusive=True)
                atomic_json(
                    app._stage_path(item_id, "assignment"),
                    {
                        "content": item.content,
                        "provider_revision": item.revision,
                        "provider_path": item.path,
                        "original_high": packet["remaining_high"],
                        "historical_original_high": original.original_high,
                        "workflow": recovery["workflow"],
                        "candidate_root": app.config.data["candidate_root"],
                        "workspace": app.config.data["workspace"],
                        "accounting_scope": "recovery_remaining_work",
                    },
                    exclusive=True,
                )
    if original.state == "Ready":
        validate_packet(
            recovery["packet"], original, Path(recovery["packet"]["candidate"]["checkout"])
        )
        imported = Path(app.config.data["candidate_root"]) / component(item_id)
        require(
            git(imported, "rev-parse", "HEAD") == recovery["packet"]["candidate"]["head"]
            and not git(imported, "status", "--porcelain"),
            "Imported preserved candidate changed",
        )
        require(
            app.provider.item(item_id) == original,
            "Ready recovery already advanced; use the normal queue",
        )
        return {
            "item_id": item_id,
            "state": "Ready",
            "recovery_prepared": True,
            "candidate": recovery["packet"]["candidate"]["head"],
        }
    return await app.run_item(item_id)


def question_handoff_paths(repository, item, declared_paths=None):
    """Bind a question move to its source and established series membership."""
    series = [
        line.removeprefix("Series: ")
        for line in item.content.splitlines()
        if line.startswith("Series: ")
    ]
    expected = [item.path, "backlog/user-action-required/" + Path(item.path).name, *series]
    paths = expected if declared_paths is None else declared_paths
    require(
        len(series) <= 1
        and paths == expected
        and len(set(paths)) == len(paths)
        and all(not Path(p).is_absolute() and ".." not in Path(p).parts for p in paths),
        "Question handoff paths or series membership are invalid",
    )
    if series:
        index = repository / series[0]
        require(
            series[0] == str(Path(item.path).parent / "index.md")
            and index.is_file()
            and "](" + Path(item.path).name + ")" in index.read_text(),
            "Question handoff series membership is not established",
        )
    return paths


async def defer_item(app, item_id, question_path):
    """Reconcile a stopped canonical rejection into the provider's question queue."""
    require(
        isinstance(app.provider, AgentProvider),
        "Question handoff requires agent provider management",
    )
    supplied = json.loads(Path(question_path).read_text())
    question = supplied["question"]
    require(question.get("question_id") and question.get("text"), "Exact pending question required")
    request_path = app._stage_path(item_id, "defer-input")
    async with async_operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        with operation_lock(app.root / "solo-execution.lock"):
            if request_path.exists():
                request = json.loads(request_path.read_text())
                require(request["supplied"] == supplied, "Question handoff input changed")
            else:
                item = app.provider.item(item_id)
                require(
                    item.state in {"Ready", "Running", "User Action Required"},
                    "Question handoff requires Ready or a stopped canonical owner",
                )
                if item.state == "Ready":
                    from .estimation import validate_preparation_invocation

                    prepared = json.loads(app._stage_path(item_id, "preparation").read_text())
                    outcome = prepared["decision"]
                    app.validate_invocation_result(outcome)
                    validate_preparation_invocation(
                        outcome, prepared["invocation_config_digest"], app.root
                    )
                    value = app.result_json(outcome)
                    require(
                        prepared["item"] == asdict(item)
                        and item.owner in {None, "Unowned"}
                        and outcome.get("role") == "coordinator"
                        and value.get("item_id") == item_id
                        and value.get("provider_revision") == item.revision
                        and (value.get("blocked") or value.get("status") == "blocked"),
                        "Ready question requires current unowned Coordinator preparation evidence",
                    )
                else:
                    outcome = json.loads(app._stage_path(item_id, "produce-review").read_text())
                    app.validate_invocation_result(outcome)
                    value = app.result_json(outcome)
                    require(
                        outcome["session"]["session_id"] == item.owner
                        and value.get("item_id") == item_id
                        and (
                            (value.get("request_completion") is False and value.get("blockers"))
                            or value.get("question") == question
                        )
                        and (
                            item.state != "User Action Required"
                            or (
                                all(
                                    (app.provider.question(item) or {}).get(key)
                                    == question.get(key)
                                    for key in ("question_id", "text")
                                )
                                and not (app.provider.question(item) or {}).get("answer")
                            )
                        ),
                        "Canonical declined completion evidence is missing",
                    )
                question_handoff_paths(app.config.repository, item, supplied["paths"])
                request = {
                    "item": asdict(item),
                    "outcome_invocation": outcome["invocation_id"],
                    "outcome": value,
                    "supplied": supplied,
                }
                atomic_json(request_path, request, exclusive=True)
            item = Item(**request["item"])
            stage = "defer-decision-" + digest(request)
            saved_decision = app._stage_path(item_id, stage)
            if saved_decision.exists():
                from .estimation import validate_preparation_invocation

                # The stage digest binds the immutable request regardless of JSON key order.
                decision = json.loads(saved_decision.read_text())
                app.validate_invocation_result(decision)
                validate_preparation_invocation(decision, app.config.file_digest, app.root)
                intent = json.loads((Path(decision["evidence_path"]) / "intent.json").read_text())
                require(
                    intent.get("item_id") is None
                    and intent.get("action") == stage
                    and decision.get("purpose") == "provider"
                    and intent.get("operation_id") == item_id + ":" + stage
                    and decision.get("role") == "coordinator",
                    "Question decision is not bound to the immutable request",
                )
            else:
                decision = await app.invoke(
                    item_id,
                    stage,
                    "coordinator",
                    (
                        "Record this unowned Ready preparation conflict without reserving or executing work. "
                        "Preserve the exact requirements, history, and any earlier candidate references. "
                        if item.state == "Ready"
                        else "Reconcile this stopped canonical execution, including an incomplete question move. "
                    )
                    + "Preserve all permission boundaries. The exact question below is already recorded; "
                    "do not ask it again, execute implementation, mutate, or delegate. Decide "
                    "whether to record User Action Required with the exact pending question, preserved "
                    "native owner/candidate/evidence, prohibited browser action, and declared provider move "
                    "and series membership paths. Return JSON {operation:await-user|assess,item_id,"
                    "provider_revision,question,paths,reason}. No new admission is authorized by this call.\n"
                    + json.dumps(request),
                    purpose="provider",
                )
            app.validate_call_limits(decision, app.config.data["coordinator_limits"])
            value = app.result_json(decision)
            require(
                value.get("operation") == "await-user"
                and value.get("item_id") == item_id
                and value.get("provider_revision") == item.revision
                and value.get("question") == question
                and value.get("paths")
                in (
                    supplied["paths"],
                    {
                        "source": supplied["paths"][0],
                        "destination": supplied["paths"][1],
                        "series_membership": supplied["paths"][2:],
                    },
                )
                and value.get("reason"),
                "Coordinator did not authorize the exact question handoff",
            )
            return await app.transition(
                item_id,
                item.revision,
                "User Action Required",
                app.authority(
                    decision,
                    item_id,
                    operation="await-user",
                    stopped_owner=item.owner,
                    outcome_evidence=request["outcome_invocation"],
                    **(
                        {
                            "provider_revision": item.revision,
                            "preparation_evidence": request["outcome_invocation"],
                        }
                        if item.state == "Ready"
                        else {}
                    ),
                    question=question,
                ),
                validate=validate_transition,
                declared_paths=supplied["paths"],
            )


async def reconcile_stopped_owner(app, item_id, evidence_path):
    """Release a stopped owner with no source candidate; do not schedule a replacement."""
    from .recovery import _validate_runtime_record, validate_active_owner_binding

    require(isinstance(app.provider, AgentProvider), "Owner reconciliation requires agent provider")
    supplied = json.loads(Path(evidence_path).read_text())
    runtime = supplied["runtime_records"]
    require(
        isinstance(runtime, list) and len(runtime) == 1, "One stopped canonical runtime is required"
    )
    async with async_operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        with operation_lock(app.root / "solo-execution.lock"):
            request_path = app._stage_path(item_id, "stopped-owner-input")
            if request_path.exists():
                request = json.loads(request_path.read_text())
                require(request["supplied"] == supplied, "Stopped owner input changed")
                item = Item(**request["item"])
            else:
                item = app.provider.item(item_id)
                require(item.state == "Running", "Stopped owner must be Running")
                request = {"item": asdict(item), "supplied": supplied}
                atomic_json(request_path, request, exclusive=True)
            for record in runtime:
                _validate_runtime_record(record)
                validate_active_owner_binding(item, record, supplied.get("owner_binding"))
            stage = "stopped-owner-decision-" + digest(request)
            decision = await app.invoke(
                item_id,
                stage,
                "coordinator",
                "Reconcile this stopped Running owner using the exact native runtime and provider "
                "evidence. provider_revision is SHA256(relative path UTF-8 + NUL + content UTF-8), "
                "not the raw content SHA256. owner_binding.section_sha256 hashes the untrimmed text "
                "after the unique ## Running Acceptance Evidence newline up to the next newline ## "
                "heading (or EOF), preserving all whitespace. Read-only: no claims, browser, "
                "mutation, delegation or dispatch. Determine "
                "whether no source changes or candidate were produced and ordinary redispatch is safe. "
                "Preserve all identity/history/unknown usage. This decision only permits Running to "
                "Ready and Unowned; it authorizes no launch or global policy change. Return JSON "
                "{operation:redispatch|assess,item_id,provider_revision,previous_owner,ownership_ended:"
                "boolean,no_source_changes:boolean,runtime_digest,owner_binding_digest,reason}. If a candidate exists, "
                "return assess; do not discard it.\n"
                + json.dumps(
                    {
                        **request,
                        "runtime_digest": digest(runtime),
                        "owner_binding_digest": digest(supplied["owner_binding"]),
                    }
                ),
                purpose="provider",
            )
            app.validate_invocation_result(decision)
            app.validate_call_limits(decision, app.config.data["coordinator_limits"])
            value = app.result_json(decision)
            require(
                value.get("operation") == "redispatch"
                and value.get("no_source_changes") is True
                and isinstance(value.get("reason"), str)
                and value["reason"].strip(),
                "Coordinator did not establish no-change owner reconciliation",
            )
            return await app.transition(
                item_id,
                item.revision,
                "Ready",
                app.authority(
                    decision,
                    item_id,
                    operation="redispatch",
                    recovery={
                        "previous_owner": item.owner,
                        "ownership_ended": True,
                        "no_source_changes": True,
                        "packet_digest": digest(request),
                        "runtime_evidence": runtime,
                        "decision_stage": stage,
                        "owner_binding": supplied["owner_binding"],
                        "reason": value["reason"],
                    },
                ),
                validate=validate_transition,
            )


def verify_starting_effect(app, record, receipt, reconciliation):
    """Verify a separate read-only receipt without completing the interrupted call."""
    require(
        record["item"]["state"] == "Ready"
        and record["target"] == "Starting"
        and receipt.get("advancement_verified") is False,
        "Effect reconciliation requires an interrupted Starting reservation",
    )
    stage = record["stage_operation"][len(record["item"]["item_id"]) + 1 :]
    original = json.loads(app._stage_path(record["item"]["item_id"], stage).read_text())
    original_path = Path(original["evidence_path"])
    prior = EvidenceStore.reconcile(original_path)
    require(
        original["outcome"] == "unresolved"
        and prior["outcome"] == "unresolved"
        and prior["invocation_id"] == original["invocation_id"]
        and prior["operation_id"] == record["stage_operation"]
        and prior["binding"] == original["binding"]
        and app.process_stopped(original_path)
        and reconciliation["original_invocation_id"] == original["invocation_id"]
        and reconciliation["original_result_digest"] == digest(original)
        and reconciliation["operation"] == digest(record),
        "Interrupted provider identity differs",
    )
    result = reconciliation["result"]
    app.validate_invocation_result(result)
    app.validate_call_limits(result, app.config.data["administrative_review_limits"])
    evidence = Path(result["evidence_path"])
    require(
        evidence.resolve().is_relative_to(app.root.resolve()),
        "Reconciliation evidence escaped root",
    )
    intent = json.loads((evidence / "intent.json").read_text())
    context = json.loads((evidence / "execution-context.json").read_text())
    native_session = json.loads((evidence / "session.json").read_text())
    require(
        result["invocation_id"] != original["invocation_id"]
        and result["role"] == record["executing_role"]
        and result["session"] == original["session"]
        and result["binding"] == original["binding"]
        and intent["invocation_id"] == result["invocation_id"]
        and intent["request_digest"] == result["request_digest"]
        and intent["binding"] == result["binding"]
        and intent["action"] == "reservation-effect-reconciliation"
        and intent["operation_id"]
        == record["item"]["item_id"] + ":reservation-effect-reconciliation"
        and all(native_session.get(key) == value for key, value in result["session"].items())
        and context.get("purpose") == "provider"
        and context.get("read_only") is True
        and context.get("provider_operation") is None,
        "Effect reconciliation was not a separate same-session read-only invocation",
    )
    observed = app.verify_provider_receipt(record, app.result_json(result))
    require(
        all(observed[key] == receipt[key] for key in ("operation", "commit", "after"))
        and reconciliation["effect"] == observed,
        "Reconciled provider effect differs",
    )
    before = record["item"]["content"]
    require(before.splitlines().count("Status: Ready") == 1, "Starting source status is ambiguous")
    preserved = before.replace("Status: Ready", "Status: Starting", 1)
    require(
        observed["after"]["content"].startswith(preserved),
        "Starting reconciliation changed existing requirements",
    )
    return observed


def reconcile_starting_effect(app, operation_id, evidence_path):
    """Record an already committed reservation; never retry the provider mutation."""
    require(
        isinstance(app.provider, AgentProvider), "Effect reconciliation requires agent management"
    )
    root = app.root / "provider-agent-operations" / component(operation_id)
    record = json.loads((root / "requested.json").read_text())
    require(digest(record) == operation_id, "Provider operation identity differs")
    result = json.loads(Path(evidence_path).read_text())
    stage = record["stage_operation"][len(record["item"]["item_id"]) + 1 :]
    original = json.loads(app._stage_path(record["item"]["item_id"], stage).read_text())
    observed = app.verify_provider_receipt(record, app.result_json(result))
    reconciliation = {
        "operation": operation_id,
        "original_invocation_id": original["invocation_id"],
        "original_result_digest": digest(original),
        "original_outcome": "unresolved",
        "original_usage_coverage": "incomplete",
        "result": result,
        "effect": observed,
    }
    receipt_path = root / "receipt.json"
    receipt = (
        json.loads(receipt_path.read_text())
        if receipt_path.exists()
        else {
            **observed,
            "advancement_verified": False,
        }
    )
    with operation_lock(app.config.repository / ".git/agentic-provider.lock"):
        verify_starting_effect(app, record, receipt, reconciliation)
        require(
            not git(
                app.config.repository, "diff", observed["commit"], "HEAD", "--", *record["paths"]
            )
            and not git(app.config.repository, "status", "--porcelain", "--", *record["paths"]),
            "Later or uncommitted provider effect conflicts with reconciliation",
        )
        saved = root / "effect-reconciliation.json"
        if saved.exists():
            require(
                json.loads(saved.read_text()) == reconciliation, "Reconciliation evidence changed"
            )
        else:
            atomic_json(saved, reconciliation, exclusive=True)
        if not receipt_path.exists():
            atomic_json(receipt_path, receipt, exclusive=True)
        before = json.loads(app.provider.cache_path.read_text())
        current = next(
            row for row in before["items"] if row["item_id"] == record["item"]["item_id"]
        )
        require(current in (record["item"], observed["after"]), "Provider projection changed")
        app.advance_provider_projection(
            before, record["source_manifest"], observed, record["paths"]
        )
    return {
        "item_id": record["item"]["item_id"],
        "state": "Starting",
        "effect_verified": True,
        "original_advancement_verified": False,
        "receipt": str(saved),
        "commit": observed["commit"],
    }


def register_work_continuation(app, item_id, instruction_path):
    """Keep one blocked result and request bounded work in its retained native session."""
    instruction = Path(instruction_path).read_text().strip()
    require(instruction, "Continuation instruction is empty")
    with operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        item = app.provider.item(item_id)
        recovery = app.recovery_record(item_id)
        require(
            item.state == "Running" and recovery and recovery["packet"].get("preserved_execution"),
            "Work continuation requires a Running preserved execution",
        )
        previous = json.loads(app._stage_path(item_id, "produce-review").read_text())
        app.validate_invocation_result(previous)
        value = app.result_json(previous)
        require(
            previous["role"] == "orchestrator"
            and previous["session"]["session_id"] == item.owner
            and value.get("item_id") == item_id
            and value.get("request_completion") is False
            and value.get("status") == "blocked"
            and value.get("blockers")
            and not value.get("question"),
            "Continuation lacks a retained owned blocked result",
        )
        candidate = recovery["packet"]["candidate"]["head"]
        require(
            value.get("candidate") == candidate
            and git(app.candidate_repository(item_id), "rev-parse", "HEAD") == candidate
            and not git(app.candidate_repository(item_id), "status", "--porcelain"),
            "Continuation preserved candidate differs",
        )
        request = {
            "item_id": item_id,
            "revision": item.revision,
            "owner": item.owner,
            "candidate": candidate,
            "previous_invocation_id": previous["invocation_id"],
            "previous_result_digest": digest(previous),
            "instruction": instruction,
        }
        path = app._stage_path(item_id, "work-continuation")
        registration = app._stage_path(item_id, "work-continuation-registration")
        if registration.exists():
            require(
                json.loads(registration.read_text()) == request,
                "Work continuation registration changed",
            )
        else:
            atomic_json(registration, request, exclusive=True)
        if path.exists():
            require(json.loads(path.read_text()) == request, "Work continuation request changed")
        else:
            atomic_json(path, request, exclusive=True)
    return {
        "item_id": item_id,
        "state": "Running",
        "continuation_prepared": True,
        "stage": "continue-work-" + digest(request),
    }


def read_work_continuation(app, item_id):
    """Validate the exact registered request before scheduling or invocation."""
    path = app._stage_path(item_id, "work-continuation")
    if not path.exists():
        return None
    request = json.loads(path.read_text())
    registration = app._stage_path(item_id, "work-continuation-registration")
    require(
        isinstance(request, dict)
        and set(request)
        == {
            "item_id",
            "revision",
            "owner",
            "candidate",
            "previous_invocation_id",
            "previous_result_digest",
            "instruction",
        }
        and registration.exists()
        and json.loads(registration.read_text()) == request,
        "Work continuation registration changed",
    )
    return request


def work_continuation(app, item_id, acceptance):
    """Bind the follow-up to the unchanged owner, result, and candidate before invocation."""
    request = read_work_continuation(app, item_id)
    if request is None:
        return None
    item = app.provider.item(item_id)
    previous = json.loads(app._stage_path(item_id, "produce-review").read_text())
    app.validate_invocation_result(previous)
    recovery = app.recovery_record(item_id)
    require(
        request["item_id"] == item_id
        and request["revision"] == item.revision
        and item.state == "Running"
        and request["owner"] == item.owner
        and previous["session"] == acceptance["session"]
        and previous["invocation_id"] == request["previous_invocation_id"]
        and digest(previous) == request["previous_result_digest"]
        and request["candidate"] == recovery["packet"]["candidate"]["head"]
        and git(app.candidate_repository(item_id), "rev-parse", "HEAD") == request["candidate"]
        and not git(app.candidate_repository(item_id), "status", "--porcelain"),
        "Work continuation identity, result, or candidate changed",
    )
    return request


async def run_artifact_proof(app, item_id, instruction_path, *, prepare_only=False):
    """Run one retained-item proof without changing its accepted owner or source permissions."""
    instruction = Path(instruction_path).read_text().strip()
    require(instruction, "Proof instruction is empty")
    async with async_operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        with operation_lock(app.root / "solo-execution.lock"):
            item = app.provider.item(item_id)
            acceptance = json.loads(app._stage_path(item_id, "accept").read_text())
            continuation = work_continuation(app, item_id, acceptance)
            require(continuation is not None, "Proof requires a retained work continuation")
            prior_stage = "continue-work-" + digest(continuation)
            previous = json.loads(app._stage_path(item_id, prior_stage).read_text())
            app.validate_invocation_result(previous)
            value = app.result_json(previous)
            require(
                previous["session"] == acceptance["session"]
                and value.get("item_id") == item_id
                and value.get("candidate") == continuation["candidate"]
                and value.get("status") == "blocked"
                and value.get("request_completion") is False
                and (value.get("blocker") or value.get("blockers"))
                and not value.get("question"),
                "Proof requires the owned blocked continuation result",
            )
            data = plain(app.config.data)
            data["workspace"] = str(app.candidate_repository(item_id))
            snapshot = replace(app.config, data=freeze(data))
            binding = snapshot.binding("orchestrator")
            require(
                snapshot.data["profiles"][binding.profile_name].get("artifact_output") is True,
                "Proof requires explicitly configured artifact_output permission",
            )
            request = {
                "item_id": item_id,
                "revision": item.revision,
                "owner": item.owner,
                "candidate": continuation["candidate"],
                "previous_result_digest": digest(previous),
                "instruction": instruction,
                "binding": asdict(binding),
            }
            saved = app._stage_path(item_id, "artifact-proof-request")
            if saved.exists():
                require(json.loads(saved.read_text()) == request, "Retained proof request changed")
            else:
                atomic_json(saved, request, exclusive=True)
            stage = "artifact-proof-" + digest(request)
            prompt = (
                "Perform only the missing proof for this retained candidate. The canonical owner "
                "is unchanged; this separate proof invocation has no lifecycle or delivery authority. "
                "Candidate source, historical inputs and harness receipts are read-only. Use only "
                "the invocation artifact-output contract for proof output. Preserve explicit denials. "
                "Reuse verified captures and source review; do not repeat production or admission. "
                "Arrange any independent Judges/review through native delegation, require read-only "
                "review, and preserve their exact identities, candidate bindings, verdicts and evidence. "
                'No claims. Return JSON {item_id,candidate,status:"evidence-ready"|"blocked",'
                'artifacts:[{path:"absolute output file",sha256:"file digest"}],blockers:[]}. '
                "Evidence-ready requires output files with supporting evidence; blocked requires "
                "exact blockers. A successful invocation is not workflow completion. "
                "Bound request: "
                + json.dumps(request)
                + "\nRetained blocked result: "
                + json.dumps(value)
            )
            if prepare_only:
                from uuid import uuid4

                resolved_binding = binding
                operation = item_id + ":" + stage
                store = EvidenceStore(app.root, "item:" + item_id)
                operation_path = store.run / "operations" / component(operation)
                paths = list(operation_path.glob("invocations/*/intent.json"))
                expected_hash = digest(["proof", str(snapshot.repository), False, None, prompt])
                if operation_path.exists():
                    require(len(paths) == 1, "Proof invocation intent is absent or ambiguous")
                    path = paths[0].parent
                    prior = EvidenceStore.reconcile(path)
                    require(
                        prior["request_digest"] == expected_hash
                        and prior["binding"] == asdict(resolved_binding)
                        and prior["config_digest"] == snapshot.file_digest,
                        "Prepared proof invocation changed",
                    )
                else:
                    path = store.begin(
                        operation,
                        str(uuid4()),
                        snapshot,
                        resolved_binding,
                        action=stage,
                        item_id=item_id,
                        request_digest=expected_hash,
                    )
                return {
                    "item_id": item_id,
                    "owner": item.owner,
                    "candidate": request["candidate"],
                    "proof_stage": stage,
                    "artifact_directory": str(path / "artifacts"),
                    "submitted": (path / "requested.json").exists(),
                    "workflow_advanced": False,
                }
            await app.enforce_guard(item_id)
            result = await app.invoke(
                item_id, stage, "orchestrator", prompt, read_only=False, purpose="proof"
            )
            require(app.provider.item(item_id) == item, "Canonical item changed during proof")
            require(
                git(app.candidate_repository(item_id), "rev-parse", "HEAD") == request["candidate"]
                and not git(app.candidate_repository(item_id), "status", "--porcelain"),
                "Candidate changed during proof",
            )
            proof = validate_artifact_proof(app, item_id, stage, request, result, acceptance)
            return {
                "item_id": item_id,
                "owner": item.owner,
                "candidate": request["candidate"],
                "proof_stage": stage,
                "result": proof,
                "workflow_advanced": False,
            }


def validate_artifact_proof(app, item_id, stage, request, result, acceptance):
    """Bind reported proof files to this invocation without asserting semantic acceptance."""
    from hashlib import sha256

    value = app.result_json(result)
    require(
        result.get("binding") == request["binding"]
        and result.get("role") == "orchestrator"
        and result.get("purpose") == "proof"
        and result["session"]["native_session_id"] != acceptance["session"]["native_session_id"]
        and result["session"]["session_id"] != request["owner"]
        and value.get("item_id") == item_id
        and value.get("candidate") == request["candidate"]
        and value.get("status") in {"evidence-ready", "blocked"},
        "Returned proof identity or status differs",
    )
    operation = item_id + ":" + stage
    expected = (
        app.root.resolve()
        / "runs"
        / component("item:" + item_id)
        / "operations"
        / component(operation)
        / "invocations"
        / component(result["invocation_id"])
    )
    evidence = Path(result["evidence_path"])
    require(
        evidence.absolute() == expected and evidence.resolve() == expected,
        "Returned proof evidence path differs",
    )
    output = expected / "artifacts"
    contract = json.loads((evidence / "artifact-output.json").read_text())
    require(
        contract
        == {
            "version": 1,
            "invocation_id": result["invocation_id"],
            "operation_id": operation,
            "path": str(output),
            "writer": "invoked agent",
            "reviewer_access": "read",
            "permission_digest": request["binding"]["permission_digest"],
        },
        "Returned proof artifact contract differs",
    )
    artifacts = value.get("artifacts")
    require(
        isinstance(artifacts, list)
        and (
            bool(artifacts)
            if value["status"] == "evidence-ready"
            else bool(value.get("blocker") or value.get("blockers"))
        ),
        "Proof needs output artifacts or concrete blockers",
    )
    require(not output.is_symlink() and output.resolve() == output, "Proof output redirected")
    for artifact in artifacts:
        require(
            isinstance(artifact, dict) and set(artifact) == {"path", "sha256"},
            "Invalid proof artifact",
        )
        require(isinstance(artifact["path"], str), "Invalid proof artifact path")
        path = Path(artifact["path"])
        require(
            path.is_absolute()
            and path.resolve().is_relative_to(output)
            and path.is_file()
            and sha256(path.read_bytes()).hexdigest() == artifact["sha256"],
            "Proof artifact escaped output directory or hash differs",
        )
    return value


def validated_auxiliary_proof(app, item_id, acceptance):
    """Verify retained proof files before handing them to the original agent for fresh review."""
    request = json.loads(app._stage_path(item_id, "artifact-proof-request").read_text())
    stage = "artifact-proof-" + digest(request)
    result = json.loads(app._stage_path(item_id, stage).read_text())
    app.validate_invocation_result(result)
    value = validate_artifact_proof(app, item_id, stage, request, result, acceptance)
    require(value["status"] == "evidence-ready", "Auxiliary proof remains blocked")
    manifests = []
    for artifact in value["artifacts"]:
        payload = json.loads(Path(artifact["path"]).read_text())
        if isinstance(payload, dict) and isinstance(payload.get("artifacts"), list):
            manifests.append(payload)
    require(len(manifests) == 1, "Auxiliary proof needs one bound supporting-file manifest")
    manifest = manifests[0]
    validate_artifact_proof(
        app, item_id, stage, request, {**result, "text": json.dumps(manifest)}, acceptance
    )
    review = manifest.get("independent_result_review", {})
    require(
        {"path": review.get("path"), "sha256": review.get("sha256")} in value["artifacts"],
        "Independent proof verdict is not bound to the returned artifacts",
    )
    return request, result, value


def register_proof_continuation(app, item_id, instruction_path):
    """Hand one validated auxiliary proof back to its unchanged canonical execution."""
    instruction = Path(instruction_path).read_text().strip()
    require(instruction, "Proof continuation instruction is empty")
    with operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        acceptance = json.loads(app._stage_path(item_id, "accept").read_text())
        previous = work_continuation(app, item_id, acceptance)
        require(previous is not None, "Proof continuation requires retained canonical work")
        request, result, _value = validated_auxiliary_proof(app, item_id, acceptance)
        item = app.provider.item(item_id)
        require(
            request["item_id"] == item_id
            and request["revision"] == item.revision
            and request["owner"] == item.owner
            and request["candidate"] == previous["candidate"],
            "Proof handoff canonical binding differs",
        )
        packet = {
            "item_id": item_id,
            "revision": item.revision,
            "owner": item.owner,
            "candidate": request["candidate"],
            "proof_result_digest": digest(result),
            "proof_request_digest": digest(request),
            "instruction": instruction,
        }
        for stage in ("proof-continuation-registration", "proof-continuation"):
            path = app._stage_path(item_id, stage)
            if path.exists():
                require(
                    json.loads(path.read_text()) == packet,
                    "Proof continuation registration changed",
                )
            else:
                atomic_json(path, packet, exclusive=True)
        return {
            "item_id": item_id,
            "continuation_prepared": True,
            "stage": "continue-proof-" + digest(packet),
        }


def proof_continuation(app, item_id, acceptance):
    path = app._stage_path(item_id, "proof-continuation")
    if not path.exists():
        return None
    packet = json.loads(path.read_text())
    registration = app._stage_path(item_id, "proof-continuation-registration")
    require(
        isinstance(packet, dict)
        and set(packet)
        == {
            "item_id",
            "revision",
            "owner",
            "candidate",
            "proof_result_digest",
            "proof_request_digest",
            "instruction",
        }
        and registration.exists()
        and json.loads(registration.read_text()) == packet,
        "Proof continuation registration changed",
    )
    previous = work_continuation(app, item_id, acceptance)
    request, result, value = validated_auxiliary_proof(app, item_id, acceptance)
    item = app.provider.item(item_id)
    require(
        previous
        and packet["item_id"] == item_id
        and packet["revision"] == item.revision
        and packet["owner"] == item.owner
        and packet["candidate"] == previous["candidate"]
        and request["revision"] == item.revision
        and request["owner"] == item.owner
        and request["candidate"] == packet["candidate"]
        and digest(request) == packet["proof_request_digest"]
        and digest(result) == packet["proof_result_digest"],
        "Proof continuation identity or evidence changed",
    )
    return {"request": packet, "proof": value}


def retained_delivery_result(app, item_id, registration, *, registered=True):
    """Validate the canonical conditional delivery request; this never creates agent evidence."""
    saved = app._stage_path(item_id, "retained-delivery-authorization")
    require(
        not registered or (saved.exists() and json.loads(saved.read_text()) == registration),
        "Retained delivery authorization changed",
    )
    result = json.loads(app._stage_path(item_id, registration["stage"]).read_text())
    app.validate_invocation_result(result)
    acceptance = json.loads(app._stage_path(item_id, "accept").read_text())
    value = app.result_json(result)
    require(
        isinstance(value, dict) and isinstance(value.get("question"), dict),
        "Canonical conditional delivery request is malformed",
    )
    authorization = registration["authorization"]
    require(
        digest(result) == registration["result_digest"]
        and result["session"] == acceptance["session"]
        and result["role"] == "orchestrator"
        and value.get("item_id") == item_id
        and value.get("status") == "awaiting_exact_candidate_approval"
        and value.get("candidate") == authorization["candidate"]
        and value.get("question", {}).get("candidate") == authorization["candidate"]
        and value["question"].get("question_id") == authorization["question_id"]
        and authorization["disposition"] == "approve"
        and authorization["source_reference"]
        and authorization["answer"],
        "Canonical conditional delivery request or authorization differs",
    )
    return result


async def authorize_retained_delivery(app, item_id, authorization_path):
    """Apply an explicit operator answer to an already verified conditional delivery request."""
    authorization = json.loads(Path(authorization_path).read_text())
    require(
        isinstance(authorization, dict)
        and set(authorization)
        == {
            "item_id",
            "question_id",
            "revision",
            "candidate",
            "disposition",
            "answer",
            "source_reference",
        }
        and authorization["item_id"] == item_id
        and authorization["disposition"] == "approve"
        and all(isinstance(v, str) and v.strip() for v in authorization.values()),
        "Explicit bound delivery authorization is required",
    )
    with operation_lock(app.root / "item-locks" / (component(item_id) + ".lock")):
        path = app._stage_path(item_id, "retained-delivery-authorization")
        if path.exists():
            registration = json.loads(path.read_text())
            require(
                registration["authorization"] == authorization, "Delivery authorization changed"
            )
        else:
            item = app.provider.item(item_id)
            question = app.provider.question(item)
            require(
                item.state == "User Action Required"
                and item.revision == authorization["revision"]
                and question
                and question.get("question_id") == authorization["question_id"]
                and question.get("candidate") == authorization["candidate"],
                "Delivery authorization does not answer the current candidate question",
            )
            candidates = []
            for stage in app._stage_path(item_id, "unused").parent.glob("*.json"):
                try:
                    record = json.loads(stage.read_text())
                except (ValueError, OSError):
                    continue
                if (
                    not isinstance(record, dict)
                    or record.get("role") != "orchestrator"
                    or not record.get("text")
                ):
                    continue
                try:
                    value = app.result_json(record)
                except (ValueError, TypeError, KeyError, TransitionBlocked):
                    continue
                if not isinstance(value, dict):
                    continue
                if (
                    value.get("question")
                    == {k: question[k] for k in ("question_id", "text", "candidate")}
                    and value.get("status") == "awaiting_exact_candidate_approval"
                ):
                    candidates.append((stage.stem, record))
            require(
                len(candidates) == 1, "Unique canonical conditional delivery request is required"
            )
            stage, result = candidates[0]
            registration = {
                "authorization": authorization,
                "stage": stage,
                "result_digest": digest(result),
            }
            retained_delivery_result(app, item_id, registration, registered=False)
            atomic_json(path, registration, exclusive=True)
        retained_delivery_result(app, item_id, registration)
    return await app.answer(
        item_id,
        authorization["question_id"],
        authorization["revision"],
        authorization["answer"],
        retained_delivery=registration,
    )


def validate_requirement_results(contract, value):
    """Check the complete configured evidence set; visual correctness belongs to review."""
    expected = {row["id"] for row in contract["requirements"]}
    rows = value.get("requirement_results")
    require(
        isinstance(rows, list)
        and all(isinstance(row, dict) for row in rows)
        and len(rows) == len(expected)
        and {row.get("id") for row in rows} == expected,
        "Proof must report every configured requirement exactly once",
    )
    artifacts = value.get("artifacts", [])
    for row in rows:
        require(
            set(row) == {"id", "outcome", "artifacts"}
            and row["outcome"] == "PASS"
            and isinstance(row["artifacts"], list)
            and bool(row["artifacts"])
            and all(ref in artifacts for ref in row["artifacts"]),
            "Proof requirement failed or supporting artifact is unbound: " + str(row.get("id")),
        )


def validate_requirement_review(contract, proof_result, proof, review):
    require(
        review.get("proof_result_digest") == digest(proof_result)
        and review.get("requirements_digest") == digest(contract),
        "Independent proof review binding differs",
    )
    expected = {row["id"]: row for row in proof["requirement_results"]}
    rows = review.get("requirement_assessments")
    require(
        isinstance(rows, list)
        and all(isinstance(row, dict) for row in rows)
        and len(rows) == len(expected)
        and {row.get("id") for row in rows} == set(expected),
        "Independent proof review omitted or duplicated a requirement",
    )
    for row in rows:
        require(
            set(row) == {"id", "verdict", "assessment", "artifacts"}
            and row["verdict"] == "ACCEPT"
            and isinstance(row["assessment"], str)
            and bool(row["assessment"].strip())
            and isinstance(row["artifacts"], list)
            and bool(row["artifacts"])
            and all(ref in expected[row["id"]]["artifacts"] for ref in row["artifacts"]),
            "Independent proof review lacks supported acceptance: " + str(row.get("id")),
        )


def configured_proof_request(app, item_id, candidate, acceptance):
    workflow = app.item_workflow(item_id)
    contract = plain(workflow.get("proof_requirements"))
    if not contract:
        return None
    preparation_path = app._stage_path(item_id, "preparation")
    require(preparation_path.exists(), "Configured proof requires original preparation evidence")
    preparation = json.loads(preparation_path.read_text())
    require(
        contract["provider_revision"] == preparation["item"]["revision"]
        and contract["preparation_digest"] == digest(preparation["decision"]),
        "Configured proof differs from original preparation",
    )
    binding = app.config.binding("orchestrator")
    require(
        app.config.data["profiles"][binding.profile_name].get("artifact_output") is True,
        "Configured proof requires artifact_output permission",
    )
    request = {
        "item_id": item_id,
        "revision": contract["provider_revision"],
        "owner": acceptance["session"]["session_id"],
        "candidate": candidate,
        "binding": asdict(binding),
        "proof_requirements": contract,
        "requirements_digest": digest(contract),
        "preparation_receipt_digest": digest(preparation),
    }
    if contract.get("verification_required"):
        from .acceptance_verification import inputs

        request["verification_inputs"] = inputs(app, item_id, candidate)
    return request


async def ensure_configured_proof(app, item_id, candidate, acceptance):
    """Reuse confined artifact production and a fresh native review for explicit visual proof."""
    request = configured_proof_request(app, item_id, candidate, acceptance)
    if request is None:
        return
    saved = app._stage_path(item_id, "configured-proof-request")
    if saved.exists():
        require(json.loads(saved.read_text()) == request, "Configured proof request changed")
    else:
        atomic_json(saved, request, exclusive=True)
    stage = "configured-proof-" + digest(request)
    preparation = app._stage_path(item_id, "preparation").read_text()
    prompt = (
        "Collect only the explicitly required browser/print evidence for this exact candidate. "
        "Source and provider are read-only; use the invocation artifact-output directory only. "
        "Requirements do not grant browser/server permissions: preserve all actual denials. "
        "If unavailable return blocked with concrete blockers, never substitute static checks. "
        "Return JSON {item_id,candidate,status:evidence-ready|blocked,artifacts:[{path,sha256}],"
        "requirement_results:[{id,outcome:PASS|FAIL,artifacts:[{path,sha256}]}],blockers:[]}. "
        "Every configured requirement needs one result and its actual supporting captures. "
        "Do not change source, commit, deliver, or mutate lifecycle. Bound request: "
        + json.dumps(request)
        + "\nOriginal preparation receipt: "
        + preparation
    )
    if not app._stage_path(item_id, stage).exists():
        await app.enforce_guard(item_id)
    result = await app.invoke(
        item_id, stage, "orchestrator", prompt, read_only=False, purpose="proof"
    )
    require(
        git(app.candidate_repository(item_id), "rev-parse", "HEAD") == candidate
        and not git(app.candidate_repository(item_id), "status", "--porcelain"),
        "Candidate changed during configured proof",
    )
    proof = validate_artifact_proof(app, item_id, stage, request, result, acceptance)
    require(
        proof["status"] == "evidence-ready",
        "Configured proof blocked: " + json.dumps(proof.get("blockers")),
    )
    validate_requirement_results(request["proof_requirements"], proof)
    review_stage = "configured-proof-review-" + digest(result)
    review_prompt = (
        "Arrange one fresh native read-only child review using fork_turns=none or fork_context=false. "
        "Do not repeat source production, proof collection, or source review. Give the reviewer the "
        "exact candidate, full original preparation, configured requirements and proof artifacts. "
        "The reviewer must inspect supporting files and assess substantive acceptance for EVERY "
        "requirement. Return child JSON {candidate,verdict:ACCEPT|REJECT,unresolved_findings:[],"
        "proof_result_digest,requirements_digest,requirement_assessments:[{id,verdict:ACCEPT|REJECT,"
        "assessment:<substantive reason>,artifacts:[{path,sha256}]}]}. Wait for that reviewer. "
        "Return only JSON {item_id,candidate,proof_reviewer_session:<native child identity>}. "
        "No delivery authority. Bound request: "
        + json.dumps(request)
        + "\nProof result digest: "
        + digest(result)
        + "\nProof result: "
        + json.dumps(proof)
        + "\nOriginal preparation receipt: "
        + preparation
    )
    if request["proof_requirements"].get("verification_required"):
        review_prompt += (
            "\nThe same fresh reviewer also acts as independent verifier, separately from visual "
            "assessment. It must differ from source producer, proof producer and source reviewer. "
            "Read full canonical acceptance from assignment and inspect every bound source-review "
            "and check receipt, verifying exact file hashes. If present, inspect accepted design "
            "and design acceptance; assess implementation against it and review justified deviations. "
            "Challenge overall acceptance coverage. "
            "Add acceptance_verification:{role:independent-verifier,candidate,requirements_digest,"
            "inputs_digest,inspected_receipts:<exact request verification_inputs.receipts>,"
            "verdict:ACCEPT|REJECT,acceptance_coverage:<substantive coverage and limits>,"
            "unresolved_findings:[]}. Never certify future integration or provider closure. "
            "Verification inputs digest: " + digest(request["verification_inputs"])
        )
    if not app._stage_path(item_id, review_stage).exists():
        await app.enforce_guard(item_id)
    reviewed = await app.invoke(
        item_id,
        review_stage,
        "orchestrator",
        review_prompt,
        session=app.session(acceptance),
        read_only=True,
    )
    value = app.result_json(reviewed)
    require(
        value.get("item_id") == item_id and value.get("candidate") == candidate,
        "Proof review response differs",
    )
    from .native_evidence import verify_native_review

    review = verify_native_review(
        reviewed["session"]["native_session_id"],
        value.get("proof_reviewer_session"),
        candidate,
        app.native_sessions_root(reviewed["binding"]),
    )
    validate_requirement_review(request["proof_requirements"], result, proof, review)
    from .acceptance_verification import validate as validate_verification

    validate_verification(app, item_id, candidate, request["proof_requirements"], result, review)
    record = {
        "request_digest": digest(request),
        "result_digest": digest(result),
        "review_result": reviewed,
        "review": review,
    }
    path = app._stage_path(item_id, "configured-proof-acceptance")
    if path.exists():
        previous = json.loads(path.read_text())
        # Native parent transcripts can grow on resume; their current verification is decisive.
        require(
            previous["request_digest"] == record["request_digest"]
            and previous["result_digest"] == record["result_digest"]
            and previous["review_result"] == reviewed,
            "Configured proof acceptance changed",
        )
    else:
        atomic_json(path, record, exclusive=True)


def verify_configured_proof(app, item_id, candidate):
    """Delivery always rechecks files and native acceptance, including recovery/integration."""
    if not app.item_workflow(item_id).get("proof_requirements"):
        return
    acceptance = json.loads(app._stage_path(item_id, "accept").read_text())
    request = configured_proof_request(app, item_id, candidate, acceptance)
    saved = app._stage_path(item_id, "configured-proof-request")
    require(
        saved.exists() and json.loads(saved.read_text()) == request,
        "Required configured candidate proof is missing or stale",
    )
    stage = "configured-proof-" + digest(request)
    result_path = app._stage_path(item_id, stage)
    require(result_path.exists(), "Configured proof result is missing")
    result = json.loads(result_path.read_text())
    app.validate_invocation_result(result)
    proof = validate_artifact_proof(app, item_id, stage, request, result, acceptance)
    require(proof["status"] == "evidence-ready", "Configured proof is blocked")
    validate_requirement_results(request["proof_requirements"], proof)
    path = app._stage_path(item_id, "configured-proof-acceptance")
    require(path.exists(), "Required configured proof acceptance is missing")
    saved = json.loads(path.read_text())
    require(
        saved["request_digest"] == digest(request) and saved["result_digest"] == digest(result),
        "Configured proof acceptance binding differs",
    )
    reviewed = saved["review_result"]
    app.validate_invocation_result(reviewed)
    value = app.result_json(reviewed)
    require(
        value.get("item_id") == item_id and value.get("candidate") == candidate,
        "Proof review response differs",
    )
    from .native_evidence import verify_native_review

    review = verify_native_review(
        reviewed["session"]["native_session_id"],
        value.get("proof_reviewer_session"),
        candidate,
        app.native_sessions_root(reviewed["binding"]),
    )
    validate_requirement_review(request["proof_requirements"], result, proof, review)
    from .acceptance_verification import validate as validate_verification

    validate_verification(app, item_id, candidate, request["proof_requirements"], result, review)


def verify_candidate_approval(app, item_id, candidate, *, include_recovery=True):
    workflow = app.item_workflow(item_id)
    recovery = app.recovery_record(item_id) if include_recovery else None
    required = workflow.get("candidate_approval_required") or bool(
        recovery
        and recovery["packet"].get("preserved_execution", {}).get("candidate_approval_required")
    )
    if not required:
        return
    path = app._stage_path(item_id, "continuation")
    approved = json.loads(path.read_text()).get("approval", {}) if path.exists() else {}
    require(
        approved.get("item_id") == item_id
        and approved.get("disposition") == "approve"
        and approved.get("question", {}).get("candidate") == candidate
        and isinstance(approved.get("answer", {}).get("text"), str)
        and bool(approved["answer"]["text"].strip())
        and approved.get("answer", {}).get("digest") == digest(approved["answer"]["text"]),
        (
            "Exact preserved-candidate approval is required before delivery"
            if recovery
            else "Exact candidate approval is required before delivery"
        ),
    )


def design_request(app, item_id, base, acceptance):
    selected = plain(app.item_workflow(item_id).get("design_review"))
    if not selected:
        return None
    preparation_path = app._stage_path(item_id, "preparation")
    require(preparation_path.exists(), "Design review requires original preparation evidence")
    preparation = json.loads(preparation_path.read_text())
    require(
        selected["provider_revision"] == preparation["item"]["revision"]
        and selected["preparation_digest"] == digest(preparation["decision"]),
        "Design selection differs from original preparation",
    )
    binding = app.config.binding("orchestrator")
    require(
        app.config.data["profiles"][binding.profile_name].get("artifact_output") is True,
        "Design review requires artifact_output permission",
    )
    return {
        "item_id": item_id,
        "owner": acceptance["session"]["session_id"],
        "candidate": base,
        "design_review": selected,
        "preparation_receipt_digest": digest(preparation),
        "binding": asdict(binding),
    }


def validated_design_attempt(app, item_id, request, acceptance, attempt):
    from .native_evidence import verify_native_review

    stage = "design-attempt-" + str(attempt) + "-" + digest(request)
    path = app._stage_path(item_id, stage)
    require(path.exists(), "Design attempt evidence is missing")
    result = json.loads(path.read_text())
    app.validate_invocation_result(result)
    value = validate_artifact_proof(app, item_id, stage, request, result, acceptance)
    require(value["status"] == "evidence-ready", "Design remains blocked")
    artifact = value.get("design")
    require(
        isinstance(artifact, dict) and artifact in value["artifacts"], "Design artifact is unbound"
    )
    review = verify_native_review(
        result["session"]["native_session_id"],
        value.get("reviewer_session"),
        request["candidate"],
        app.native_sessions_root(result["binding"]),
        accepted_verdicts=("ACCEPT", "REJECT"),
    )
    require(
        review.get("candidate") == request["candidate"]
        and review.get("design_digest") == artifact["sha256"]
        and review.get("request_digest") == digest(request),
        "Independent design review binding differs",
    )
    return result, value, review


def verify_design_acceptance(app, item_id, base=None):
    """Recheck exact artifacts and native decision before allowing source writes or delivery."""
    if not app.item_workflow(item_id).get("design_review"):
        return None
    path = app._stage_path(item_id, "design-acceptance")
    request_path = app._stage_path(item_id, "design-request")
    require(
        path.exists() and request_path.exists(),
        "Independent design acceptance is required before source implementation",
    )
    saved = json.loads(path.read_text())
    request = json.loads(request_path.read_text())
    acceptance = json.loads(app._stage_path(item_id, "accept").read_text())
    require(
        request
        == design_request(
            app, item_id, base if base is not None else request["candidate"], acceptance
        )
        and saved.get("request_digest") == digest(request)
        and saved.get("source_base") == request["candidate"]
        and type(saved.get("attempt")) is int
        and saved["attempt"] in (1, 2),
        "Accepted design request or source base changed",
    )
    if saved["attempt"] == 2:
        _, _, prior = validated_design_attempt(app, item_id, request, acceptance, 1)
        require(prior["verdict"] == "REJECT", "Design correction lacks independent rejection")
    result, value, review = validated_design_attempt(
        app, item_id, request, acceptance, saved["attempt"]
    )
    require(
        saved["result_digest"] == digest(result)
        and saved["design"] == value["design"]
        and review["verdict"] == "ACCEPT",
        "Accepted design evidence changed",
    )
    return saved


async def ensure_design_acceptance(app, item_id, base, acceptance):
    """At most one correction follows a genuine native rejection; failed evidence never retries."""
    request = design_request(app, item_id, base, acceptance)
    if request is None:
        return None
    if app._stage_path(item_id, "design-acceptance").exists():
        return verify_design_acceptance(app, item_id, base)
    repo = app.candidate_repository(item_id)
    require(
        git(repo, "rev-parse", "HEAD") == base and not git(repo, "status", "--porcelain"),
        "Source changed before independent design acceptance",
    )
    path = app._stage_path(item_id, "design-request")
    if path.exists():
        require(json.loads(path.read_text()) == request, "Design request changed")
    else:
        atomic_json(path, request, exclusive=True)
    feedback = None
    for attempt in (1, 2):
        stage = "design-attempt-" + str(attempt) + "-" + digest(request)
        prompt = (
            "Prepare only a preimplementation design artifact for the bound requirement. Source and "
            "provider are read-only; write only to the invocation artifact-output directory. No source "
            "implementation, commits or lifecycle actions. Preserve permissions and denials. Arrange "
            "one fresh native read-only child reviewer with fork_turns=none or fork_context=false. "
            "Give the child the source base, full original preparation, design artifact and hash, and "
            "request digest. The child must return JSON {candidate:<source base>,design_digest:<file "
            "sha256>,request_digest,verdict:ACCEPT|REJECT,unresolved_findings:[]}; REJECT needs concrete "
            "findings. Wait for the native result. Return JSON {item_id,candidate:<source base>,"
            "status:evidence-ready|blocked,artifacts:[{path,sha256}],design:{path,sha256},"
            "reviewer_session:<native child identity>,blockers:[]}. A rejected design is evidence-ready "
            "but is not accepted. Do not correct it within this invocation after the reviewer verdict; "
            "the harness permits at most one bounded correction. Request digest: "
            + digest(request)
            + "\nBound design request: "
            + json.dumps(request)
            + "\nOriginal preparation: "
            + app._stage_path(item_id, "preparation").read_text()
            + "\nPrior rejected design and findings: "
            + json.dumps(feedback)
        )
        if not app._stage_path(item_id, stage).exists():
            await app.enforce_guard(item_id)
        await app.invoke(item_id, stage, "orchestrator", prompt, read_only=False, purpose="proof")
        require(
            git(repo, "rev-parse", "HEAD") == base and not git(repo, "status", "--porcelain"),
            "Source changed before independent design acceptance",
        )
        result, value, review = validated_design_attempt(app, item_id, request, acceptance, attempt)
        if review["verdict"] == "ACCEPT":
            saved = {
                "request_digest": digest(request),
                "result_digest": digest(result),
                "attempt": attempt,
                "design": value["design"],
                "source_base": base,
            }
            atomic_json(app._stage_path(item_id, "design-acceptance"), saved, exclusive=True)
            return saved
        feedback = {"design": value["design"], "review": review}
    require(False, "Independent design review rejected both bounded attempts")
