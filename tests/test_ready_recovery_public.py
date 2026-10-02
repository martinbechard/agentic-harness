"""AI attribution: Generated with AI assistance.

Exercise preserved Ready recovery through real evidence and provider boundaries.
"""

import asyncio
import json
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml
from test_provider_coordination import policy_evidence
from test_recovery_evidence import _ready_recovery_case
from test_telemetry import payload

from backlog_harness.application import Application
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.native_evidence import coordination_instructions
from backlog_harness.provider import TransitionBlocked, git
from backlog_harness.recovery_flow import recover_item
from backlog_harness.telemetry import Sink


def _public_case(config_file, provider, tmp_path, monkeypatch, completion=False):
    case = tmp_path / "preserved"
    case.mkdir()
    packet, historical, repository, runtime = _ready_recovery_case(case)
    # Ignore evidence through committed configuration so a fresh clone is clean.
    git(repository, "checkout", packet["candidate"]["base"])
    (repository / ".gitignore").write_text(".agents/\n")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-m", "Ignore operational evidence")
    packet["candidate"]["base"] = git(repository, "rev-parse", "HEAD")
    (repository / "answer.txt").write_text("after\n")
    git(repository, "add", "answer.txt")
    git(repository, "commit", "-m", "Preserved implementation")
    old_head = packet["candidate"]["head"]
    packet["candidate"]["head"] = git(repository, "rev-parse", "HEAD")
    receipt = Path(packet["preserved_execution"]["receipt"]["path"])
    receipt.write_text(receipt.read_text().replace(old_head, packet["candidate"]["head"]))
    packet["preserved_execution"]["receipt"]["sha256"] = sha256(receipt.read_bytes()).hexdigest()
    remaining = "Finish semantic validation, integration review, independent review and exact-candidate approval."
    packet["scope"]["checks"] = [
        [
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('answer.txt').read_text() == 'after\\n'",
        ]
    ]
    packet["preserved_execution"]["remaining_work"] = remaining
    packet["preserved_execution"]["candidate_approval_required"] = True
    item = provider.item("item-one")
    content = (
        item.content
        + "\n"
        + historical.content.replace(old_head, packet["candidate"]["head"]).replace(
            "Finish semantic validation, independent review and exact-candidate approval.",
            remaining,
        )
        + "Attempt: 3\n"
    )
    (provider.repository / item.path).write_text(content)
    git(provider.repository, "add", item.path)
    git(provider.repository, "commit", "-m", "Preserve execution history")
    item = provider.item(item.item_id)
    packet["preserved_execution"]["canonical_sha256"] = sha256(content.encode()).hexdigest()
    config, data = config_file
    imports = tmp_path / "imports"
    imports.mkdir()
    data.update(
        repository=str(provider.repository),
        provider_interaction="agent",
        candidate_root=str(imports),
    )
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    (tmp_path / "native-home").mkdir()
    data["agent_clis"]["primary"]["adapter_options"] = {"codex_home": str(tmp_path / "native-home")}
    for name in ("manage-work-items", "manage-work-items-file"):
        skill = Path(data["methodology_root"]) / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("Fixture management skill")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    atomic_json(
        app.provider.cache_path,
        {
            "items": [asdict(item)],
            "dependencies": {item.item_id: []},
            "non_items": [],
            "questions": {},
            "transition_paths": {},
            "archive_debt": [],
            "source_revision": app.provider.source_revision(),
            "source_manifest": app.provider.source_manifest(),
            "observer_digest": app.provider_observer_digest(app.config),
            "capability_digest": "retired-helper-metadata",
            "policy": {
                "eligible": True,
                "mode": "SOLO",
                "primary_branch": "main",
                "evidence": policy_evidence(provider.repository),
            },
        },
    )
    supplied = tmp_path / "recovery.json"
    atomic_json(
        supplied,
        {key: packet[key] for key in ("candidate", "runtime_records", "preserved_execution")},
    )
    calls, mutations, prompts = [], [], {}
    question = {
        "candidate": packet["candidate"]["head"],
        "question_id": "approve-preserved",
        "text": "Approve exact candidate "
        + packet["candidate"]["head"]
        + " after semantic and integration review?",
    }

    def envelope(stage, role, value, kwargs):
        native = app.root / "test-native" / stage
        sink = Sink(native / "telemetry.jsonl", {})
        export = payload()
        export["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "1"}}
        ]
        sink.write(export)
        binding = asdict(app.config.binding(role))
        session = kwargs.get("session")
        result = {
            "invocation_id": stage,
            "request_digest": digest(value),
            "binding": binding,
            "role": role,
            "purpose": "provider"
            if kwargs.get("provider_operation")
            else kwargs.get("purpose", "implementation"),
            "outcome": "returned",
            "session": {
                "session_id": session.session_id if session else stage,
                "native_session_id": session.native_session_id
                if session
                else str(uuid5(NAMESPACE_URL, stage)),
                "binding": binding,
            },
            "evidence_path": str(native),
            "text": json.dumps(value),
            "events": [
                {
                    "at": f"2026-10-01T12:00:{len(calls):02d}Z",
                    "type": "turn.completed",
                    "usage": {"output_tokens": 2 if session else 1},
                }
            ],
            "telemetry_path": str(native / "telemetry.jsonl"),
            "telemetry": {
                "span_count": len(sink.seen),
                "rejected_exports": 0,
                "evidence_sha256": sink.evidence_digest(),
            },
        }
        atomic_json(native / "session.json", result["session"])
        atomic_json(
            native / "execution-context.json",
            {
                "purpose": result["purpose"],
                "read_only": kwargs.get("read_only", True),
                "provider_operation": kwargs.get("provider_operation"),
            },
        )
        intent = {
            "invocation_id": stage,
            "request_digest": result["request_digest"],
            "binding": binding,
            "config_digest": app.config.file_digest,
            "item_id": None if result["purpose"] == "provider" else item.item_id,
            "action": stage,
            "operation_id": item.item_id + ":" + stage,
        }
        if kwargs.get("coordination"):
            context = app.claim_coordination_context(app.config)
            intent["coordination_digest"] = digest(context)
            atomic_json(native / "coordination-context.json", context)
            atomic_json(native / "requested.json", {"version": 1})
        atomic_json(native / "intent.json", intent)
        atomic_json(app._stage_path(item.item_id, stage), result)
        return result

    async def native_agent(item_id, stage, role, prompt, **kwargs):
        saved = app._stage_path(item_id, stage)
        if saved.exists():
            return json.loads(saved.read_text())
        coordination = None
        if kwargs.get("coordination"):
            coordination = app.claim_coordination_context(app.config)
            prompt = prompt.replace(
                "Follow current claim-free crisis authority; do not invoke claims. ", ""
            )
            prompt += coordination_instructions(coordination)
        calls.append(stage)
        prompts[stage] = prompt
        if stage == "reservation-effect-reconciliation":
            value = json.loads(prompt)
        elif stage.startswith("recover-decision-"):
            value = {
                "operation": "redispatch",
                "item_id": item_id,
                "provider_revision": item.revision,
                "previous_owner": packet["preserved_execution"]["execution_id"],
                "ownership_ended": True,
                "continuation_authorized": True,
                "reason": "Stopped execution has an immutable preserved candidate",
                "remaining_high": 25,
                "scope": packet["scope"],
                "dependencies": [],
                "preserved_execution": packet["preserved_execution"],
            }
        elif stage == "admit":
            value = {
                "operation": "new",
                "item_id": item_id,
                "provider_revision": item.revision,
                "reason": "Continue same item",
            }
        elif stage == "accept":
            value = {"item_id": item_id, "accepted": True}
        elif stage == "produce-review" or stage.startswith(("continue-work-", "continue-proof-")):
            assert app.provider.item(item_id).state == "Running"
            producer = kwargs["session"].native_session_id
            reviewer = str(uuid5(NAMESPACE_URL, "unused-before-approval"))
            logs = tmp_path / "native-home/sessions/2026/10/01"
            logs.mkdir(parents=True, exist_ok=True)
            parent = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": "review-call",
                        "arguments": json.dumps(
                            {
                                "fork_turns": "none",
                                "message": "Review retained candidate."
                                + (
                                    coordination_instructions(coordination)
                                    if coordination is not None
                                    else ""
                                ),
                            }
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "review-call",
                        "output": reviewer,
                    },
                },
            ]
            child = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": reviewer,
                        "timestamp": "2026-10-01T12:00:00Z",
                        "parent_thread_id": producer,
                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": producer}}},
                        "git": {"commit_hash": packet["candidate"]["head"]},
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": json.dumps(
                            {
                                "candidate": packet["candidate"]["head"],
                                "verdict": "ACCEPT",
                                "unresolved_findings": [],
                            }
                        ),
                    },
                },
            ]
            if stage.startswith("continue-proof-"):
                handoff = json.loads(app._stage_path(item_id, "proof-continuation").read_text())
                verdict = json.loads(child[-1]["payload"]["last_agent_message"])
                verdict["proof_result_digest"] = handoff["proof_result_digest"]
                child[-1]["payload"]["last_agent_message"] = json.dumps(verdict)
            for identity, records in ((producer, parent), (reviewer, child)):
                (logs / f"rollout-{identity}.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in records)
                )
            value = (
                {
                    "item_id": item_id,
                    "candidate": packet["candidate"]["head"],
                    "request_completion": True,
                    "reviewer_session": str(uuid5(NAMESPACE_URL, "unused-before-approval")),
                }
                if completion
                else {
                    "item_id": item_id,
                    "question": question,
                    "status": "awaiting_exact_candidate_approval",
                    "candidate": packet["candidate"]["head"],
                    "reviewer_session": reviewer,
                }
            )
            if stage.startswith("continue-proof-"):
                value["proof_reviewer_session"] = reviewer
        else:
            assert stage.startswith("provider-") and kwargs["provider_operation"]
            request = json.loads(prompt.split("\n", 1)[1])
            before, target = request["item"], request["target"]
            paths = request["paths"]
            owner = request["authority"]["session_id"] if target == "Running" else before["owner"]
            content = (
                before["content"]
                .replace("Status: " + before["state"], "Status: " + target)
                .replace("Owner: " + before["owner"], "Owner: " + owner)
            )
            destination = paths[-1] if target in {"User Action Required", "Completed"} else paths[0]
            if target == "User Action Required":
                content += (
                    "\nQuestion ID: " + question["question_id"] + "\n" + question["text"] + "\n"
                )
            if destination != paths[0]:
                (provider.repository / paths[0]).unlink()
            if request["authority"].get("answer"):
                content += "\n" + request["authority"]["answer"]["text"] + "\n"
            output = provider.repository / destination
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content)
            git(provider.repository, "add", "--", *paths)
            git(provider.repository, "commit", "-m", target, "--", *paths)
            mutations.append(target)
            value = {
                "operation_id": request["operation_id"],
                "before_revision": before["revision"],
                "commit": git(provider.repository, "rev-parse", "HEAD"),
                "after": {
                    "item_id": item_id,
                    "path": destination,
                    "state": target,
                    "owner": owner,
                    "original_high": before["original_high"],
                },
            }
            if target == "User Action Required":
                value["question"] = request["authority"]["question"]
        return envelope(stage, role, value, kwargs)

    monkeypatch.setattr(app, "invoke", native_agent)
    return app, item, packet, supplied, runtime, calls, mutations, prompts, question


