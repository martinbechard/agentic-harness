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
                    item.state == "Running", "Only a stopped Running owner requires this handoff"
                )
                outcome = json.loads(app._stage_path(item_id, "produce-review").read_text())
                app.validate_invocation_result(outcome)
                value = app.result_json(outcome)
                require(
                    outcome["session"]["session_id"] == item.owner
                    and value.get("item_id") == item_id
                    and value.get("request_completion") is False
                    and value.get("blockers"),
                    "Canonical declined completion evidence is missing",
                )
                paths = supplied["paths"]
                require(
                    len(paths) in {2, 3}
                    and paths[0] == item.path
                    and paths[1] == "backlog/user-action-required/" + Path(item.path).name
                    and len(set(paths)) == len(paths)
                    and all(not Path(p).is_absolute() and ".." not in Path(p).parts for p in paths),
                    "Question handoff paths are invalid",
                )
                series = [
                    line.removeprefix("Series: ")
                    for line in item.content.splitlines()
                    if line.startswith("Series: ")
                ]
                require(
                    len(series) <= 1 and paths[2:] == series,
                    "Question handoff series membership is required",
                )
                if len(paths) == 3:
                    require(
                        "Series: " + paths[2] in item.content.splitlines()
                        and paths[2] == str(Path(item.path).parent / "index.md")
                        and "](" + Path(item.path).name + ")"
                        in (app.config.repository / paths[2]).read_text(),
                        "Question handoff series membership is not established",
                    )
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
                "Reconcile this stopped canonical execution. Its explicit browser permission denial "
                "must not be bypassed. The exact user question below is ALREADY PENDING; do not ask it "
                "again, browse, mutate, or delegate. The unrelated bundle failure is established "
                "pre-existing; retain it as residual evidence, not a second user question. Decide "
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
                    question=question,
                ),
                validate=validate_transition,
                declared_paths=supplied["paths"],
            )
