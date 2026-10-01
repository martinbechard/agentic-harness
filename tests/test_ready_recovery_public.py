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
        atomic_json(
            native / "intent.json",
            {
                "invocation_id": stage,
                "request_digest": result["request_digest"],
                "binding": binding,
                "config_digest": app.config.file_digest,
                "item_id": None if result["purpose"] == "provider" else item.item_id,
                "action": stage,
                "operation_id": item.item_id + ":" + stage,
            },
        )
        atomic_json(app._stage_path(item.item_id, stage), result)
        return result

    async def native_agent(item_id, stage, role, prompt, **kwargs):
        saved = app._stage_path(item_id, stage)
        if saved.exists():
            return json.loads(saved.read_text())
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
        elif stage == "produce-review" or stage.startswith("continue-work-"):
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
                        "arguments": json.dumps({"fork_turns": "none"}),
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
                    "candidate": packet["candidate"]["head"],
                    "reviewer_session": reviewer,
                }
            )
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
            destination = paths[1] if target == "User Action Required" else paths[0]
            if target == "User Action Required":
                content += (
                    "\nQuestion ID: " + question["question_id"] + "\n" + question["text"] + "\n"
                )
                (provider.repository / paths[0]).unlink()
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
                value["question"] = question
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
