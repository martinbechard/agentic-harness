"""Focused recovery coverage for interrupted read-only provider observations."""

import asyncio
import json
import urllib.request
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from test_provider_coordination import policy_evidence

from backlog_harness.application import Application
from backlog_harness.contracts import digest, freeze, load_config, plain, utcnow
from backlog_harness.evidence import EvidenceStore, JsonlWriter, atomic_json, component
from backlog_harness.provider import TransitionBlocked
from backlog_harness.runtime import InvocationHandle
from backlog_harness.telemetry import Sink

SESSION_ID = "00000000-0000-4000-8000-000000000123"


def usage_span(output_tokens, end_ns, *, trace="a", span="b"):
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace * 32,
                                "spanId": span * 16,
                                "endTimeUnixNano": str(end_ns),
                                "attributes": [
                                    {
                                        "key": "gen_ai.usage.output_tokens",
                                        "value": {"intValue": output_tokens},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def prepare_interrupted_observation(config_file, provider, *, uncovered=False):
    config, data = config_file
    item = provider.item("item-one")
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    codex_home = config.parent / "codex-home"
    codex_home.mkdir()
    data["agent_clis"]["primary"]["adapter_options"] = {"codex_home": str(codex_home)}
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    snapshot = load_config(config)
    revision = app.provider.source_revision()
    observer = app.provider_observer_digest(snapshot)
    stage = "observe-" + digest([revision, observer])
    operation = "provider-inventory:" + stage
    provider_data = plain(snapshot.data)
    provider_data["workspace"] = str(snapshot.repository)
    snapshot = replace(snapshot, data=freeze(provider_data))
    binding = snapshot.binding("coordinator")
    store = EvidenceStore(app.root, "item:provider-inventory")
    path = store.begin(
        operation,
        "original-invocation",
        snapshot,
        binding,
        action=stage,
        request_digest="semantic-request",
    )
    EvidenceStore.requested(path)
    EvidenceStore.session(
        path,
        {
            "session_id": "portable-session",
            "native_session_id": SESSION_ID,
            "binding": asdict(binding),
        },
    )
    atomic_json(
        path / "execution-context.json", {"purpose": "provider", "provider_operation": None}
    )
    atomic_json(path / "process.json", {"pid": 99999999, "started": "stopped"})
    EvidenceStore.outcome(path, "unresolved", failure_reason="timeout")

    end_ns = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
    telemetry = app.root / "telemetry/original/spans.jsonl"
    JsonlWriter(telemetry).append(usage_span(10, end_ns))
    atomic_json(path / "telemetry.json", {"path": str(telemetry)})
    atomic_json(
        path / "telemetry-report.json",
        {
            "span_count": 1,
            "rejected_exports": 0,
            "evidence_sha256": Sink(telemetry, {}).evidence_digest(),
        },
    )
    native = Path(binding.auth_context) / "sessions/2026/10/01" / f"rollout-test-{SESSION_ID}.jsonl"
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-10-01T00:00:00+00:00",
            "payload": {"id": SESSION_ID, "timestamp": "2026-10-01T00:00:00+00:00"},
        },
        {
            "type": "token_usage_record",
            "timestamp": "2026-10-01T00:00:00+00:00",
            "payload": {
                "session_id": SESSION_ID,
                "thread_token_usage": {"output_tokens": 10},
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-10-01T00:00:00+00:00",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"output_tokens": 10}},
            },
        },
    ]
    if uncovered:
        records.append(
            {
                "type": "event_msg",
                "timestamp": "2026-10-01T00:00:01+00:00",
                "payload": {
                    "type": "item_completed",
                    "started_at_ms": end_ns // 1_000_000 + 1,
                    "item": {"type": "Reasoning"},
                },
            }
        )
    native.parent.mkdir(parents=True, exist_ok=True)
    for record in records:
        JsonlWriter(native).append(record)

    result = {
        "version": 1,
        "request_digest": "semantic-request",
        "invocation_id": "original-invocation",
        "outcome": "unresolved",
        "role": "coordinator",
        "purpose": "provider",
        "binding": asdict(binding),
        "session": {
            "session_id": "portable-session",
            "native_session_id": SESSION_ID,
            "binding": asdict(binding),
        },
        "text": None,
        "events": [],
        "telemetry": json.loads((path / "telemetry-report.json").read_text()),
        "telemetry_path": str(telemetry),
        "evidence_path": str(path),
    }
    atomic_json(app._stage_path("provider-inventory", stage), result)
    inventory = {
        "items": [asdict(item)],
        "policy": {
            "eligible": True,
            "mode": "SOLO",
            "primary_branch": "main",
            "evidence": policy_evidence(provider.repository),
        },
        "questions": {},
        "archive_debt": [],
        "non_items": [],
        "dependencies": {item.item_id: []},
        "transition_paths": {},
    }
    return app, operation, inventory