def test_ready_recovery_public_import_then_separate_reservation(
    config_file, provider, tmp_path, monkeypatch
):
    app, item, packet, supplied, _, calls, mutations, prompts, question = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    provider_head = git(provider.repository, "rev-parse", "HEAD")
    result = asyncio.run(recover_item(app, item.item_id, supplied))
    assert result == {
        "item_id": item.item_id,
        "state": "Ready",
        "recovery_prepared": True,
        "candidate": packet["candidate"]["head"],
    }
    assert app.provider.item(item.item_id) == item
    recovery_packet = app.recovery_record(item.item_id)["packet"]
    assert recovery_packet["previous_owner"] == item.owner
    assert recovery_packet["preserved_execution"] == packet["preserved_execution"]
    assert git(provider.repository, "rev-parse", "HEAD") == provider_head
    assert mutations == [] and len(calls) == 1
    assert asyncio.run(recover_item(app, item.item_id, supplied)) == result
    assert len(calls) == 1
    imported = Path(app.config.data["candidate_root"]) / component(item.item_id)
    assert git(imported, "rev-parse", "HEAD") == packet["candidate"]["head"]
    assert git(imported, "status", "--porcelain") == ""
    result = asyncio.run(app.run_item(item.item_id))
    assert result["state"] == "User Action Required"
    assert mutations == ["Starting", "Running", "User Action Required"]
    assert calls[1] == "admit" and "accept" in calls and "produce-review" in calls
    assert app.provider.question(app.provider.item(item.item_id))["text"] == question["text"]
    assert "Attempt: 3" in app.provider.item(item.item_id).content
    assert git(imported, "rev-parse", "HEAD") == packet["candidate"]["head"]
    assert git(imported, "status", "--porcelain") == ""
    assert (
        json.loads(app._stage_path(item.item_id, "base").read_text())["commit"]
        == packet["candidate"]["base"]
    )
    prompt = prompts["produce-review"]
    assert packet["preserved_execution"]["remaining_work"] in prompt
    assert "no source production" in prompt and "Preserve prior attempts" in prompt
    assert "Do not integrate or claim completion before approval" in prompt
    assert all("browser checks remain required" not in p for p in prompts.values())
    receipts = list((app.root / "provider-agent-operations").glob("*/receipt.json"))
    assert len(receipts) == 3
    assert all(json.loads(path.read_text())["advancement_verified"] for path in receipts)
    assert app.usage_view(item.item_id)["generated_tokens"] == 2
    review = json.loads(app._stage_path(item.item_id, "review").read_text())
    assert review["candidate"] == packet["candidate"]["head"]
    assert review["verdict"] == "ACCEPT" and review["fresh_context"]
    assert review["native_verified"]
    assert not app._stage_path(item.item_id, "delivery").exists()
    before_replay = (len(calls), list(mutations))
    with pytest.raises(TransitionBlocked, match="already advanced"):
        asyncio.run(recover_item(app, item.item_id, supplied))
    assert (len(calls), mutations) == before_replay


