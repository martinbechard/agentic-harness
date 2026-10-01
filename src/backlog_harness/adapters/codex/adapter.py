"""Codex JSON process adapter. Production capability gates are intentionally explicit."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from ...contracts import digest, resume_binding_compatible, utcnow, validate_workspace
from ...evidence import EvidenceStore, JsonlWriter, atomic_json, component
from ...runtime import AgentRequest, InvocationHandle, SessionHandle


class CodexAdapter:
    version = "1"

    def __init__(self):
        self.processes = {}

    @staticmethod
    def validate_provider_request(request, cwd):
        """Provider purpose alone never authorizes a write to the primary checkout."""
        from ...provider import Item
        from ...workflow import require, validate_transition

        require(cwd == request.snapshot.repository, "Provider workspace must be authoritative")
        if request.read_only:
            return
        operation = request.provider_operation
        require(isinstance(operation, str) and bool(operation), "Provider operation is required")
        from ...evidence import component

        path = (
            request.snapshot.operational_root / "provider-agent-operations" / component(operation)
        )
        record = json.loads((path / "requested.json").read_text())
        require(digest(record) == operation, "Provider operation identity differs")
        require(record["repository"] == str(cwd), "Provider operation repository differs")
        require(
            record.get("executing_role", record["authority"]["role"]) == request.binding.role,
            "Provider actor differs",
        )
        require(record["stage_operation"] == request.operation_id, "Provider invocation differs")
        require(record["prompt_digest"] == digest(request.prompt), "Provider prompt differs")
        validate_transition(Item(**record["item"]), record["target"], record["authority"])
        paths = record.get("paths")
        require(isinstance(paths, list) and bool(paths), "Provider mutation paths are required")
        for name in paths:
            relative = Path(name)
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and relative.parts
                and relative.parts[0] == "backlog"
                and (cwd / relative).resolve().is_relative_to(cwd / "backlog"),
                "Provider mutation path escapes backlog",
            )
        return paths

    def validate_profile(self, request):
        env = dict(os.environ)
        env["CODEX_HOME"] = request.binding.auth_context
        version = subprocess.run(
            [request.binding.executable, "--version"],
            capture_output=True,
            check=False,
            timeout=10,
            env=env,
        )
        authentication = subprocess.run(
            [request.binding.executable, "login", "status"],
            capture_output=True,
            check=False,
            timeout=10,
            env=env,
        )
        supported = (
            version.returncode == 0
            and version.stdout.decode().strip() == "codex-cli 0.159.2"
            and authentication.returncode == 0
        )
        return {
            "binding_digest": request.binding.relevant_digest,
            "production_ready": supported,
            "cli_version": version.stdout.decode().strip(),
            "authenticated": authentication.returncode == 0,
            "supported_controls": [
                "model",
                "effort",
                "injected_role_and_skills",
                "native_tools",
                "explicit_filesystem_permissions",
                "native_max_threads_request",
                "exact_resume",
                "otlp_http_json",
            ],
            "post_invocation_gates": [
                "complete_native_usage_coverage",
                "durable_export_and_flush",
                "independent_review",
            ],
            "native_delegation": "permitted",
            "mode": "workflow_evidence_gated",
        }

    async def prepare_telemetry(self, request):
        if request.telemetry.invocation_id != request.invocation_id:
            raise ValueError("Telemetry invocation mismatch")
        if not request.telemetry.path.parent.is_dir():
            raise ValueError("Telemetry destination is unavailable")
        quote = json.dumps
        exporter = (
            "otel.trace_exporter={otlp-http={endpoint="
            + quote(request.telemetry.endpoint)
            + ',protocol="json",headers={"x-harness-invocation"='
            + quote(request.telemetry.token)
            + "}}}"
        )
        return {
            "invocation_id": request.invocation_id,
            "config_digest": request.snapshot.file_digest,
            "overrides": ["otel.log_user_prompt=false", exporter],
        }

    async def start_session(self, request):
        return await self._invoke(request, None)

    async def resume_session(self, session, request):
        if not resume_binding_compatible(session.binding, request.binding):
            raise ValueError("Resume permission changes require explicit authorization evidence")
        audit = {
            "session_id": session.session_id,
            "native_session_id": session.native_session_id,
            "previous_binding": asdict(session.binding),
            "current_binding": asdict(request.binding),
            "current_config_digest": request.snapshot.file_digest,
        }
        audit_path = request.evidence_path / "resume-binding.json"
        if audit_path.exists():
            if json.loads(audit_path.read_text()) != audit:
                raise ValueError("Retained resume configuration evidence differs")
        else:
            atomic_json(audit_path, audit, exclusive=True)
        UUID(session.native_session_id)
        return await self._invoke(request, session)

    @staticmethod
    def prepare_artifact_output(request):
        """Expose only an explicitly enabled invocation-local output directory."""
        profile = request.snapshot.data["profiles"][request.binding.profile_name]
        if (
            not profile.get("artifact_output", False)
            or request.read_only
            or request.purpose not in {"implementation", "proof"}
        ):
            return None
        root = request.snapshot.operational_root.resolve()
        evidence = request.evidence_path
        intent = json.loads((evidence / "intent.json").read_text())
        expected = (
            root
            / "runs"
            / component(intent["run_id"])
            / "operations"
            / component(request.operation_id)
            / "invocations"
            / component(request.invocation_id)
        )
        if (
            evidence.absolute() != expected
            or evidence.resolve() != expected
            or intent["invocation_id"] != request.invocation_id
            or intent["operation_id"] != request.operation_id
            or intent["binding"] != asdict(request.binding)
        ):
            raise ValueError("Artifact output invocation identity differs")
        output = evidence / "artifacts"
        if output.is_symlink() or output.resolve() != output:
            raise ValueError("Artifact output must not redirect writes")
        output.mkdir(mode=0o700, exist_ok=True)
        contract = {
            "version": 1,
            "invocation_id": request.invocation_id,
            "operation_id": request.operation_id,
            "path": str(output),
            "writer": "invoked agent",
            "reviewer_access": "read",
            "permission_digest": request.binding.permission_digest,
        }
        saved = evidence / "artifact-output.json"
        if saved.exists():
            if json.loads(saved.read_text()) != contract:
                raise ValueError("Artifact output contract changed")
        else:
            atomic_json(saved, contract, exclusive=True)
        return contract

    async def _invoke(self, request: AgentRequest, session):
        prepared = await self.prepare_telemetry(request)
        profile = request.snapshot.data["profiles"][request.binding.profile_name]
        cwd = Path(
            request.snapshot.data.get("workspace", str(request.snapshot.repository))
        ).resolve()
        if request.purpose == "provider":
            provider_paths = self.validate_provider_request(request, cwd)
        elif request.purpose in {"implementation", "proof"}:
            validate_workspace(cwd, request.snapshot.repository, request.snapshot.operational_root)
        else:
            raise ValueError("Unknown invocation purpose")
        if not request.read_only and tuple(profile["permissions"]) != ("workspace-write",):
            raise ValueError("Current role profile does not authorize candidate writes")
        if (
            request.purpose == "implementation"
            and not request.read_only
            and (cwd == request.snapshot.repository or not (cwd / ".git").is_dir())
        ):
            raise ValueError("Writable invocation requires a separate candidate repository")
        if request.purpose == "proof" and (request.read_only or not profile.get("artifact_output")):
            raise ValueError("Proof invocation requires explicit artifact output permission")
        filesystem = {":root": "read"}
        if not request.read_only and request.purpose != "proof":
            if request.purpose == "provider":
                for name in provider_paths:
                    filesystem[str(cwd / name)] = "write"
            else:
                filesystem[str(cwd)] = "write"
            filesystem[str(cwd / ".git")] = "write"
            filesystem[str(cwd / ".codex")] = "read"
        artifact_output = self.prepare_artifact_output(request)
        if artifact_output:
            filesystem[artifact_output["path"]] = "write"
        permissions = (
            "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in filesystem.items()) + "}"
        )
        args = [
            request.binding.executable,
            "exec",
            "--json",
            "-m",
            profile["model"],
            "-c",
            "model_reasoning_effort=" + json.dumps(profile["effort"]),
            "-c",
            'approval_policy="never"',
            "-c",
            'default_permissions="harness"',
            "-c",
            "permissions.harness.filesystem=" + permissions,
            "-c",
            "features.multi_agent=true",
            "-c",
            "agents.max_threads="
            + str(
                request.snapshot.data["agent_clis"][request.binding.cli_name]
                .get("adapter_options", {})
                .get("native_max_threads", 2)
            ),
        ]
        if (
            not request.snapshot.data["agent_clis"][request.binding.cli_name]
            .get("adapter_options", {})
            .get("load_user_config", False)
        ):
            args.insert(3, "--ignore-user-config")
        if (
            request.snapshot.data["agent_clis"][request.binding.cli_name]
            .get("adapter_options", {})
            .get("disable_memories", True)
        ):
            args.extend(["-c", "features.memories=false"])
        for override in prepared["overrides"]:
            args.extend(["-c", override])
        if session:
            args.extend(["resume", session.native_session_id, "-"])
        else:
            args.extend(["-C", str(cwd), "-"])
        handle = InvocationHandle(request.invocation_id, request.evidence_path, session)
        writer = JsonlWriter(request.evidence_path / "events.jsonl")
        skills = []
        for name in profile["skills"]:
            skill = Path(request.snapshot.data["methodology_root"]) / "skills" / name / "SKILL.md"
            skills.append("\nConfigured skill " + name + ":\n" + skill.read_text())
        if request.purpose == "provider":
            root = Path(request.snapshot.data["methodology_root"]) / "skills"
            references = [
                str(root / name / "SKILL.md")
                for name in (
                    "manage-work-items",
                    "manage-work-items-file",
                    "resource-claim",
                    "resource-claim-helper",
                    "resource-claim-helper-mcp",
                )
                if (root / name / "SKILL.md").is_file()
            ]
            skills.append(
                "\nApplicable provider skills are available by reference. Read those required "
                "by the selected project policy; their availability does not authorize claim "
                "operations forbidden by active crisis authority:\n" + "\n".join(references)
            )
        if (
            request.snapshot.binding(request.binding.role).relevant_digest
            != request.binding.relevant_digest
        ):
            raise ValueError("Configured dependency changed before submission")
        effective_prompt = (
            "Configured role: "
            + profile["role"]
            + "\n"
            + "".join(skills)
            + "\nWorkflow request:\n"
            + request.prompt
        )
        if artifact_output:
            effective_prompt += (
                "\nArtifact output contract: "
                + json.dumps(artifact_output)
                + "\nWrite operational proof artifacts only in this directory, separate from candidate "
                "source. Harness receipts and other invocation directories remain read-only. "
                "Give reviewers exact artifact paths and require read-only inspection. Native child "
                "tool permissions are managed by the CLI; this prompt does not enforce child isolation. "
                "Artifact presence does not establish independent review or workflow completion."
            )
        from .completion_evidence import prepare_native_request

        effective_prompt = prepare_native_request(request.evidence_path, effective_prompt)
        EvidenceStore.requested(request.evidence_path)
        try:
            env = dict(os.environ)
            env["CODEX_HOME"] = request.binding.auth_context
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=8 * 1024 * 1024,
                env=env,
            )
        except OSError as exc:
            EvidenceStore.outcome(
                request.evidence_path, "submission_rejected", error=type(exc).__name__
            )
            handle.outcome = "submission_rejected"
            return handle
        self.processes[handle.invocation_id] = process
        process_start = (
            (
                await asyncio.to_thread(
                    subprocess.run,
                    ["ps", "-p", str(process.pid), "-o", "lstart="],
                    capture_output=True,
                    check=False,
                )
            )
            .stdout.decode()
            .strip()
        )
        atomic_json(
            request.evidence_path / "process.json",
            {"pid": process.pid, "started": process_start},
            exclusive=True,
        )
        stderr_bytes = 0

        async def stderr():
            nonlocal stderr_bytes
            while data := await process.stderr.read(65536):
                stderr_bytes += len(data)
            # Native diagnostics may contain exporter tokens, prompts or local secrets.
            # Persist only the byte count; failures stay explicit through outcome/events.

        async def stdout():
            ordinal = 0
            while line := await process.stdout.readline():
                raw = json.loads(line)
                kind = raw.get("type")
                normalized = {"type": kind}
                if kind == "thread.started":
                    native = raw.get("thread_id")
                    UUID(native)
                    if handle.session and handle.session.native_session_id != native:
                        raise ValueError("Native resume identity mismatch")
                    if handle.session is None:
                        handle.session = SessionHandle(str(uuid4()), native, request.binding)
                    EvidenceStore.session(
                        request.evidence_path,
                        {
                            "session_id": handle.session.session_id,
                            "native_session_id": native,
                            "binding": asdict(request.binding),
                        },
                    )
                    normalized["session_id"] = handle.session.session_id
                elif kind == "turn.completed":
                    normalized["usage"] = raw.get("usage")
                elif kind in ("error", "turn.failed"):
                    normalized["error_observed"] = True
                    normalized["message"] = str(raw.get("message", raw.get("error", "")))[:2000]
                elif kind == "item.completed":
                    normalized["item_type"] = raw.get("item", {}).get("type")
                    if normalized["item_type"] in ("agent_message", "error"):
                        text = str(
                            raw.get("item", {}).get("text", raw.get("item", {}).get("message", ""))
                        )
                        normalized["text"] = (
                            text if normalized["item_type"] == "agent_message" else text[:16000]
                        )
                event = {
                    "version": 1,
                    "event_id": digest([request.invocation_id, ordinal, raw]),
                    "invocation_id": request.invocation_id,
                    "at": utcnow(),
                    **normalized,
                }
                writer.append(event)
                handle.events.append(event)
                ordinal += 1

        async def run():
            process.stdin.write(effective_prompt.encode())
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.TaskGroup() as group:
                group.create_task(stderr())
                group.create_task(stdout())
                group.create_task(process.wait())

        failure_reason = None
        try:
            await asyncio.wait_for(run(), request.timeout_seconds)
            failed = any(e["type"] in ("error", "turn.failed") for e in handle.events)
            handle.outcome = (
                "returned"
                if process.returncode == 0
                and handle.session
                and any(e["type"] == "turn.completed" for e in handle.events)
                and not failed
                else "runtime_failed"
            )
        except (TimeoutError, ExceptionGroup, ValueError, OSError, asyncio.CancelledError) as exc:
            failure_reason = "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            handle.outcome = "unresolved"
        finally:
            self.processes.pop(handle.invocation_id, None)
        EvidenceStore.outcome(
            request.evidence_path,
            handle.outcome,
            returncode=process.returncode,
            stderr_bytes=stderr_bytes,
            failure_reason=failure_reason,
        )
        return handle

    async def observe_events(self, invocation):
        for event in invocation.events:
            yield event

    async def reconcile(self, invocation):
        return EvidenceStore.reconcile(invocation.evidence_path)

    async def request_interrupt(self, invocation):
        process = self.processes.get(invocation.invocation_id)
        if process is None or process.returncode is not None:
            return {"outcome": "unresolved", "reason": "No owned active process"}
        os.killpg(process.pid, signal.SIGTERM)
        return {"outcome": "requested", "verified_stopped": False}