class ReturningResumeAdapter:
    def __init__(self, inventory):
        self.inventory = inventory
        self.calls = 0
        self.sessions = []
        self.requests = []

    def validate_profile(self, request):
        return {"production_ready": True}

    async def resume_session(self, session, request):
        self.calls += 1
        self.sessions.append(session)
        self.requests.append(request)
        assert session.native_session_id == SESSION_ID
        UUID(session.native_session_id)
        EvidenceStore.requested(request.evidence_path)
        EvidenceStore.session(
            request.evidence_path,
            {
                "session_id": session.session_id,
                "native_session_id": session.native_session_id,
                "binding": asdict(request.binding),
            },
        )
        payload = usage_span(5, 2_000_000_000, trace="c", span="d")
        body = json.dumps(payload).encode()
        call = urllib.request.Request(
            request.telemetry.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-harness-invocation": request.telemetry.token,
            },
        )
        response = await asyncio.to_thread(urllib.request.urlopen, call)
        try:
            assert response.status == 200
        finally:
            response.close()
        events = [
            {
                "version": 1,
                "event_id": "message",
                "invocation_id": request.invocation_id,
                "at": utcnow(),
                "type": "item.completed",
                "item_type": "agent_message",
                "text": json.dumps(self.inventory),
            },
            {
                "version": 1,
                "event_id": "complete",
                "invocation_id": request.invocation_id,
                "at": utcnow(),
                "type": "turn.completed",
                "usage": {"output_tokens": 15},
            },
        ]
        writer = JsonlWriter(request.evidence_path / "events.jsonl")
        for event in events:
            writer.append(event)
        native = next(
            (Path(request.binding.auth_context) / "sessions").glob(
                f"*/*/*/*{session.native_session_id}.jsonl"
            )
        )
        JsonlWriter(native).append(
            {
                "type": "token_usage_record",
                "timestamp": utcnow(),
                "payload": {
                    "session_id": session.native_session_id,
                    "thread_token_usage": {"output_tokens": 15},
                },
            }
        )
        JsonlWriter(native).append(
            {
                "type": "event_msg",
                "timestamp": utcnow(),
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"output_tokens": 15}},
                },
            }
        )
        EvidenceStore.outcome(request.evidence_path, "returned")
        return InvocationHandle(
            request.invocation_id,
            request.evidence_path,
            session=session,
            outcome="returned",
            events=events,
        )


def test_resumed_provider_observation_reuses_exact_session_and_retry_does_not_launch_again(
    config_file, provider, monkeypatch
):
    app, operation, inventory = prepare_interrupted_observation(config_file, provider)
    adapter = ReturningResumeAdapter(inventory)
    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: adapter)

    first = asyncio.run(app.resume_provider_observation(operation, SESSION_ID))
    second = asyncio.run(app.resume_provider_observation(operation, SESSION_ID))

    assert first == second
    assert adapter.calls == 1
    assert first["original_invocation_id"] == "original-invocation"
    assert first["continuation_invocation_id"] != "original-invocation"
    operation_path = (
        app.root
        / "runs"
        / component("item:provider-inventory")
        / "operations"
        / component(operation)
    )
    assert len(list(operation_path.glob("invocations/*/intent.json"))) == 2
    assert app.provider.observation()["invocation_id"] == first["continuation_invocation_id"]


def test_provider_observation_resume_uses_current_launch_tuning_and_budget(
    config_file, provider, monkeypatch
):
    app, operation, inventory = prepare_interrupted_observation(config_file, provider)
    config, data = config_file
    data["profiles"]["control"].update(model="current-model", effort="high")
    data["coordinator_limits"]["generated_tokens"] = 20
    config.write_text(yaml.safe_dump(data))
    adapter = ReturningResumeAdapter(inventory)
    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: adapter)

    result = asyncio.run(app.resume_provider_observation(operation, SESSION_ID))

    assert result["output_tokens"] == 15
    assert adapter.calls == 1
    assert adapter.sessions[0].binding != adapter.requests[0].binding
    assert (
        adapter.sessions[0].binding.permission_digest
        == adapter.requests[0].binding.permission_digest
    )
    assert adapter.requests[0].snapshot.data["coordinator_limits"]["generated_tokens"] == 20