@pytest.mark.parametrize("defect", ["runtime", "receipt", "candidate"])
def test_ready_recovery_public_blocks_invalid_evidence(
    config_file, provider, tmp_path, monkeypatch, defect
):
    app, item, packet, supplied, runtime, _, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    if defect == "runtime":
        runtime.write_text(runtime.read_text() + "{}\n")
    elif defect == "receipt":
        Path(packet["preserved_execution"]["receipt"]["path"]).write_text("changed")
    else:
        (Path(packet["candidate"]["checkout"]) / "answer.txt").write_text("unapproved\n")
    before = git(provider.repository, "rev-parse", "HEAD")
    with pytest.raises(TransitionBlocked):
        asyncio.run(recover_item(app, item.item_id, supplied))
    assert mutations == [] and app.provider.item(item.item_id) == item
    assert git(provider.repository, "rev-parse", "HEAD") == before
    assert not app._stage_path(item.item_id, "assignment").exists()


def test_ready_recovery_cannot_complete_without_exact_candidate_approval(
    config_file, provider, tmp_path, monkeypatch
):
    app, item, packet, supplied, _, _, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch, completion=True
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    with pytest.raises(TransitionBlocked, match="approval"):
        asyncio.run(app.run_item(item.item_id))
    assert mutations == ["Starting", "Running"]
    assert app.provider.item(item.item_id).state == "Running"
    imported = Path(app.config.data["candidate_root"]) / component(item.item_id)
    assert git(imported, "rev-parse", "HEAD") == packet["candidate"]["head"]
    assert git(imported, "status", "--porcelain") == ""


def test_ready_recovery_replay_rejects_changed_import(config_file, provider, tmp_path, monkeypatch):
    app, item, _packet, supplied, _, calls, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    imported = Path(app.config.data["candidate_root"]) / component(item.item_id)
    (imported / "answer.txt").write_text("unapproved change\n")
    with pytest.raises(TransitionBlocked, match="Imported preserved candidate changed"):
        asyncio.run(recover_item(app, item.item_id, supplied))
    assert len(calls) == 1 and mutations == []


def test_ready_recovery_rejects_unbound_historical_owner(
    config_file, provider, tmp_path, monkeypatch
):
    app, item, _packet, supplied, _, _, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    external = app.invoke

    async def wrong_owner(*args, **kwargs):
        result = await external(*args, **kwargs)
        value = json.loads(result["text"])
        value["previous_owner"] = "unrelated-execution"
        return {**result, "text": json.dumps(value)}

    monkeypatch.setattr(app, "invoke", wrong_owner)
    with pytest.raises(TransitionBlocked, match="did not authorize"):
        asyncio.run(recover_item(app, item.item_id, supplied))
    assert not mutations and not app._stage_path(item.item_id, "assignment").exists()


def test_normal_queue_waits_for_committed_provider_result(
    config_file, provider, tmp_path, monkeypatch
):
    from backlog_harness.coordination import RunController

    config_file[1]["poll_seconds"] = 0.01
    app, item, packet, supplied, _, calls, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    external = app.invoke
    committed, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    interrupted = []

    async def delayed_result(*args, **kwargs):
        result = await external(*args, **kwargs)
        value = json.loads(result["text"])
        if value.get("after", {}).get("state") == "Starting":
            committed.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.append(args[1])
                raise
        return result

    monkeypatch.setattr(app, "invoke", delayed_result)

    def publish(value):
        if value.get("result", {}).get("state") == "User Action Required":
            settled.set()

    async def exercise():
        controller = RunController(app, publish)
        running = asyncio.create_task(controller.run("until-terminal"))
        try:
            await asyncio.wait_for(committed.wait(), 5)
            # The provider has committed, but the external invocation has not returned.
            with pytest.raises(TransitionBlocked, match="observation is stale"):
                app.provider.snapshot()
            await asyncio.sleep(0.15)
            assert not running.done() and not interrupted
            assert mutations == ["Starting"]
            release.set()
            await asyncio.wait_for(settled.wait(), 10)
            await controller.stop()
            await running
        finally:
            release.set()
            if not running.done():
                await controller.stop()
                await running

    asyncio.run(exercise())
    assert not interrupted
    assert mutations == ["Starting", "Running", "User Action Required"]
    assert calls.count("admit") == 1 and calls.count("produce-review") == 1
    assert app.provider.item(item.item_id).state == "User Action Required"
    assert (
        app.recovery_record(item.item_id)["packet"]["preserved_execution"]
        == packet["preserved_execution"]
    )
    receipts = [
        json.loads(p.read_text())
        for p in (app.root / "provider-agent-operations").glob("*/receipt.json")
    ]
    reservations = [r for r in receipts if r["after"]["state"] == "Starting"]
    assert len(reservations) == 1 and reservations[0]["advancement_verified"]


