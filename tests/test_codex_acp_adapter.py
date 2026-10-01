"""Scripted ACP boundary tests: native-shaped evidence, no model or adapter launch."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from backlog_harness.adapters.codex.acp_adapter import CodexAcpAdapter, _Client
from backlog_harness.contracts import digest, load_config
from backlog_harness.evidence import EvidenceStore
from backlog_harness.provider import TransitionBlocked
from backlog_harness.runtime import AgentRequest
from backlog_harness.telemetry import TelemetryDestination


class Value:
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self, **kwargs):
        return self.__dict__.copy()


@pytest.fixture
def request_factory(config_file, tmp_path):
    config, data = config_file
    home = tmp_path / "auth"
    home.mkdir()
    (home / "config.toml").write_text("")
    data["agent_clis"]["primary"]["adapter_options"] = {
        "load_user_config": True,
        "codex_home": str(home),
    }
    config.write_text(yaml.safe_dump(data))

    def make(stage="one", **changes):
        snapshot = load_config(config)
        binding = snapshot.binding("orchestrator")
        invocation = str(uuid4())
        store = EvidenceStore(snapshot.operational_root, "item:one")
        path = store.begin(
            stage, invocation, snapshot, binding, action=stage, request_digest=digest("assignment")
        )
        telemetry = path / "telemetry.jsonl"
        destination = TelemetryDestination(
            invocation, "http://localhost:9999/trace", "secret-export-token", telemetry
        )
        return AgentRequest(
            stage, invocation, snapshot, binding, "assignment", path, destination, **changes
        )

    return make, config, data


class ScriptedConnection:
    def __init__(self, adapter, request, client):
        self.adapter, self.request, self.client = adapter, request, client
        self.loaded = None

    async def new_session(self, **kwargs):
        self.adapter.calls.append(("new", kwargs))
        self.native = str(uuid4())
        return Value(session_id=self.native, actual_response="fixture SDK response")

    async def load_session(self, **kwargs):
        self.adapter.calls.append(("load", kwargs))
        self.native = kwargs["session_id"]
        self.loaded = self.native
        await self.client.session_update(self.native, Value(sessionUpdate="usage_update", used=999))
        return Value(actual_response="fixture load response")

    async def set_session_mode(self, identity, mode):
        self.adapter.calls.append(("mode", identity, mode))

    async def prompt(self, identity, blocks):
        self.adapter.calls.append(("prompt", identity, blocks[0].text))
        if self.adapter.fault == "timeout":
            await asyncio.sleep(10)
        if self.adapter.fault == "cancel":
            raise asyncio.CancelledError()
        await self.client.session_update(
            identity, Value(sessionUpdate="usage_update", used=125, size=1000)
        )
        await self.client.session_update(
            identity,
            Value(sessionUpdate="agent_thought_chunk", content={"text": "secret thoughts"}),
        )
        turn = str(uuid4())
        path = (
            Path(self.request.binding.auth_context)
            / "sessions/2026/10/01"
            / f"rollout-{identity}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"type": "session_meta", "payload": {"id": identity}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
            {"type": "turn_context", "payload": {"turn_id": turn}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": blocks[0].text}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"output_tokens": 25}},
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn,
                    "last_agent_message": '{"candidate":"fixture"}',
                },
            },
        ]
        if self.adapter.fault == "native-failure":
            rows[-1]["payload"]["error"] = {"message": "fixture error"}
        if self.adapter.fault == "missing-native":
            rows.pop()
        if self.adapter.fault == "unknown-usage":
            rows.pop(4)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return Value(stop_reason="end_turn")

    async def close(self):
        self.adapter.calls.append(("close",))


class ScriptedAdapter(CodexAcpAdapter):
    def __init__(self, fault=None):
        super().__init__()
        self.fault, self.calls, self.environments, self.children = fault, [], [], []

    async def _launch(self, request, cwd, env):
        self.request = request
        self.environments.append(env)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self.children.append(process)
        return process

    async def _connect(self, process, client):
        client.auth.set_result(
            {"kind": "apiKey"}
            if self.fault == "auth"
            else {"kind": "account", "label": "ChatGPT Pro"}
        )
        return ScriptedConnection(self, self.request, client)


def assert_gone(adapter):
    assert not adapter.processes
    for process in adapter.children:
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)


def test_normalizes_native_completion_and_preserves_actual_creation(request_factory):
    make, *_ = request_factory
    request = make()
    adapter = ScriptedAdapter()
    handle = asyncio.run(adapter.start_session(request))
    assert handle.outcome == "returned"
    assert handle.events[-2]["usage"] == {"output_tokens": 25}
    assert handle.events[-1]["text"] == '{"candidate":"fixture"}'
    assert not any("secret thoughts" in json.dumps(event) for event in handle.events)
    exchange = json.loads((request.evidence_path / "session-exchange.json").read_text())
    assert exchange["response"]["actual_response"] == "fixture SDK response"
    assert exchange["method"] == "session/new"
    assert exchange["invocation_id"] == request.invocation_id
    assert_gone(adapter)
    recovered = asyncio.run(adapter.reconcile(handle))
    assert recovered["events"][-1]["text"] == handle.events[-1]["text"]
    assert len([call for call in adapter.calls if call[0] == "prompt"]) == 1


@pytest.mark.parametrize("fault", ["native-failure", "missing-native", "auth", "timeout"])
def test_unproven_completion_and_auth_block_with_cleanup(request_factory, fault):
    make, *_ = request_factory
    request = make(timeout_seconds=0.02)
    adapter = ScriptedAdapter(fault)
    handle = asyncio.run(adapter.start_session(request))
    assert handle.outcome == "unresolved"
    assert not any(event["type"] == "turn.completed" for event in handle.events)
    if fault == "auth":
        assert not adapter.calls or adapter.calls == [("close",)]
        assert not (request.evidence_path / "requested.json").exists()
    assert_gone(adapter)


def test_cancellation_cleans_and_preserves_unknown_submission(request_factory):
    make, *_ = request_factory
    request = make()
    adapter = ScriptedAdapter("cancel")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.start_session(request))
    assert EvidenceStore.reconcile(request.evidence_path)["outcome"] == "unresolved"
    assert_gone(adapter)


def test_missing_usage_does_not_promote_acp_usage_summary(request_factory):
    make, *_ = request_factory
    adapter = ScriptedAdapter("unknown-usage")
    handle = asyncio.run(adapter.start_session(make()))
    assert handle.outcome == "returned"
    assert handle.events[-2]["usage"] is None
    assert any(event.get("used") == 125 for event in handle.events)


def test_resume_reloads_settings_same_session_and_ignores_history(request_factory):
    make, config, data = request_factory
    adapter = ScriptedAdapter()
    first = asyncio.run(adapter.start_session(make()))
    data["profiles"]["worker"]["effort"] = "high"
    config.write_text(yaml.safe_dump(data))
    request = make("answer")
    resumed = asyncio.run(adapter.resume_session(first.session, request))
    assert resumed.outcome == "returned"
    assert resumed.session.native_session_id == first.session.native_session_id
    assert len([call for call in adapter.calls if call[0] == "new"]) == 1
    assert len([call for call in adapter.calls if call[0] == "load"]) == 1
    assert not any(event.get("used") == 999 for event in resumed.events)
    assert json.loads(adapter.environments[-1]["CODEX_CONFIG"])["model_reasoning_effort"] == "high"
    assert (
        json.loads((request.evidence_path / "session-exchange.json").read_text())["method"]
        == "session/load"
    )
    assert_gone(adapter)


def test_resume_permission_change_blocks_before_launch(request_factory):
    make, *_ = request_factory
    adapter = ScriptedAdapter()
    first = asyncio.run(adapter.start_session(make()))
    request = make("answer")
    request = replace(request, binding=replace(request.binding, permission_digest="changed"))
    with pytest.raises(ValueError, match="permission"):
        asyncio.run(adapter.resume_session(first.session, request))
    assert len(adapter.children) == 1


def test_environment_scopes_memory_auth_and_attributed_exporter(request_factory, monkeypatch):
    make, *_ = request_factory
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden-key")
    monkeypatch.setenv("CODEX_CONFIG", "forbidden-settings")
    monkeypatch.setenv("APP_SERVER_LOGS", "forbidden-secret-log")
    request = make()
    adapter = ScriptedAdapter()
    asyncio.run(adapter.start_session(request))
    env = adapter.environments[0]
    settings = json.loads(env["CODEX_CONFIG"])
    assert settings["features.memories"] is False
    assert settings["forced_login_method"] == "chatgpt"
    assert settings["model_provider"] == "openai"
    assert settings["features.multi_agent"] is True
    assert settings["otel"]["log_user_prompt"] is False
    target = settings["otel"]["trace_exporter"]["otlp-http"]
    assert target["endpoint"] == request.telemetry.endpoint
    assert target["headers"]["x-harness-invocation"] == request.telemetry.token
    assert env["INITIAL_AGENT_MODE"] == "read-only"
    assert "OPENAI_API_KEY" not in env and "APP_SERVER_LOGS" not in env
    assert os.environ["CODEX_CONFIG"] == "forbidden-settings"
    assert not any(
        "secret-export-token" in p.read_text() for p in request.evidence_path.glob("*.json")
    )


@pytest.mark.parametrize("change", ["write", "proof", "ignore-user-config", "artifact"])
def test_unsupported_scope_rejected_before_child(request_factory, change):
    make, config, data = request_factory
    if change == "ignore-user-config":
        data["agent_clis"]["primary"]["adapter_options"]["load_user_config"] = False
    if change == "artifact":
        data["profiles"]["worker"]["artifact_output"] = True
    config.write_text(yaml.safe_dump(data))
    request = make(
        read_only=change != "write", purpose="proof" if change == "proof" else "implementation"
    )
    adapter = ScriptedAdapter()
    with pytest.raises(TransitionBlocked):
        asyncio.run(adapter.start_session(request))
    assert not adapter.children


def test_requested_invocation_never_relaunched(request_factory):
    make, *_ = request_factory
    request = make()
    EvidenceStore.requested(request.evidence_path)
    adapter = ScriptedAdapter()
    with pytest.raises(TransitionBlocked, match="Reconcile"):
        asyncio.run(adapter.start_session(request))
    assert not adapter.children


def test_permission_requests_declined_and_readiness_honest(request_factory):
    async def run():
        events = []
        client = _Client(events.append)
        result = await client.request_permission("session", None, [])
        assert result.outcome.outcome == "cancelled"
        assert events == [{"type": "acp.permission", "decision": "cancelled"}]

    asyncio.run(run())
    make, *_ = request_factory
    assert CodexAcpAdapter().validate_profile(make())["production_ready"] is False


def test_uncertain_startup_and_mismatched_intent_do_not_launch(request_factory):
    make, *_ = request_factory
    request = make()
    (request.evidence_path / "launch-intent.json").write_text("{}")
    adapter = ScriptedAdapter()
    with pytest.raises(TransitionBlocked, match="startup"):
        asyncio.run(adapter.start_session(request))
    other = make("two")
    with pytest.raises(TransitionBlocked, match="intent differs"):
        asyncio.run(adapter.start_session(replace(other, operation_id="different-operation")))
    assert not adapter.children


def test_recovery_after_result_loss_uses_native_evidence_without_prompt(request_factory):
    make, *_ = request_factory
    request = make()
    adapter = ScriptedAdapter()
    handle = asyncio.run(adapter.start_session(request))
    # Lose portable completion outputs after native completion, before graph checkpoint.
    (request.evidence_path / "transport-response.json").unlink()
    (request.evidence_path / "events.jsonl").unlink()
    recovered = asyncio.run(CodexAcpAdapter().reconcile(handle))
    assert recovered["proof"]["outcome"] == "native_completed"
    assert recovered["events"][-1]["text"] == '{"candidate":"fixture"}'
    assert len([call for call in adapter.calls if call[0] == "prompt"]) == 1
