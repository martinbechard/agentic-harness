"""Explicit Coordinator recovery of preserved work from an ended external execution."""

import asyncio
import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from .contracts import digest, plain
from .evidence import async_operation_lock, atomic_json, component, operation_lock
from .provider import AgentProvider, Item, git
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
                require(item.state == "Running", "Recovery requires a stopped Running assignment")
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
                    "You are the Dev Backlog Coordinator. Reconcile this one preserved Running item "
                    "whose desktop execution has ended. Read supplied runtime, candidate and supporting "
                    "evidence plus selected management/recovery skills. No mutation, claims, dispatch or "
                    "delegation in this assessment. Claim-free crisis does not prohibit a local HTTP verification server. "
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
                    "Choose executable checks that validate the actual work; browser checks remain "
                    "required through supported HTTP/browser tools, not waived by command-only checks.\n"
                    + json.dumps(request),
                    purpose="provider",
                )
                app.validate_call_limits(decision, app.config.data["coordinator_limits"])
                answer = app.result_json(decision)
                require(
                    answer.get("operation") == "redispatch"
                    and answer.get("item_id") == item_id
                    and answer.get("provider_revision") == original.revision
                    and answer.get("previous_owner") == original.owner
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
                and value.get("paths") == supplied["paths"]
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