@pytest.mark.parametrize(
    "invalid", [None, "owner", "writable", "budget", "later_effect", "action", "operation"]
)
def test_normal_queue_reconciles_interrupted_committed_reservation(
    config_file, provider, tmp_path, monkeypatch, invalid
):
    from backlog_harness.contracts import AgentBinding
    from backlog_harness.coordination import RunController
    from backlog_harness.evidence import EvidenceStore
    from backlog_harness.recovery_flow import reconcile_starting_effect
    from backlog_harness.runtime import SessionHandle

    config_file[1]["poll_seconds"] = 0.01
    app, item, packet, supplied, _, calls, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    external = app.invoke
    interrupted = {}

    async def interrupt_after_commit(*args, **kwargs):
        result = await external(*args, **kwargs)
        value = json.loads(result["text"])
        if value.get("after", {}).get("state") == "Starting":
            result["outcome"] = "unresolved"
            operation_record = json.loads(
                (
                    app.root
                    / "provider-agent-operations"
                    / component(kwargs["provider_operation"])
                    / "requested.json"
                ).read_text()
            )
            result["request_digest"] = digest(
                [
                    "provider",
                    operation_record["repository"],
                    False,
                    digest(operation_record),
                    operation_record["prompt"],
                ]
            )
            store = EvidenceStore(app.root, "item:" + item.item_id)
            path = store.begin(
                item.item_id + ":" + args[1],
                result["invocation_id"],
                app.config,
                app.config.binding("coordinator"),
                action=args[1],
                request_digest=result["request_digest"],
            )
            EvidenceStore.requested(path)
            EvidenceStore.outcome(path, "unresolved", failure_reason="CancelledError")
            atomic_json(path / "process.json", {"pid": 99999999, "started": "stopped"})
            atomic_json(
                path / "execution-context.json",
                {
                    "purpose": "provider",
                    "read_only": False,
                    "provider_operation": kwargs["provider_operation"],
                },
            )
            result["evidence_path"] = str(path)
            result["telemetry"]["coverage"] = "observed_unverified"
            atomic_json(app._stage_path(item.item_id, args[1]), result)
            interrupted.update(result=result, value=value, operation=kwargs["provider_operation"])
            raise RuntimeError("Interrupted after commit; usage incomplete")
        return result

    async def run_to_event(predicate):
        settled = asyncio.Event()
        controller = RunController(app, lambda value: settled.set() if predicate(value) else None)
        task = asyncio.create_task(controller.run("until-terminal"))
        try:
            await asyncio.wait_for(settled.wait(), 10)
            await controller.stop()
            await task
        finally:
            if not task.done():
                await controller.stop()
                await task

    monkeypatch.setattr(app, "invoke", interrupt_after_commit)
    asyncio.run(run_to_event(lambda value: "blocked" in value))
    assert mutations == ["Starting"]
    assert calls.count("admit") == 1
    original = interrupted["result"]
    session = original["session"]
    monkeypatch.setattr(app, "invoke", external)
    result = asyncio.run(
        app.invoke(
            item.item_id,
            "reservation-effect-reconciliation",
            "coordinator",
            json.dumps(interrupted["value"]),
            session=SessionHandle(
                session["session_id"],
                session["native_session_id"],
                AgentBinding(**session["binding"]),
            ),
            read_only=True,
            purpose="provider",
        )
    )
    if invalid == "owner":
        value = json.loads(result["text"])
        value["after"]["owner"] = "other"
        result["text"] = json.dumps(value)
    if invalid == "writable":
        atomic_json(
            Path(result["evidence_path"]) / "execution-context.json",
            {"purpose": "provider", "read_only": False},
        )
    if invalid in {"action", "operation"}:
        intent_path = Path(result["evidence_path"]) / "intent.json"
        intent = json.loads(intent_path.read_text())
        intent["action" if invalid == "action" else "operation_id"] = (
            "unrelated-read-only-operation"
        )
        atomic_json(intent_path, intent)
    if invalid == "budget":
        result["events"][-1]["usage"]["output_tokens"] = 999999999
    if invalid == "later_effect":
        canonical = provider.repository / item.path
        canonical.write_text(canonical.read_text() + "\nLater conflicting effect\n")
        git(provider.repository, "add", "--", item.path)
        git(provider.repository, "commit", "-m", "Later effect", "--", item.path)
    proof = app._stage_path(item.item_id, "reservation-effect-reconciliation")
    atomic_json(proof, result)
    root = app.root / "provider-agent-operations" / component(interrupted["operation"])
    if invalid:
        with pytest.raises(TransitionBlocked):
            reconcile_starting_effect(app, interrupted["operation"], proof)
        assert not (root / "effect-reconciliation.json").exists()
        assert mutations == ["Starting"]
        return
    reconciled = reconcile_starting_effect(app, interrupted["operation"], proof)
    assert reconciled["effect_verified"] and not reconciled["original_advancement_verified"]
    assert reconcile_starting_effect(app, interrupted["operation"], proof) == reconciled
    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["advancement_verified"] is False
    assert (
        json.loads(app._stage_path(item.item_id, original["invocation_id"]).read_text())["outcome"]
        == "unresolved"
    )
    assert (
        json.loads((root / "effect-reconciliation.json").read_text())["original_usage_coverage"]
        == "incomplete"
    )
    (app.root / "scheduling-blocks.json").unlink()
    asyncio.run(
        run_to_event(lambda value: value.get("result", {}).get("state") == "User Action Required")
    )
    assert mutations == ["Starting", "Running", "User Action Required"]
    assert calls.count("admit") == 1 and calls.count("produce-review") == 1
    assert (
        app.recovery_record(item.item_id)["packet"]["preserved_execution"]
        == packet["preserved_execution"]
    )
    assert not app._stage_path(item.item_id, "delivery").exists()


def test_normal_queue_continues_same_blocked_preserved_execution(
    config_file, provider, tmp_path, monkeypatch
):
    from backlog_harness.coordination import RunController
    from backlog_harness.recovery_flow import register_work_continuation

    config_file[1]["poll_seconds"] = 0.01
    app, item, packet, supplied, _, calls, mutations, prompts, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    external = app.invoke
    blocked, settled = asyncio.Event(), asyncio.Event()
    original = {}

    async def unfinished_proof(*args, **kwargs):
        result = await external(*args, **kwargs)
        if args[1] == "produce-review":
            value = json.loads(result["text"])
            value.pop("question", None)
            value.update(
                request_completion=False,
                status="blocked",
                blockers=["Missing exact semantic proof"],
            )
            result["text"] = json.dumps(value)
            atomic_json(app._stage_path(item.item_id, "produce-review"), result)
            original.update(result)
        return result

    monkeypatch.setattr(app, "invoke", unfinished_proof)
    instruction = tmp_path / "followup.txt"
    instruction.write_text(
        "Complete only missing semantic proof; reuse valid source review. No broad suite or source changes."
    )

    async def exercise():
        def publish(value):
            if "blocked" in value:
                blocked.set()
            if value.get("result", {}).get("state") == "User Action Required":
                settled.set()

        controller = RunController(app, publish)
        task = asyncio.create_task(controller.run("until-terminal"))
        try:
            await asyncio.wait_for(blocked.wait(), 10)
            await asyncio.sleep(0.03)
            request = register_work_continuation(app, item.item_id, instruction)
            assert register_work_continuation(app, item.item_id, instruction) == request
            from backlog_harness.recovery_flow import work_continuation

            continuation_path = app._stage_path(item.item_id, "work-continuation")
            frozen = json.loads(continuation_path.read_text())
            acceptance = json.loads(app._stage_path(item.item_id, "accept").read_text())
            for key in (
                "owner",
                "revision",
                "candidate",
                "previous_result_digest",
                "instruction",
                "extra_instruction",
            ):
                atomic_json(continuation_path, {**frozen, key: "changed"})
                with pytest.raises(TransitionBlocked, match="registration changed"):
                    work_continuation(app, item.item_id, acceptance)
                assert controller.eligible(app.provider.snapshot()) == []
            atomic_json(continuation_path, frozen)
            await asyncio.wait_for(settled.wait(), 10)
            await controller.stop()
            await task
            return request
        finally:
            if not task.done():
                await controller.stop()
                await task

    request = asyncio.run(exercise())
    assert calls.count("produce-review") == 1 and calls.count(request["stage"]) == 1
    assert calls.count("admit") == 1 and calls.count("accept") == 1
    assert mutations == ["Starting", "Running", "User Action Required"]
    assert json.loads(app._stage_path(item.item_id, "produce-review").read_text()) == original
    final = json.loads(app._stage_path(item.item_id, request["stage"]).read_text())
    assert final["session"] == original["session"]
    assert instruction.read_text() in prompts[request["stage"]]
    assert (
        app.recovery_record(item.item_id)["packet"]["candidate"]["head"]
        == packet["candidate"]["head"]
    )
    assert not app._stage_path(item.item_id, "delivery").exists()
    with pytest.raises(TransitionBlocked, match="Running preserved"):
        register_work_continuation(app, item.item_id, instruction)