@pytest.mark.parametrize(
    "case",
    ["identity", "permission", "budget", "live", "missing_usage", "uncovered_usage"],
)
def test_provider_observation_recovery_rejects_unsafe_original_evidence(
    config_file, provider, monkeypatch, case
):
    app, operation, inventory = prepare_interrupted_observation(
        config_file, provider, uncovered=case == "uncovered_usage"
    )
    native = SESSION_ID
    if case == "identity":
        native = "00000000-0000-4000-8000-000000000999"
    elif case == "permission":
        config, data = config_file
        data["profiles"]["control"]["permissions"] = ["workspace-write"]
        config.write_text(yaml.safe_dump(data))
    elif case == "budget":
        config, data = config_file
        data["profiles"]["control"].update(model="current-model", effort="high")
        data["coordinator_limits"]["generated_tokens"] = 10
        config.write_text(yaml.safe_dump(data))
    elif case == "live":
        monkeypatch.setattr(app, "process_stopped", lambda *_: False)
    elif case == "missing_usage":
        original = next(
            (app.root / "runs").glob("*/operations/*/invocations/*/telemetry-report.json")
        )
        original.unlink()
    adapter = ReturningResumeAdapter(inventory)
    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: adapter)

    with pytest.raises(TransitionBlocked):
        asyncio.run(app.resume_provider_observation(operation, native))

    assert adapter.calls == 0
    operation_path = (
        app.root
        / "runs"
        / component("item:provider-inventory")
        / "operations"
        / component(operation)
    )
    assert len(list(operation_path.glob("invocations/*/intent.json"))) == 1


@pytest.mark.parametrize("case", ["instructions", "skill", "provider_source"])
def test_provider_observation_recovery_rejects_semantic_or_source_drift(
    config_file, provider, monkeypatch, case
):
    app, operation, inventory = prepare_interrupted_observation(config_file, provider)
    _, data = config_file
    if case == "instructions":
        (provider.repository / "AGENTS.md").write_text("Changed provider instructions\n")
    elif case == "skill":
        skill = Path(data["methodology_root"]) / "skills/manage-work-items/SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("Changed provider management contract\n")
    else:
        item = provider.item("item-one")
        (provider.repository / item.path).write_text(
            (provider.repository / item.path).read_text() + "\nChanged source\n"
        )
    adapter = ReturningResumeAdapter(inventory)
    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: adapter)

    with pytest.raises(TransitionBlocked, match="Provider source or observation semantics changed"):
        asyncio.run(app.resume_provider_observation(operation, SESSION_ID))

    assert adapter.calls == 0
    operation_path = (
        app.root
        / "runs"
        / component("item:provider-inventory")
        / "operations"
        / component(operation)
    )
    assert len(list(operation_path.glob("invocations/*/intent.json"))) == 1


def test_child_usage_cannot_cover_later_unaccounted_parent_output(
    config_file, provider, monkeypatch
):
    app, operation, inventory = prepare_interrupted_observation(
        config_file, provider, uncovered=True
    )
    stage = operation.split(":", 1)[1]
    result = json.loads(app._stage_path("provider-inventory", stage).read_text())
    path = Path(result["evidence_path"])
    telemetry = Path(result["telemetry_path"])
    end_ns = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1_000_000_000)
    JsonlWriter(telemetry).append(usage_span(5, end_ns + 2_000_000_000, trace="c", span="d"))
    atomic_json(
        path / "telemetry-report.json",
        {
            "span_count": 2,
            "rejected_exports": 0,
            "evidence_sha256": Sink(telemetry, {}).evidence_digest(),
        },
    )
    monkeypatch.setattr("backlog_harness.native_evidence.child_usage", lambda *_: (5, ["child"]))
    adapter = ReturningResumeAdapter(inventory)
    monkeypatch.setattr("backlog_harness.application.AdapterRegistry.resolve", lambda *_: adapter)
    with pytest.raises(TransitionBlocked, match="uncovered model output"):
        asyncio.run(app.resume_provider_observation(operation, SESSION_ID))
    assert adapter.calls == 0
