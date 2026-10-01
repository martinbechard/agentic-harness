"""Bounded ACP transport candidate; deliberately not registered as production ready.

The maintained adapter owns the native agent loop. Its current turn-mode presets
cannot enforce the legacy exact filesystem map or ignore-user-config contract.
OTEL settings are forwarded, but native exporter/flush compatibility still needs
verification: neither ACP end_turn nor usage_update proves accounting coverage.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tomllib
from contextlib import suppress
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import UUID, uuid4

from ...contracts import digest, utcnow, validate_workspace
from ...evidence import EvidenceStore, JsonlWriter, atomic_json
from ...native_evidence import native_records
from ...runtime import InvocationHandle, SessionHandle
from ...workflow import require
from .adapter import CodexAdapter
from .completion_evidence import prepare_native_request, reconcile_native_completion


def _json(value):
    return value.model_dump(mode="json", by_alias=True)


class _Client:
    """ACP callbacks persist only selected public observations, never thoughts/auth secrets."""

    def __init__(self, emit):
        self.emit = emit
        self.auth = asyncio.get_running_loop().create_future()
        self.session_id = None
        self.collecting = False

    def on_connect(self, connection):
        pass

    async def ext_notification(self, method, params):
        if method == "auth/status_update" and not self.auth.done():
            self.auth.set_result(params.get("authStatus", {}))

    async def session_update(self, session_id, update, **kwargs):
        if not self.collecting:
            return  # session/load replays history; it is not this turn's output.
        require(session_id == self.session_id, "ACP notification session differs")
        value = _json(update)
        kind = value.get("sessionUpdate")
        if kind in {"tool_call", "tool_call_update", "usage_update"}:
            self.emit(
                {
                    "type": "acp.observation",
                    "session_id": session_id,
                    **{
                        key: value[key]
                        for key in ("sessionUpdate", "toolCallId", "status", "kind", "used", "size")
                        if key in value
                    },
                }
            )

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        from acp.schema import RequestPermissionResponse

        self.emit({"type": "acp.permission", "decision": "cancelled"})
        return RequestPermissionResponse.model_validate({"outcome": {"outcome": "cancelled"}})


class CodexAcpAdapter(CodexAdapter):
    """Existing AgentCliAdapter contract, with explicit unsupported-control fences."""

    version = "acp-candidate-1"

    def validate_profile(self, request):
        try:
            sdk = version("agent-client-protocol")
        except PackageNotFoundError:
            sdk = None
        return {
            "binding_digest": request.binding.relevant_digest,
            "production_ready": False,
            "python_sdk_version": sdk,
            "required_python_sdk_version": "0.12.1",
            "required_maintained_adapter_version": "2.1.1",
            "reason": "Native permission and OTEL exporter/flush compatibility remain unverified",
            "unsupported_controls": ["exact_path_writes", "ignore_user_config"],
            "native_delegation": "CLI-managed",
        }

    async def _connect(self, process, client):
        import acp
        from acp.schema import ClientCapabilities, Implementation

        require(version("agent-client-protocol") == "0.12.1", "Unsupported ACP SDK version")
        connection = acp.connect_to_agent(client, process.stdin, process.stdout)
        await asyncio.wait_for(
            connection.initialize(
                acp.PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="agentic-harness", version=self.version),
            ),
            30,
        )
        return connection

    async def _launch(self, request, cwd, env):
        return await asyncio.create_subprocess_exec(
            request.binding.executable,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    async def _settings(self, request):
        profile = request.snapshot.data["profiles"][request.binding.profile_name]
        options = request.snapshot.data["agent_clis"][request.binding.cli_name].get(
            "adapter_options", {}
        )
        require(options.get("load_user_config") is True, "ACP cannot enforce ignore-user-config")
        require(request.binding.auth_profile == "chatgpt", "ACP requires ChatGPT subscription auth")
        require(request.purpose in {"implementation", "provider"}, "ACP proof writes unsupported")
        require(not profile.get("artifact_output"), "ACP artifact-only permissions unsupported")
        require(request.read_only, "ACP exact writable filesystem scope is not yet supported")
        cwd = Path(request.snapshot.data["workspace"]).resolve()
        if request.purpose == "provider":
            self.validate_provider_request(request, cwd)
        else:
            validate_workspace(cwd, request.snapshot.repository, request.snapshot.operational_root)
        require(
            request.snapshot.binding(request.binding.role).relevant_digest
            == request.binding.relevant_digest,
            "Configured dependency changed before submission",
        )
        settings = {
            "forced_login_method": "chatgpt",
            "model_provider": "openai",
            "model": profile["model"],
            "model_reasoning_effort": profile["effort"],
            "features.multi_agent": True,
            "agents.max_threads": options.get("native_max_threads", 2),
            "sandbox_workspace_write.network_access": False,
        }
        if options.get("disable_memories", True):
            settings["features.memories"] = False
        prepared = await self.prepare_telemetry(request)
        for override in prepared["overrides"]:
            # Existing exporter builder produces TOML; preserve the same exact target/header.
            settings.setdefault("otel", {}).update(tomllib.loads(override)["otel"])
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.endswith("API_KEY")
            and key
            not in {
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "MODEL_PROVIDER",
                "DEFAULT_AUTH_REQUEST",
                "APP_SERVER_LOGS",
                "CODEX_PATH",
                "CODEX_CONFIG",
                "INITIAL_AGENT_MODE",
            }
        }
        env.update(
            CODEX_HOME=request.binding.auth_context,
            CODEX_CONFIG=json.dumps(settings),
            INITIAL_AGENT_MODE="read-only",
        )
        skills = []
        for name in profile["skills"]:
            source = Path(request.snapshot.data["methodology_root"]) / "skills" / name / "SKILL.md"
            skills.append(f"\nConfigured skill {name}:\n{source.read_text()}")
        prompt = "Configured role: " + profile["role"] + "\n" + "".join(skills)
        prompt += "\nWorkflow request:\n" + request.prompt
        return cwd, env, prompt

    async def _close(self, process, connection):
        if connection is not None:
            with suppress(Exception):
                await asyncio.wait_for(connection.close(), 2)
        # Give the maintained adapter/native child time to flush after stdin EOF.
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 3)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                await asyncio.wait_for(process.wait(), 3)
                return
            os.killpg(process.pid, sig)
            for _ in range(30):
                await asyncio.sleep(0.1)
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    await asyncio.wait_for(process.wait(), 3)
                    return
        raise RuntimeError("ACP process group remains live")

    async def _invoke(self, request, session):
        cwd, env, prompt = await self._settings(request)
        prior = EvidenceStore.reconcile(request.evidence_path)
        require(prior["outcome"] == "not_submitted", "Reconcile requested ACP invocation first")
        require(
            prior["invocation_id"] == request.invocation_id
            and prior["operation_id"] == request.operation_id
            and prior["binding"] == asdict(request.binding)
            and prior["config_digest"] == request.snapshot.file_digest,
            "ACP invocation intent differs from the current request",
        )
        # Never restart a partially prepared session: preserve its original creation identity.
        require(
            not (request.evidence_path / "session.json").exists(),
            "Retained ACP session requires reconciliation",
        )
        require(
            not (request.evidence_path / "launch-intent.json").exists(),
            "Prior ACP startup may have reached the native process; reconcile before retry",
        )
        wire = prepare_native_request(request.evidence_path, prompt)
        handle = InvocationHandle(request.invocation_id, request.evidence_path, session)
        writer = JsonlWriter(request.evidence_path / "events.jsonl")

        def emit(value):
            event = {
                "version": 1,
                "event_id": digest([request.invocation_id, len(handle.events), value]),
                "invocation_id": request.invocation_id,
                "at": utcnow(),
                **value,
            }
            writer.append(event)
            handle.events.append(event)

        client = _Client(emit)
        process = connection = None
        cancelled = False
        try:
            atomic_json(
                request.evidence_path / "launch-intent.json",
                {
                    "invocation_id": request.invocation_id,
                    "binding_digest": request.binding.relevant_digest,
                },
                exclusive=True,
            )
            process = await self._launch(request, cwd, env)
            self.processes[request.invocation_id] = process
            started = await asyncio.to_thread(
                subprocess.run,
                ["ps", "-p", str(process.pid), "-o", "lstart="],
                capture_output=True,
                check=True,
                timeout=5,
            )
            atomic_json(
                request.evidence_path / "process.json",
                {
                    "pid": process.pid,
                    "started": started.stdout.decode().strip(),
                },
                exclusive=True,
            )
            connection = await asyncio.wait_for(self._connect(process, client), 35)
            auth = await asyncio.wait_for(client.auth, 30)
            require(
                auth.get("kind") == "account" and str(auth.get("label", "")).startswith("ChatGPT"),
                "Native account is not ChatGPT; no authentication fallback",
            )
            atomic_json(
                request.evidence_path / "auth-observation.json",
                {"kind": "account", "subscription": "ChatGPT"},
                exclusive=True,
            )
            args = {"cwd": str(cwd), "mcp_servers": []}
            if session:
                response = await asyncio.wait_for(
                    connection.load_session(session_id=session.native_session_id, **args), 30
                )
                native = session.native_session_id
            else:
                response = await asyncio.wait_for(connection.new_session(**args), 30)
                native = response.session_id
                require(str(UUID(native)) == native, "Invalid native session identity")
                handle.session = SessionHandle(str(uuid4()), native, request.binding)
            atomic_json(
                request.evidence_path / "session-exchange.json",
                {
                    "invocation_id": request.invocation_id,
                    "operation_id": request.operation_id,
                    "method": "session/load" if session else "session/new",
                    "request": {
                        "cwd": str(cwd),
                        "mcpServers": [],
                        **({"sessionId": native} if session else {}),
                    },
                    "response": _json(response),
                    "binding_digest": request.binding.relevant_digest,
                },
                exclusive=True,
            )
            EvidenceStore.session(request.evidence_path, asdict(handle.session))
            await asyncio.wait_for(connection.set_session_mode(native, "read-only"), 30)
            client.session_id, client.collecting = native, True
            from acp.schema import TextContentBlock

            EvidenceStore.requested(request.evidence_path)
            response = await asyncio.wait_for(
                connection.prompt(native, [TextContentBlock(type="text", text=wire)]),
                request.timeout_seconds,
            )
            atomic_json(
                request.evidence_path / "transport-response.json", _json(response), exclusive=True
            )
            require(response.stop_reason == "end_turn", "ACP prompt did not finish")
        except asyncio.CancelledError:
            cancelled = True
            handle.outcome = "unresolved"
        except Exception as exc:  # noqa: BLE001 - SDK errors must retain uncertainty, never log secrets.
            handle.outcome = "unresolved"
            emit({"type": "acp.failure", "error_type": type(exc).__name__})
        finally:
            if process:
                try:
                    await self._close(process, connection)
                except Exception as exc:  # noqa: BLE001 - SDK errors must retain uncertainty, never log secrets.
                    handle.outcome = "unresolved"
                    emit({"type": "acp.cleanup_failed", "error_type": type(exc).__name__})
                self.processes.pop(request.invocation_id, None)
        if handle.outcome != "unresolved":
            try:
                recovered = self._completed(request.evidence_path)
                atomic_json(
                    request.evidence_path / "native-completion.json",
                    recovered["proof"],
                    exclusive=True,
                )
                for event in recovered["events"]:
                    emit(event)
                handle.outcome = "returned"
            except Exception as exc:  # noqa: BLE001 - SDK errors must retain uncertainty, never log secrets.
                handle.outcome = "unresolved"
                emit({"type": "acp.reconciliation_failed", "error_type": type(exc).__name__})
        EvidenceStore.outcome(request.evidence_path, handle.outcome)
        if cancelled:
            raise asyncio.CancelledError()
        return handle

    @staticmethod
    def _completed(path):
        proof = reconcile_native_completion(path)
        intent = EvidenceStore.reconcile(path)
        rows, fingerprint = native_records(
            proof["native_session_id"], Path(intent["binding"]["auth_context"]) / "sessions"
        )
        require(
            fingerprint == proof["native_evidence_sha256"],
            "Native evidence changed during usage read",
        )
        active = False
        usage = None
        for row in rows:
            value = row["payload"]
            if row.get("type") == "event_msg" and value.get("type") == "task_started":
                active = value.get("turn_id") == proof["native_turn_id"]
            if active and row.get("type") == "event_msg" and value.get("type") == "token_count":
                info = value.get("info") or {}
                counter = info.get("total_token_usage", {}).get("output_tokens")
                usage = (
                    {"output_tokens": counter} if type(counter) is int and counter >= 0 else None
                )
            if row.get("type") == "event_msg" and value.get("type") == "task_complete":
                active = False
        # Counter remains cumulative; existing analytics reconcile it with prior-session
        # counters, native children and attributed OTEL deltas. Missing means unknown.
        return {
            "proof": proof,
            "events": [
                {
                    "type": "turn.completed",
                    "usage": usage,
                    "native_turn_id": proof["native_turn_id"],
                },
                {"type": "item.completed", "item_type": "agent_message", "text": proof["text"]},
            ],
        }

    async def reconcile(self, invocation):
        # Read-only: caller saves a recovered result only after its normal telemetry gates.
        return self._completed(invocation.evidence_path)