@pytest.mark.parametrize(
    "relocate,review_fault",
    [
        (False, None),
        (True, None),
        (False, "missing"),
        (False, "inherited"),
        (False, "wrong-proof"),
        (False, "reject"),
    ],
)
def test_retained_blocked_continuation_can_run_scoped_proof_without_readmission(
    config_file, provider, tmp_path, monkeypatch, relocate, review_fault
):
    from dataclasses import replace

    from backlog_harness.adapters.codex.adapter import CodexAdapter
    from backlog_harness.contracts import freeze, load_config, plain
    from backlog_harness.evidence import EvidenceStore
    from backlog_harness.recovery_flow import register_work_continuation, run_artifact_proof
    from backlog_harness.runtime import AgentRequest

    config_file[1]["operational_root"] = str(tmp_path / "legacy-execution-evidence")
    app, item, packet, supplied, _, calls, mutations, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    external = app.invoke
    native_app = app
    proof_calls = []

    async def blocked_then_proof(item_id, stage, role, prompt, **kwargs):
        if kwargs.get("purpose") == "proof":
            saved = app._stage_path(item_id, stage)
            if saved.exists():
                return json.loads(saved.read_text())
            assert "session" not in kwargs
            proof_calls.append(stage)
            data = plain(app.config.data)
            data["workspace"] = str(app.candidate_repository(item_id))
            snapshot = replace(app.config, data=freeze(data))
            binding = snapshot.binding(role)
            operation = item_id + ":" + stage
            operation_path = (
                EvidenceStore(app.root, "item:" + item_id).run / "operations" / component(operation)
            )
            (intent_path,) = operation_path.glob("invocations/*/intent.json")
            path = intent_path.parent
            intent = json.loads(intent_path.read_text())
            invocation_id = intent["invocation_id"]
            assert intent["binding"] == asdict(binding)
            assert intent["request_digest"] == digest(
                ["proof", str(snapshot.repository), False, None, prompt]
            )
            EvidenceStore.requested(path)
            request = AgentRequest(
                operation,
                invocation_id,
                snapshot,
                binding,
                prompt,
                path,
                None,
                read_only=False,
                purpose="proof",
            )
            contract = CodexAdapter.prepare_proof_output(request)
            output = Path(contract["path"]) / "proof.json"
            output.write_text(json.dumps({"candidate": packet["candidate"]["head"]}))
            # A reviewer can read the exact output; its presence is not an acceptance verdict.
            assert json.loads(output.read_text())["candidate"] == packet["candidate"]["head"]
            result = json.loads(app._stage_path(item_id, "produce-review").read_text())
            result.update(
                invocation_id=invocation_id,
                purpose="proof",
                binding=asdict(binding),
                evidence_path=str(path),
            )
            result["session"] = {
                "session_id": "proof-owner",
                "native_session_id": str(uuid5(NAMESPACE_URL, "proof-owner")),
                "binding": asdict(binding),
            }
            result["events"] = [
                {
                    "at": "2026-10-01T12:01:00Z",
                    "type": "turn.completed",
                    "usage": {"output_tokens": 1},
                }
            ]

            def artifact(path):
                return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}

            verdict = {
                "candidate": packet["candidate"]["head"],
                "verdict": "ACCEPT",
                "blockers": [],
                "writes_performed": False,
            }
            review_path = output.parent / "review.json"
            atomic_json(review_path, verdict)
            reviewer = str(uuid5(NAMESPACE_URL, "proof-review"))
            native_parent = result["session"]["native_session_id"]
            logs = tmp_path / "native-home/sessions/2026/10/01"
            records = {
                native_parent: [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "spawn_agent",
                            "call_id": "proof-review",
                            "arguments": json.dumps({"fork_turns": "none"}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "proof-review",
                            "output": reviewer,
                        },
                    },
                ],
                reviewer: [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": reviewer,
                            "source": {
                                "subagent": {"thread_spawn": {"parent_thread_id": native_parent}}
                            },
                            "git": {"commit_hash": packet["candidate"]["head"]},
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": json.dumps(verdict),
                        },
                    },
                ],
            }
            for identity, rows in records.items():
                (logs / f"rollout-{identity}.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows)
                )
            manifest = output.parent / "manifest.json"
            atomic_json(
                manifest,
                {
                    "item_id": item_id,
                    "candidate": packet["candidate"]["head"],
                    "status": "evidence-ready",
                    "artifacts": [artifact(output)],
                    "independent_result_review": {
                        **artifact(review_path),
                        "identity": reviewer,
                        "verdict": "ACCEPT",
                    },
                },
            )
            result["text"] = json.dumps(
                {
                    "item_id": item_id,
                    "candidate": packet["candidate"]["head"],
                    "status": "evidence-ready",
                    "artifacts": [artifact(manifest), artifact(review_path)],
                }
            )
            atomic_json(saved, result)
            return result
        result = await external(item_id, stage, role, prompt, **kwargs)
        if stage.startswith("continue-proof-"):
            result["events"][-1]["usage"]["output_tokens"] = 4
            atomic_json(app._stage_path(item_id, stage), result)
        if stage.startswith("continue-proof-") and review_fault:
            value = json.loads(result["text"])
            if review_fault == "missing":
                value.pop("proof_reviewer_session")
                result["text"] = json.dumps(value)
            else:
                identity = (
                    result["session"]["native_session_id"]
                    if review_fault == "inherited"
                    else value["proof_reviewer_session"]
                )
                log = tmp_path / "native-home/sessions/2026/10/01" / f"rollout-{identity}.jsonl"
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                if review_fault == "inherited":
                    rows[0]["payload"]["arguments"] = json.dumps({"fork_turns": "all"})
                else:
                    verdict = json.loads(rows[-1]["payload"]["last_agent_message"])
                    verdict[
                        "proof_result_digest" if review_fault == "wrong-proof" else "verdict"
                    ] = "wrong"
                    rows[-1]["payload"]["last_agent_message"] = json.dumps(verdict)
                log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        if stage == "produce-review" or stage.startswith("continue-work-"):
            value = json.loads(result["text"])
            value.pop("question", None)
            value.update(request_completion=False, status="blocked")
            if stage == "produce-review":
                value["blockers"] = ["Missing semantic proof"]
            else:
                result["events"][-1]["usage"]["output_tokens"] = 3
                value["blocker"] = {
                    "operation": "historical output mkdir",
                    "error": "PermissionError",
                    "required_action": "Provide scoped artifact output",
                }
            result["text"] = json.dumps(value)
            atomic_json(app._stage_path(item_id, stage), result)
        return result

    monkeypatch.setattr(app, "invoke", blocked_then_proof)
    with pytest.raises(TransitionBlocked, match="Missing semantic proof"):
        asyncio.run(app.run_item(item.item_id))
    instruction = tmp_path / "proof.txt"
    instruction.write_text("Reuse captures and run missing proof only; preserve all denials.")
    registered = register_work_continuation(app, item.item_id, instruction)
    with pytest.raises(TransitionBlocked, match="Provide scoped artifact output"):
        asyncio.run(app.run_item(item.item_id))
    owner = app.provider.item(item.item_id).owner
    original = app._stage_path(item.item_id, registered["stage"]).read_bytes()
    for field, invalid in (
        ("candidate", "different"),
        ("item_id", "other"),
        ("request_completion", True),
    ):
        changed = json.loads(original)
        value = json.loads(changed["text"])
        value[field] = invalid
        changed["text"] = json.dumps(value)
        atomic_json(app._stage_path(item.item_id, registered["stage"]), changed)
        with pytest.raises(TransitionBlocked, match="owned blocked continuation"):
            asyncio.run(run_artifact_proof(app, item.item_id, instruction))
        assert proof_calls == []
    app._stage_path(item.item_id, registered["stage"]).write_bytes(original)
    # Explicit proof work uses the fixed evidence directory without an output flag.
    config, data = config_file
    prepared = asyncio.run(run_artifact_proof(app, item.item_id, instruction, prepare_only=True))
    assert prepared == asyncio.run(
        run_artifact_proof(app, item.item_id, instruction, prepare_only=True)
    )
    output_dir = Path(prepared["artifact_directory"])
    assert not output_dir.exists()
    assert not (output_dir.parent / "artifact-output.json").exists()
    assert not (output_dir.parent / "requested.json").exists()
    assert not (output_dir.parent / "process.json").exists()
    assert prepared["submitted"] is False and not proof_calls
    if relocate:
        old_root = app.root
        new_root = provider.repository / ".agent-ops/backlog-harness"
        new_root.parent.mkdir(parents=True, exist_ok=True)
        before = {
            str(p.relative_to(old_root)): p.read_bytes() for p in old_root.rglob("*") if p.is_file()
        }
        old_root.rename(new_root)
        old_root.symlink_to(new_root, target_is_directory=True)
        assert before == {
            str(p.relative_to(new_root)): p.read_bytes() for p in new_root.rglob("*") if p.is_file()
        }
        del data["operational_root"]
        config.write_text(yaml.safe_dump(data))
        app = Application(config)
        monkeypatch.setattr(app, "invoke", blocked_then_proof)
        with pytest.raises(TransitionBlocked, match="Prepared proof invocation changed"):
            asyncio.run(run_artifact_proof(app, item.item_id, instruction, prepare_only=True))
        # Only this never-submitted preparation is deliberately rebound. Original bytes survive.
        history = new_root / "relocation-history"
        history.mkdir()
        prior_id = json.loads((output_dir.parent / "intent.json").read_text())["invocation_id"]
        for name, field in (("intent.json", "config_digest"), ("config.json", "digest")):
            path = output_dir.parent / name
            (history / name).write_bytes(path.read_bytes())
            record = json.loads(path.read_text())
            record[field] = app.config.file_digest
            atomic_json(path, record)
        prepared = asyncio.run(
            run_artifact_proof(app, item.item_id, instruction, prepare_only=True)
        )
        output_dir = Path(prepared["artifact_directory"])
        assert output_dir.is_relative_to(new_root)
        assert (
            json.loads((output_dir.parent / "intent.json").read_text())["invocation_id"] == prior_id
        )
        assert app.provider.item(item.item_id).owner == owner
        assert app._stage_path(item.item_id, registered["stage"]).read_bytes() == original
    guard = app.enforce_guard

    async def held(_):
        raise TransitionBlocked("Usage hold")

    monkeypatch.setattr(app, "enforce_guard", held)
    with pytest.raises(TransitionBlocked, match="Usage hold"):
        asyncio.run(run_artifact_proof(app, item.item_id, instruction))
    assert not proof_calls and not (output_dir.parent / "requested.json").exists()
    monkeypatch.setattr(app, "enforce_guard", guard)
    result = asyncio.run(run_artifact_proof(app, item.item_id, instruction))
    assert (output_dir / "proof.json").is_file()
    assert result["workflow_advanced"] is False
    assert asyncio.run(run_artifact_proof(app, item.item_id, instruction)) == result
    assert len(proof_calls) == 1
    assert app.provider.item(item.item_id).owner == owner
    assert app.provider.item(item.item_id).state == "Running"
    assert mutations == ["Starting", "Running"]
    assert calls.count("admit") == calls.count("accept") == calls.count("produce-review") == 1
    assert app._stage_path(item.item_id, registered["stage"]).read_bytes() == original
    assert not app._stage_path(item.item_id, "delivery").exists()
    from backlog_harness.recovery_flow import validate_artifact_proof

    request = json.loads(app._stage_path(item.item_id, "artifact-proof-request").read_text())
    receipt = json.loads(app._stage_path(item.item_id, result["proof_stage"]).read_text())
    acceptance = json.loads(app._stage_path(item.item_id, "accept").read_text())
    for field, invalid in (
        ("item_id", "other"),
        ("candidate", "wrong"),
        ("status", "done"),
        ("artifacts", []),
        ("artifacts", [{"path": result["result"]["artifacts"][0]["path"], "sha256": "wrong"}]),
        ("artifacts", [{"path": str(provider.repository / "PROJECT.yaml"), "sha256": "bad"}]),
    ):
        changed = json.loads(json.dumps(receipt))
        value = json.loads(changed["text"])
        value[field] = invalid
        changed["text"] = json.dumps(value)
        with pytest.raises(TransitionBlocked):
            validate_artifact_proof(
                app, item.item_id, result["proof_stage"], request, changed, acceptance
            )
    changed = json.loads(json.dumps(receipt))
    changed["binding"]["permission_digest"] = "changed"
    with pytest.raises(TransitionBlocked, match="identity"):
        validate_artifact_proof(
            app, item.item_id, result["proof_stage"], request, changed, acceptance
        )
    instruction.write_text("Different work")
    with pytest.raises(TransitionBlocked, match="proof request changed"):
        asyncio.run(run_artifact_proof(app, item.item_id, instruction))
    assert len(proof_calls) == 1

    # Feed the verified auxiliary proof through the normal queue, retaining earlier results.
    from backlog_harness.coordination import RunController
    from backlog_harness.recovery_flow import register_proof_continuation

    data["poll_seconds"] = 0.01
    config.write_text(yaml.safe_dump(data))
    app.config = load_config(config)
    native_app.config = app.config
    instruction.write_text(
        "Consume existing proof; finish integration checks and request exact candidate approval."
    )
    handoff = register_proof_continuation(app, item.item_id, instruction)
    assert register_proof_continuation(app, item.item_id, instruction) == handoff
    monkeypatch.setattr(app, "enforce_guard", held)
    with pytest.raises(TransitionBlocked, match="Usage hold"):
        asyncio.run(app.run_item(item.item_id))
    assert handoff["stage"] not in calls
    monkeypatch.setattr(app, "enforce_guard", guard)
    frozen_path = app._stage_path(item.item_id, "proof-continuation")
    frozen = json.loads(frozen_path.read_text())
    controller = RunController(app, lambda _: None)
    for field in ("candidate", "owner", "revision", "proof_result_digest", "instruction"):
        atomic_json(frozen_path, {**frozen, field: "changed"})
        assert controller.eligible(app.provider.snapshot()) == []
    atomic_json(frozen_path, frozen)
    supporting = output_dir / "proof.json"
    valid_bytes = supporting.read_bytes()
    supporting.write_text("changed")
    assert controller.eligible(app.provider.snapshot()) == []
    supporting.write_bytes(valid_bytes)
    assert [i.item_id for i, _ in controller.eligible(app.provider.snapshot())] == [item.item_id]

    async def finish():
        settled = asyncio.Event()
        controller.publish = lambda v: (
            settled.set()
            if v.get("result", {}).get("state") == "User Action Required" or "blocked" in v
            else None
        )
        task = asyncio.create_task(controller.run("until-terminal"))
        try:
            await asyncio.wait_for(settled.wait(), 10)
        finally:
            await controller.stop()
            await task

    asyncio.run(finish())
    assert calls.count(handoff["stage"]) == 1
    assert calls.count("admit") == calls.count("accept") == calls.count("produce-review") == 1
    assert len(proof_calls) == 1
    if review_fault:
        assert mutations == ["Starting", "Running"]
        assert app.provider.item(item.item_id).state == "Running"
        assert not app._stage_path(item.item_id, "delivery").exists()
        return
    assert mutations == ["Starting", "Running", "User Action Required"]
    final = json.loads(app._stage_path(item.item_id, handoff["stage"]).read_text())
    assert final["session"] == acceptance["session"]
    assert app.provider.item(item.item_id).owner == owner
    assert app._stage_path(item.item_id, registered["stage"]).read_bytes() == original
    assert not app._stage_path(item.item_id, "delivery").exists()

    # An operator-answer continuation takes precedence over the old proof request revision.
    # Native CLIs may record the child task path without its resolved UUID in spawn output.
    from backlog_harness.native_evidence import verify_native_review

    native = final["session"]["native_session_id"]
    child_id = json.loads(final["text"])["proof_reviewer_session"]
    alias = "/root/proof_review"
    logs = tmp_path / "native-home/sessions/2026/10/01"
    parent_path = logs / f"rollout-{native}.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    parent[1]["payload"]["output"] = json.dumps({"task_name": alias})
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in parent))
    child_path = logs / f"rollout-{child_id}.jsonl"
    child = [json.loads(line) for line in child_path.read_text().splitlines()]
    child[0]["payload"].update(parent_thread_id=native, agent_path=alias)
    child.insert(
        1,
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"output_tokens": 0}}},
        },
    )
    child_path.write_text("".join(json.dumps(row) + "\n" for row in child))
    review = verify_native_review(
        native, alias, packet["candidate"]["head"], tmp_path / "native-home/sessions"
    )
    atomic_json(app._stage_path(item.item_id, "proof-review"), review)
    current = replace(
        app.provider.item(item.item_id), state="Running", revision="after-approved-answer"
    )
    monkeypatch.setattr(app.provider, "item", lambda _: current)
    atomic_json(
        app._stage_path(item.item_id, "continuation"),
        {
            "stage": "approved-proof",
            "approval": {
                "item_id": item.item_id,
                "disposition": "approve",
                "question": {"candidate": packet["candidate"]["head"]},
                "answer": {"text": "yes", "digest": digest("yes")},
            },
        },
    )
    assert controller.eligible([current])

    async def approved(item_id, stage, role, prompt, **kwargs):
        assert stage == "approved-proof" and "include its reviewer_task" in prompt
        assert kwargs["session"].native_session_id == native
        result = dict(final)
        value = json.loads(result["text"])
        value.pop("question")
        value.update(request_completion=True, reviewer_session=alias, proof_reviewer_session=alias)
        result["text"] = json.dumps(value)
        return result

    def delivery_boundary(*args, **kwargs):
        raise RuntimeError("verified approval reached delivery boundary")

    monkeypatch.setattr(app, "invoke", approved)
    monkeypatch.setattr("backlog_harness.delivery.integrate", delivery_boundary)
    with pytest.raises(RuntimeError, match="verified approval reached delivery boundary"):
        asyncio.run(app.run_item(item.item_id))


@pytest.mark.parametrize("extra,status", [(3, "unknown"), (60, "crossed")])
def test_unreconciled_exported_usage_retains_lower_bound_and_generation_fence(
    config_file, provider, tmp_path, monkeypatch, extra, status
):
    app, item, _, supplied, _, _, _, _, _ = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(recover_item(app, item.item_id, supplied))
    asyncio.run(app.run_item(item.item_id))
    baseline = app.usage_view(item.item_id)
    assert baseline["status"] == "below" and baseline["may_generate"]
    stage = app._stage_path(item.item_id, "produce-review")
    result = json.loads(stage.read_text())
    sink = Sink(Path(result["telemetry_path"]), {})
    export = payload()
    span = export["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["spanId"] = "1234567890abcdef"
    span["attributes"] = [{"key": "gen_ai.usage.output_tokens", "value": {"intValue": str(extra)}}]
    sink.write(export)
    sink.write(export)  # Exact duplicate exports cannot increase the lower bound.
    result["telemetry"].update(span_count=len(sink.seen), evidence_sha256=sink.evidence_digest())
    atomic_json(stage, result)
    view = app.usage_view(item.item_id)
    assert view["status"] == status and not view["may_generate"]
    assert view["generated_tokens"] is None and view["coverage"] == "incomplete"
    assert view["generated_tokens_lower_bound"] == baseline["generated_tokens"] + extra
    assert view["ceiling"] == baseline["ceiling"]
    # Even a larger reviewed ceiling cannot turn incomplete accounting into permission.
    atomic_json(
        app._stage_path(item.item_id, "allowance"),
        {"original_high": baseline["original_high"], "ceiling": 10000},
    )
    raised = app.usage_view(item.item_id)
    assert raised["status"] == "unknown" and not raised["may_generate"]
    accepted_path = app._stage_path(item.item_id, "accept")
    accepted = json.loads(accepted_path.read_text())
    for usage in ({}, {"output_tokens": None}, {"output_tokens": "bad"}, {"output_tokens": -1}):
        changed = json.loads(json.dumps(accepted))
        changed["events"][-1]["usage"] = usage
        atomic_json(accepted_path, changed)
        malformed = app.usage_view(item.item_id)
        assert not malformed["may_generate"] and malformed["generated_tokens"] is None
        assert malformed["generated_tokens_lower_bound"] == view["generated_tokens_lower_bound"]
    atomic_json(accepted_path, accepted)
    from dataclasses import replace

    from backlog_harness.contracts import freeze, plain

    data = plain(app.config.data)
    data["generation_configuration_error"] = "invalid current configuration"
    app.config = replace(app.config, data=freeze(data))
    invalid = app.usage_view(item.item_id)
    assert invalid["status"] == "configuration_invalid" and not invalid["may_generate"]
    assert invalid["generated_tokens"] is None and invalid["coverage"] == "incomplete"
    assert invalid["generated_tokens_lower_bound"] == view["generated_tokens_lower_bound"]


def _awaiting_delivery(config_file, provider, tmp_path, monkeypatch):
    app, item, packet, supplied, _, calls, mutations, _, question = _public_case(
        config_file, provider, tmp_path, monkeypatch
    )
    exclude = provider.repository / ".git/info/exclude"
    exclude.write_text(exclude.read_text() + "\n.agent-ops/\n")
    # Give the provider repository the preserved source base, as a real checkout has.
    git(
        provider.repository,
        "fetch",
        str(packet["candidate"]["checkout"]),
        packet["candidate"]["base"],
    )
    git(provider.repository, "merge", "--allow-unrelated-histories", "--no-edit", "FETCH_HEAD")
    asyncio.run(recover_item(app, item.item_id, supplied))
    asyncio.run(app.run_item(item.item_id))
    current = app.provider.item(item.item_id)
    cached = json.loads(app.provider.cache_path.read_text())
    cached["transition_paths"] = {
        item.item_id: {"Completed": [current.path, "backlog/archive/item-one.md"]}
    }
    atomic_json(app.provider.cache_path, cached)
    authorization = {
        "item_id": item.item_id,
        "question_id": question["question_id"],
        "revision": current.revision,
        "candidate": packet["candidate"]["head"],
        "disposition": "approve",
        "answer": "Deliver the exact accepted candidate under my existing authorization.",
        "source_reference": "operator chat turn: authorized routine delivery",
    }
    path = tmp_path / "authorization.json"
    atomic_json(path, authorization)
    return app, item, path, calls, mutations


@pytest.mark.parametrize("interrupt_answer", [False, True])
def test_retained_delivery_completes_under_incomplete_usage_without_generation(
    config_file, provider, tmp_path, monkeypatch, interrupt_answer
):
    from backlog_harness.recovery_flow import authorize_retained_delivery

    app, item, path, calls, mutations = _awaiting_delivery(
        config_file, provider, tmp_path, monkeypatch
    )

    async def blocked(*args, **kwargs):
        raise AssertionError("No implementation generation may be attempted")

    monkeypatch.setattr(app, "enforce_guard", blocked)
    monkeypatch.setattr(
        app,
        "usage_view",
        lambda _: {
            "status": "crossed",
            "generated_tokens": None,
            "generated_tokens_lower_bound": 100,
            "may_generate": False,
        },
    )
    atomic_json(
        app._stage_path(item.item_id, "historical-unstructured"),
        {"role": "orchestrator", "text": "historical prose, not a conditional request"},
    )
    before = len(calls)
    if interrupt_answer:
        original_transition = app.transition

        async def interrupted(*args, **kwargs):
            if args[2] == "Running":
                raise RuntimeError("interrupt before authorized resume")
            return await original_transition(*args, **kwargs)

        monkeypatch.setattr(app, "transition", interrupted)
        with pytest.raises(RuntimeError, match="interrupt before"):
            asyncio.run(authorize_retained_delivery(app, item.item_id, path))
        monkeypatch.setattr(app, "transition", original_transition)
        answer = asyncio.run(app.resume_answer(item.item_id))
    else:
        answer = asyncio.run(authorize_retained_delivery(app, item.item_id, path))
    assert answer["state"] == "Running"
    result = asyncio.run(app.run_item(item.item_id))
    assert result["state"] == "Completed" and result["delivery"]["verified"]
    assert all(stage.startswith("provider-") for stage in calls[before:])
    assert mutations[-3:] == ["User Action Required", "Running", "Completed"]
    head = git(provider.repository, "rev-parse", "HEAD")
    count = len(calls)
    assert asyncio.run(authorize_retained_delivery(app, item.item_id, path)) == answer
    assert len(calls) == count and git(provider.repository, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("defect", ["candidate", "missing", "canonical", "invalid"])
def test_retained_delivery_rejects_unbound_authorization(
    config_file, provider, tmp_path, monkeypatch, defect
):
    from backlog_harness.recovery_flow import authorize_retained_delivery

    app, item, path, calls, mutations = _awaiting_delivery(
        config_file, provider, tmp_path, monkeypatch
    )
    authorization = json.loads(path.read_text())
    if defect == "candidate":
        authorization["candidate"] = "0" * 40
    elif defect == "missing":
        authorization.pop("source_reference")
    elif defect == "invalid":
        authorization["disposition"] = "decline"
    else:
        stage = app._stage_path(item.item_id, "produce-review")
        record = json.loads(stage.read_text())
        record["session"]["native_session_id"] = "wrong"
        atomic_json(stage, record)
    atomic_json(path, authorization)
    before = (len(calls), len(mutations))
    with pytest.raises(TransitionBlocked):
        asyncio.run(authorize_retained_delivery(app, item.item_id, path))
    assert (len(calls), len(mutations)) == before
    assert not app._stage_path(item.item_id, "retained-delivery-authorization").exists()


@pytest.mark.parametrize("fault", ["missing", "reject", "wrong-candidate"])
def test_retained_delivery_still_requires_exact_independent_review(
    config_file, provider, tmp_path, monkeypatch, fault
):
    from backlog_harness.recovery_flow import authorize_retained_delivery

    app, item, path, calls, mutations = _awaiting_delivery(
        config_file, provider, tmp_path, monkeypatch
    )
    asyncio.run(authorize_retained_delivery(app, item.item_id, path))
    produced = json.loads(app._stage_path(item.item_id, "produce-review").read_text())
    reviewer = json.loads(produced["text"])["reviewer_session"]
    log = tmp_path / "native-home/sessions/2026/10/01" / f"rollout-{reviewer}.jsonl"
    if fault == "missing":
        log.unlink()
    else:
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        value = json.loads(rows[-1]["payload"]["last_agent_message"])
        value["verdict" if fault == "reject" else "candidate"] = (
            "REJECT" if fault == "reject" else "0" * 40
        )
        rows[-1]["payload"]["last_agent_message"] = json.dumps(value)
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = (len(calls), len(mutations), git(provider.repository, "rev-parse", "HEAD"))
    with pytest.raises(TransitionBlocked):
        asyncio.run(app.run_item(item.item_id))
    assert (len(calls), len(mutations), git(provider.repository, "rev-parse", "HEAD")) == before
    assert not app._stage_path(item.item_id, "delivery").exists()
