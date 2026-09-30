"""Foreground execution of one selected workflow, with durable generation boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .adapters.registry import AdapterRegistry
from .contracts import AgentBinding, digest, freeze, load_config, load_control_config, plain, utcnow
from .evidence import (
    EvidenceStore,
    async_operation_lock,
    atomic_json,
    component,
    operation_lock,
    read_jsonl,
)
from .provider import FileProvider, TransitionBlocked, git
from .runtime import AgentRequest, SessionHandle
from .telemetry import TelemetryReceiver
from .workflow import require, validate_candidate, validate_transition


class Application:
    def __init__(self, config_path, *, control_only=False):
        self.config_path = Path(config_path).resolve()
        self.config = (load_control_config if control_only else load_config)(self.config_path)
        self.provider = FileProvider(self.config.repository, self.config.operational_root)
        self.root = self.config.operational_root
        self.projection_lock = threading.Lock()
        self.trace_cache = {}

    def _stage_path(self, item_id, stage):
        return self.root / "workflow-evidence" / component(item_id) / (stage + ".json")

    def item_workflow(self, item_id):
        assignment = self._stage_path(item_id, "assignment")
        if assignment.exists():
            frozen = json.loads(assignment.read_text())
            if "workflow" in frozen:
                return frozen["workflow"]
        selected = plain(self.config.data["workflow"])
        selected.update(selected.get("items", {}).get(item_id, {}))
        selected.pop("items", None)
        return selected

    def candidate_repository(self, item_id):
        if "candidate_root" not in self.config.data:
            return Path(self.config.data["workspace"]).resolve()
        root = Path(self.config.data["candidate_root"]).resolve()
        destination = root / component(item_id)
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix=".clone-", dir=root) as temporary:
                prepared = Path(temporary) / "candidate"
                subprocess.run(
                    ["git", "clone", "--no-hardlinks", str(self.config.repository), str(prepared)],
                    capture_output=True,
                    check=True,
                )
                os.rename(prepared, destination)
        require((destination / ".git").is_dir(), "Candidate clone is incomplete")
        return destination

    async def invoke(self, item_id, stage, role, prompt, *, session=None, read_only=True):
        with operation_lock(self.root / "locks" / (component(item_id + ":" + stage) + ".lock")):
            async with self.capacity_slot() as reservation:
                return await self._invoke(
                    item_id,
                    stage,
                    role,
                    prompt,
                    session=session,
                    read_only=read_only,
                    reservation=reservation,
                )

    @staticmethod
    def process_stopped(path):
        state = EvidenceStore.reconcile(path)["outcome"]
        if state in {"not_submitted", "returned", "runtime_failed", "submission_rejected"}:
            return True
        if not (path / "process.json").exists():
            return False
        process = json.loads((path / "process.json").read_text())
        observed = (
            subprocess.run(
                ["ps", "-p", str(process["pid"]), "-o", "lstart="], capture_output=True, check=False
            )
            .stdout.decode()
            .strip()
        )
        return bool(process["started"]) and observed != process["started"]

    @asynccontextmanager
    async def capacity_slot(self):
        """Count every locked or uncertain slot, including indexes above a reduced cap."""
        while True:
            chosen = None
            busy = uncertain = 0
            async with async_operation_lock(self.root / "capacity/allocation.lock"):
                limit = load_config(self.config_path).data["max_active_invocations"]
                indexes = set(range(limit))
                indexes.update(
                    int(p.name)
                    for p in (self.root / "capacity").glob("*")
                    if p.is_dir() and p.name.isdigit()
                )
                for index in sorted(indexes):
                    root = self.root / "capacity" / str(index)
                    lock = operation_lock(root / "owner.lock")
                    try:
                        lock.__enter__()
                    except RuntimeError:
                        busy += 1
                        continue
                    reservation = root / "reservation.json"
                    occupied = False
                    try:
                        if reservation.exists():
                            prior = Path(json.loads(reservation.read_text())["evidence_path"])
                            require(
                                prior.resolve().is_relative_to(self.root.resolve()),
                                "Capacity reservation escaped evidence root",
                            )
                            occupied = not self.process_stopped(prior)
                        if occupied:
                            uncertain += 1
                        elif chosen is None and index < limit:
                            chosen = (lock, reservation)
                            lock = None
                    finally:
                        if lock is not None:
                            lock.__exit__(None, None, None)
                if busy + uncertain >= limit and chosen:
                    chosen[0].__exit__(None, None, None)
                    chosen = None
            if chosen:
                try:
                    yield chosen[1]
                finally:
                    chosen[0].__exit__(None, None, None)
                return
            require(busy > 0, "Uncertain executions occupy every configured invocation slot")
            await asyncio.sleep(0.1)

    async def _invoke(
        self, item_id, stage, role, prompt, *, session=None, read_only=True, reservation=None
    ):
        # Re-read immediately before every harness invocation. The resolved storage identity
        # cannot move mid-run; such edits require explicit reconciliation.
        snapshot = load_config(self.config_path)
        require(
            snapshot.repository == self.config.repository
            and snapshot.operational_root == self.root,
            "Repository or evidence root changed during the run",
        )
        assignment = self._stage_path(item_id, "assignment")
        if assignment.exists():
            frozen = json.loads(assignment.read_text())
            selected = plain(snapshot.data["workflow"])
            selected.update(selected.get("items", {}).get(item_id, {}))
            selected.pop("items", None)
            require(
                "workflow" not in frozen or selected == frozen["workflow"],
                "Accepted workflow changed; reconcile before another invocation",
            )
            require(
                "candidate_root" not in frozen
                or frozen["candidate_root"] == snapshot.data.get("candidate_root"),
                "Accepted candidate storage changed; reconcile before another invocation",
            )
            require(
                "workspace" not in frozen or frozen["workspace"] == snapshot.data["workspace"],
                "Accepted workspace changed; reconcile before another invocation",
            )
        if "candidate_root" in snapshot.data:
            data = plain(snapshot.data)
            data["workspace"] = str(self.candidate_repository(item_id))
            snapshot = replace(snapshot, data=freeze(data))
        binding = snapshot.binding(role)
        saved = self._stage_path(item_id, stage)
        request_hash = digest(prompt)
        if saved.exists():
            result = json.loads(saved.read_text())
            require(
                result["request_digest"] == request_hash,
                "Stage request changed; reconcile prior evidence",
            )
            require(result["outcome"] == "returned", "Previous invocation is unresolved or failed")
            self.validate_invocation_result(result)
            return result
        run_id = "item:" + item_id
        store = EvidenceStore(self.root, run_id)
        operation = item_id + ":" + stage
        operation_path = store.run / "operations" / component(operation)
        if operation_path.exists():
            candidates = list(operation_path.glob("invocations/*/intent.json"))
            require(
                len(candidates) == 1, "Invocation intent is absent or ambiguous; reconcile storage"
            )
            path = candidates[0].parent
            prior = EvidenceStore.reconcile(path)
            require(prior["request_digest"] == request_hash, "Recovered request differs")
            invocation_id = prior["invocation_id"]
            if prior["outcome"] != "not_submitted":
                recovered = self.recover_invocation(path)
                require(
                    recovered is not None,
                    "Requested invocation is unresolved; replacement is prohibited",
                )
                atomic_json(saved, recovered, exclusive=True)
                self.validate_invocation_result(recovered)
                return recovered
            require(
                prior["binding"] == asdict(binding),
                "Unsubmitted intent binding changed; explicit reconciliation required",
            )
        else:
            invocation_id = str(uuid4())
            path = store.begin(
                operation,
                invocation_id,
                snapshot,
                binding,
                action=stage,
                item_id=None if role == "coordinator" else item_id,
                request_digest=request_hash,
            )
        with TelemetryReceiver(self.root) as receiver:
            if reservation:
                atomic_json(
                    reservation, {"evidence_path": str(path), "invocation_id": invocation_id}
                )
            dest = receiver.register(
                run_id=run_id,
                invocation_id=invocation_id,
                role=role,
                adapter=binding.adapter,
                item_id=None if role == "coordinator" else item_id,
                provider_id="file:" + str(snapshot.repository),
            )
            atomic_json(
                path / "telemetry.json",
                {"path": str(dest.path)},
                exclusive=not (path / "telemetry.json").exists(),
            )
            request = AgentRequest(
                operation,
                invocation_id,
                snapshot,
                binding,
                prompt,
                path,
                dest,
                timeout_seconds=180,
                read_only=read_only,
            )
            adapter = AdapterRegistry().resolve(binding.adapter)
            capability = await asyncio.to_thread(adapter.validate_profile, request)
            require(
                capability.get("production_ready") is True,
                "CLI binding has not passed launch capability validation",
            )
            atomic_json(path / "capability.json", capability)
            handle = await (
                adapter.resume_session(session, request)
                if session
                else adapter.start_session(request)
            )
            await asyncio.sleep(2)
        report = receiver.report(dest)
        atomic_json(path / "telemetry-report.json", report, exclusive=True)
        messages = [e["text"] for e in handle.events if e.get("item_type") == "agent_message"]
        result = {
            "version": 1,
            "request_digest": request_hash,
            "invocation_id": invocation_id,
            "outcome": handle.outcome,
            "role": role,
            "binding": asdict(binding),
            "session": asdict(handle.session) if handle.session else None,
            "text": messages[-1] if messages else None,
            "events": handle.events,
            "telemetry": report,
            "telemetry_path": str(dest.path),
            "evidence_path": str(path),
        }
        atomic_json(saved, result, exclusive=True)
        self.validate_invocation_result(result)
        return result

    def validate_invocation_result(self, result):
        require(
            result["outcome"] == "returned",
            "Agent invocation did not return successfully; evidence preserved",
        )
        report = result["telemetry"]
        require(
            report["span_count"] > 0 and report["rejected_exports"] == 0,
            "Telemetry is missing or rejected; next generation is fenced",
        )
        from .telemetry import Sink

        path = Path(result["telemetry_path"])
        require(
            path.resolve().is_relative_to(self.root.resolve()),
            "Telemetry path escaped evidence root",
        )
        sink = Sink(path, {})
        require(
            len(sink.seen) == report["span_count"], "Telemetry no longer matches its saved receipt"
        )
        require(
            report.get("evidence_sha256") == sink.evidence_digest(),
            "Telemetry content is not bound to its saved receipt",
        )

    @staticmethod
    def validate_call_limits(result, limits):
        turns = [e for e in result["events"] if e["type"] == "turn.completed"]
        require(0 < len(turns) <= limits["turns"], "Observed CLI turn count exceeds call budget")
        require(
            all(type(e.get("usage", {}).get("output_tokens")) is int for e in turns),
            "Call budget usage is unknown",
        )
        require(
            turns[-1]["usage"]["output_tokens"] <= limits["generated_tokens"],
            "Generated tokens exceed call budget",
        )

    def recover_invocation(self, path):
        """Reconstruct a returned stage from durable observations, never launch a CLI."""
        prior = EvidenceStore.reconcile(path)
        events, _, partial = read_jsonl(path / "events.jsonl")
        require(not partial, "Partial event evidence requires storage repair")
        if prior["outcome"] != "returned":
            process_path = path / "process.json"
            if prior["outcome"] != "unresolved" or not process_path.exists():
                return None
            process = json.loads(process_path.read_text())
            observed = (
                subprocess.run(
                    ["ps", "-p", str(process["pid"]), "-o", "lstart="],
                    capture_output=True,
                    check=False,
                )
                .stdout.decode()
                .strip()
            )
            if observed and observed == process["started"]:
                return None
            if (
                not events
                or events[-1]["type"] != "turn.completed"
                or any(e["type"] in {"error", "turn.failed"} for e in events)
            ):
                return None
            from .native_evidence import native_records

            session = json.loads((path / "session.json").read_text())
            records, _ = native_records(
                session["native_session_id"], self.native_sessions_root(prior["binding"])
            )
            completed = [
                r
                for r in records
                if r.get("payload", {}).get("type") == "task_complete"
                and r.get("timestamp", "") >= events[-1]["at"]
            ]
            if not completed or completed[-1]["payload"].get("error"):
                return None
            EvidenceStore.outcome(
                path, "returned", reconciliation="native_terminal_and_stopped_process"
            )
        if not (path / "telemetry-report.json").exists():
            return None
        report = json.loads((path / "telemetry-report.json").read_text())
        require(
            report["span_count"] > 0 and report["rejected_exports"] == 0,
            "Recovered telemetry was missing or rejected",
        )
        session_value = json.loads((path / "session.json").read_text())
        session_value.pop("version", None)
        telemetry_path = Path(json.loads((path / "telemetry.json").read_text())["path"])
        require(
            telemetry_path.resolve().is_relative_to(self.root.resolve()),
            "Telemetry evidence escaped root",
        )
        from .telemetry import Sink

        sink = Sink(telemetry_path, {})
        require(sink.seen, "Recovered telemetry is missing")
        messages = [e["text"] for e in events if e.get("item_type") == "agent_message"]
        return {
            "version": 1,
            "request_digest": prior["request_digest"],
            "invocation_id": prior["invocation_id"],
            "outcome": "returned",
            "role": prior["binding"]["role"],
            "binding": prior["binding"],
            "session": session_value,
            "text": messages[-1] if messages else None,
            "events": events,
            "telemetry": {**report, "coverage": "reconciled"},
            "telemetry_path": str(telemetry_path),
            "evidence_path": str(path),
        }

    def native_sessions_root(self, binding):
        if binding.get("auth_context"):
            return Path(binding["auth_context"]) / "sessions"
        cli = self.config.data.get("agent_clis", {}).get(binding["cli_name"])
        require(cli is not None, "The originating native authentication context is unavailable")
        import os

        return (
            Path(
                cli.get("adapter_options", {}).get(
                    "codex_home", os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
                )
            )
            / "sessions"
        )

    @staticmethod
    def result_json(result):
        try:
            text = result["text"].strip()
            if text.startswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            return json.loads(text)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TransitionBlocked(
                "Expected a structured workflow request from the agent"
            ) from exc

    @staticmethod
    def session(result):
        value = result["session"]
        return SessionHandle(
            value["session_id"], value["native_session_id"], AgentBinding(**value["binding"])
        )

    @staticmethod
    def authority(result, item_id, **facts):
        return {
            "role": result["role"],
            "item_id": item_id,
            "invocation_id": result["invocation_id"],
            "observed_result": result["outcome"] == "returned",
            "session_id": result["session"]["session_id"],
            "native_session_id": result["session"]["native_session_id"],
            **facts,
        }

    def checks(self, repository, item_id, candidate, stage):
        receipts = []
        for argv in self.item_workflow(item_id)["checks"]:
            proc = subprocess.run(
                list(argv), cwd=repository, capture_output=True, check=False, timeout=60
            )
            output = proc.stdout + proc.stderr
            receipt = {
                "argv": list(argv),
                "returncode": proc.returncode,
                "candidate": candidate,
                "evidence_sha256": sha256(output).hexdigest(),
                "at": utcnow(),
                "output": output.decode(errors="replace"),
            }
            receipts.append(receipt)
        atomic_json(self._stage_path(item_id, stage), receipts)
        require(all(c["returncode"] == 0 for c in receipts), "Required checks failed")
        return receipts

    def usage_view(self, item_id, *, observed_item=None):
        from .analytics import invocation_usage
        from .native_evidence import child_usage

        item = observed_item or self.provider.item(item_id)
        results = []
        for path in self._stage_path(item_id, "unused").parent.glob("*.json"):
            value = json.loads(path.read_text())
            if (
                isinstance(value, dict)
                and value.get("role") == "orchestrator"
                and "events" in value
            ):
                results.append(value)
        results.sort(key=lambda v: v["events"][0]["at"] if v["events"] else "")
        total, previous = 0, {}
        for result in results:
            if result["telemetry"].get("evidence_sha256"):
                try:
                    self.validate_invocation_result(result)
                except (ValueError, OSError, RuntimeError):
                    return {"status": "unknown", "generated_tokens": None, "may_generate": False}
            elif item.state != "Completed":
                return {"status": "unknown", "generated_tokens": None, "may_generate": False}
            if result["outcome"] != "returned" or not result["events"]:
                return {"status": "unknown", "generated_tokens": None, "may_generate": False}
            session = result["session"]["native_session_id"]
            try:
                children, _ = child_usage(
                    session,
                    result["events"][0]["at"],
                    result["events"][-1]["at"],
                    self.native_sessions_root(result["binding"]),
                )
            except TransitionBlocked:
                return {"status": "unknown", "generated_tokens": None, "may_generate": False}
            if children is None:
                return {"status": "unknown", "generated_tokens": None, "may_generate": False}
            measured = invocation_usage(result, previous.get(session, 0), child_outputs=children)
            if measured is None:
                return {"status": "unknown", "generated_tokens": None, "may_generate": False}
            previous[session] = [
                e["usage"]["output_tokens"] for e in result["events"] if e.get("usage")
            ][-1]
            total += measured
        frozen_path = self._stage_path(item_id, "assignment")
        original = (
            json.loads(frozen_path.read_text()).get("original_high", item.original_high)
            if frozen_path.exists()
            else item.original_high
        )
        known = {r["invocation_id"] for r in results}
        for path in (self.root / "runs" / component("item:" + item_id)).glob(
            "operations/*/invocations/*/intent.json"
        ):
            pending = EvidenceStore.reconcile(path.parent)
            if (
                pending["binding"]["role"] == "orchestrator"
                and pending["outcome"] != "not_submitted"
                and pending["invocation_id"] not in known
            ):
                return {"status": "unknown", "generated_tokens": None, "may_generate": False}
        if original is None:
            return {"status": "unknown", "generated_tokens": None, "may_generate": False}
        if self.config.data.get("generation_configuration_error"):
            return {
                "status": "configuration_invalid",
                "generated_tokens": total,
                "original_high": original,
                "ceiling": None,
                "may_generate": False,
            }
        ceiling = original * self.config.data.get("generation_guard_multiplier", 2.0)
        allowance = self._stage_path(item_id, "allowance")
        if allowance.exists():
            approved = json.loads(allowance.read_text())
            require(approved["original_high"] == original, "Allowance baseline changed")
            ceiling = max(ceiling, approved["ceiling"])
        return {
            "status": "below" if total < ceiling else "crossed",
            "generated_tokens": total,
            "original_high": original,
            "ceiling": ceiling,
            "may_generate": total < ceiling,
            "overshoot": max(0, total - ceiling),
        }

    def guard(self, item_id):
        view = self.usage_view(item_id)
        atomic_json(self._stage_path(item_id, "usage"), view)
        if not view["may_generate"]:
            incident = "usage_unknown" if view["status"] == "unknown" else "usage_limit"
            item = self.provider.item(item_id)
            incident_path = self._stage_path(
                item_id, "incident-" + component(incident + ":" + str(view.get("ceiling")))
            )
            if not incident_path.exists():
                atomic_json(
                    incident_path,
                    {
                        "incident": incident,
                        "usage": view,
                        "item_id": item_id,
                        "owner": item.owner,
                        "resume_state": item.state,
                        "at": utcnow(),
                    },
                    exclusive=True,
                )
            admit_path = self._stage_path(item_id, "admit")
            if item.state in {"Starting", "Running"} and admit_path.exists():
                admit = json.loads(admit_path.read_text())
                self.provider.transition(
                    item_id,
                    item.revision,
                    "Holding",
                    self.authority(
                        admit,
                        item_id,
                        incident=incident,
                        policy="configured original-estimate generation guard",
                        usage=view,
                    ),
                    validate=validate_transition,
                )
            raise TransitionBlocked("Item held pending usage review: " + incident)
        return view

    async def review_hold(self, item_id, *, requested_ceiling=None, reference=None):
        """One bounded Coordinator review of a particular durable guard incident."""
        item = self.provider.item(item_id)
        require(item.state == "Holding", "Item is not Holding")
        require(self.item_quiescent(item_id), "Item execution is not proven quiescent")
        incidents = sorted(
            self._stage_path(item_id, "unused").parent.glob("incident-*.json"),
            key=lambda p: json.loads(p.read_text())["at"],
        )
        require(incidents, "Guard incident is missing")
        incident = json.loads(incidents[-1].read_text())
        usage = self.usage_view(item_id)
        require(
            usage["status"] != "unknown", "Unknown usage must be restored before allowance review"
        )
        proposed = usage["ceiling"] if requested_ceiling is None else requested_ceiling
        require(
            type(proposed) in (int, float)
            and proposed > usage["generated_tokens"]
            and proposed >= usage["ceiling"],
            "Requested ceiling must exceed measured usage and preserve the baseline",
        )
        require(
            proposed == usage["ceiling"] or reference,
            "A ceiling increase needs an operator approval reference",
        )
        stage = "review-hold-" + component(incidents[-1].name)
        request_path = self._stage_path(item_id, stage + "-request")
        if request_path.exists():
            request = json.loads(request_path.read_text())
            require(
                request["requested_ceiling"] == proposed
                and request["operator_reference"] == reference,
                "The retained hold review request differs; reconcile it before changing approval",
            )
        else:
            request = {
                "item_id": item_id,
                "owner": item.owner,
                "revision": item.revision,
                "incident": incident,
                "usage": usage,
                "requested_ceiling": proposed,
                "operator_reference": reference,
            }
            atomic_json(request_path, request, exclusive=True)
        decision = await self.invoke(
            item_id,
            stage,
            "coordinator",
            "You own the bounded administrative guard review. Do not implement, delegate or mutate files. Review the retained owner, original baseline, usage and operator ceiling request. Return only JSON with item_id, operation resume or assess, approved_ceiling, retained_owner, reason. Never approve unknown usage.\n"
            + json.dumps(
                {
                    key: request[key]
                    for key in (
                        "item_id",
                        "owner",
                        "incident",
                        "usage",
                        "requested_ceiling",
                        "operator_reference",
                    )
                },
                sort_keys=True,
            ),
        )
        self.validate_call_limits(decision, self.config.data["administrative_review_limits"])
        counts = [e["usage"]["output_tokens"] for e in decision["events"] if e.get("usage")]
        require(
            counts
            and counts[-1] <= self.config.data["administrative_review_limits"]["generated_tokens"],
            "Administrative review exceeded its output budget",
        )
        value = self.result_json(decision)
        require(
            value.get("operation") == "resume"
            and value.get("item_id") == item_id
            and value.get("approved_ceiling") == proposed
            and value.get("retained_owner") == item.owner,
            "Coordinator did not approve exact hold release",
        )
        approved = {
            "ceiling": proposed,
            "original_high": usage["original_high"],
            "decision": decision["invocation_id"],
            "reference": reference,
        }
        atomic_json(self._stage_path(item_id, "allowance"), approved)
        usage = self.usage_view(item_id)
        current = self.provider.item(item_id)
        if "release_authority" not in request:
            request["release_authority"] = self.authority(
                decision,
                item_id,
                operation="resume",
                retained_owner=current.owner,
                usage=usage,
                approval=approved,
            )
            atomic_json(request_path, request)
        return self.provider.transition(
            item_id,
            request["revision"],
            incident["resume_state"],
            request["release_authority"],
            validate=validate_transition,
        )

    def item_quiescent(self, item_id):
        return all(
            self.process_stopped(p.parent)
            for p in (self.root / "runs" / component("item:" + item_id)).glob(
                "operations/*/invocations/*/intent.json"
            )
        )

    async def answer(self, item_id, question_id, expected_revision, text):
        with operation_lock(self.root / "item-locks" / (component(item_id) + ".lock")):
            item = self.provider.item(item_id)
            operation_path = self._stage_path(
                item_id, "answer-operation-" + digest([question_id, expected_revision, text])
            )
            if operation_path.exists():
                operation = json.loads(operation_path.read_text())
            else:
                require(item.revision == expected_revision, "Stale question revision")
                question = self.provider.question(item)
                require(
                    question and question["question_id"] == question_id, "Question identity differs"
                )
                require(isinstance(text, str) and text.strip(), "Answer must be nonempty")
                operation = {
                    "question": question,
                    "answer": {
                        "text": text,
                        "digest": digest(text),
                        "question_revision": expected_revision,
                    },
                    "before_revision": expected_revision,
                }
                atomic_json(operation_path, operation, exclusive=True)
            question, answer = operation["question"], operation["answer"]
            require(self.item_quiescent(item_id), "Canonical execution is not quiescent")
            persisted = self.provider.transition(
                item_id,
                operation["before_revision"],
                "User Action Required",
                {
                    "role": "operator",
                    "item_id": item_id,
                    "invocation_id": "operator:" + digest([question_id, text, expected_revision]),
                    "observed_result": True,
                    "question": question,
                    "answer": answer,
                },
                validate=validate_transition,
            )
            acceptance = json.loads(self._stage_path(item_id, "accept").read_text())
            self.guard(item_id)
            stage = "answer-" + digest([question_id, answer["digest"]])
            disposition = await self.invoke(
                item_id,
                stage,
                "orchestrator",
                "Classify the exact operator answer to your question. This invocation is read-only. Do not implement, delegate, or mutate. Only approve authorizes continuation; defer, decline and ambiguous retain the wait. Return only JSON with question_id, answer_digest, disposition approve/defer/decline/ambiguous and reason.\n"
                + json.dumps({"question": question, "answer": answer}, sort_keys=True),
                session=self.session(acceptance),
            )
            value = self.result_json(disposition)
            require(
                value.get("question_id") == question_id
                and value.get("answer_digest") == answer["digest"]
                and value.get("disposition") in {"approve", "defer", "decline", "ambiguous"},
                "Answer disposition is missing or unbound",
            )
            target = "Running" if value["disposition"] == "approve" else "User Action Required"
            authority = self.authority(
                disposition,
                item_id,
                question=question,
                answer=answer,
                disposition=value["disposition"],
            )
            result = self.provider.transition(
                item_id, persisted["revision"], target, authority, validate=validate_transition
            )
            if target == "Running":
                atomic_json(
                    self._stage_path(item_id, "continuation"),
                    {
                        "stage": "produce-review-" + digest([question_id, answer["digest"]]),
                        "approval": authority,
                    },
                )
            operation["result"] = {**result, "disposition": value["disposition"]}
            atomic_json(operation_path, operation)
            return operation["result"]

    async def resume_answer(self, item_id):
        operations = list(
            self._stage_path(item_id, "unused").parent.glob("answer-operation-*.json")
        )
        pending = [
            json.loads(p.read_text())
            for p in operations
            if "result" not in json.loads(p.read_text())
        ]
        require(len(pending) == 1, "No unique pending persisted answer")
        operation = pending[0]
        return await self.answer(
            item_id,
            operation["question"]["question_id"],
            operation["before_revision"],
            operation["answer"]["text"],
        )

    def recover_provider(self, operation_id):
        matches = [
            p.parent
            for p in (self.root / "provider-operations").glob("*/requested.json")
            if json.loads(p.read_text()).get("operation") == operation_id
            or p.parent.name == operation_id
        ]
        require(len(matches) == 1, "No unique prepared provider operation")
        return self.provider.recover_prepared(matches[0], validate=validate_transition)

    def reconcile(self):
        results = []
        for intent_path in sorted(
            (self.root / "runs").glob("*/operations/*/invocations/*/intent.json")
        ):
            path = intent_path.parent
            prior = EvidenceStore.reconcile(path)
            stage = prior["action"]
            item_id = prior["operation_id"].rsplit(":" + stage, 1)[0]
            saved = self._stage_path(item_id, stage)
            if not saved.exists() and prior["outcome"] != "not_submitted":
                try:
                    recovered = self.recover_invocation(path)
                    if recovered:
                        with operation_lock(
                            self.root / "locks" / (component(item_id + ":" + stage) + ".lock")
                        ):
                            atomic_json(saved, recovered, exclusive=True)
                        prior = EvidenceStore.reconcile(path)
                except (ValueError, OSError, RuntimeError) as exc:
                    prior = {**prior, "recovery_error": str(exc)}
            results.append(
                {
                    "invocation_id": prior["invocation_id"],
                    "item_id": item_id,
                    "outcome": prior["outcome"],
                    "quiescent": self.process_stopped(path),
                    "error": prior.get("recovery_error"),
                }
            )
        for record in sorted((self.root / "provider-operations").glob("*/requested.json")):
            try:
                with self.provider.transaction():
                    receipt = self.provider.reconcile_operation(record.parent)
                results.append(
                    {
                        "provider_operation": record.parent.name,
                        "outcome": "returned",
                        "receipt": receipt,
                    }
                )
            except (ValueError, OSError) as exc:
                results.append(
                    {
                        "provider_operation": record.parent.name,
                        "outcome": "unresolved",
                        "error": str(exc),
                    }
                )
        return results

    async def run_item(self, item_id):
        async with async_operation_lock(self.root / "item-locks" / (component(item_id) + ".lock")):
            mode = self.config.data["workflow"]["mode"]
            with (
                operation_lock(self.root / "solo-execution.lock")
                if mode == "SOLO"
                else nullcontext()
            ):
                self.provider.policy()
                import yaml

                project = yaml.safe_load((self.config.repository / "PROJECT.yaml").read_text())
                require(
                    project["execution_mode"] == mode,
                    "Configuration execution mode differs from provider policy",
                )
                if mode == "MULTITASK":
                    mine = set(self.item_workflow(item_id)["allowed_paths"])
                    for other in self.provider.snapshot():
                        if other.item_id != item_id and other.state in {"Starting", "Running"}:
                            theirs = set(self.item_workflow(other.item_id)["allowed_paths"])
                            require(not mine & theirs, "Concurrent assignment scopes overlap")
                return await self._run_item(item_id)

    async def _run_item(self, item_id):
        self.reconcile()
        self.provider.policy()
        item = self.provider.item(item_id)
        if item.state == "Completed":
            return {"item_id": item_id, "state": item.state, "revision": item.revision}
        require(
            not any(
                "result" not in json.loads(p.read_text())
                for p in self._stage_path(item_id, "unused").parent.glob("answer-operation-*.json")
            ),
            "A persisted answer is incomplete; use resume-answer before continuing",
        )
        assignment_path = self._stage_path(item_id, "assignment")
        if not assignment_path.exists():
            require(
                item.state == "Ready",
                "Existing reservation has no retained assignment; restore original evidence before continuation",
            )
            atomic_json(
                assignment_path,
                {
                    "content": item.content,
                    "original_high": item.original_high,
                    "workflow": self.item_workflow(item_id),
                    "candidate_root": self.config.data.get("candidate_root"),
                    "workspace": self.config.data["workspace"],
                },
                exclusive=True,
            )
        assignment = json.loads(assignment_path.read_text())["content"]
        workflow = self.item_workflow(item_id)
        candidate_repo = self.candidate_repository(item_id)
        require(
            candidate_repo != self.config.repository, "Candidate must be separate from provider"
        )
        require(item.original_high is not None, "Original estimate is missing")
        if item.state == "Ready":
            async with async_operation_lock(self.root / "admission.lock"):
                if self.config.data["workflow"]["mode"] == "MULTITASK":
                    mine = set(workflow["allowed_paths"])
                    for other in self.provider.snapshot():
                        if other.item_id != item_id and other.state in {"Starting", "Running"}:
                            theirs = set(self.item_workflow(other.item_id)["allowed_paths"])
                            require(not mine & theirs, "Concurrent assignment scopes overlap")
                decision = await self.invoke(
                    item_id,
                    "admit",
                    "coordinator",
                    "You are the Dev Backlog Coordinator. Decide whether to admit this user-authorized bounded "
                    "Work Item using its current provider record below. Do not implement, mutate files, or delegate. "
                    "Return only JSON with operation new or assess, item_id, provider_revision, and reason. "
                    f"Provider revision: {item.revision}\n\n{item.content}",
                )
                self.validate_call_limits(decision, self.config.data["coordinator_limits"])
                value = self.result_json(decision)
                require(
                    value.get("operation") == "new"
                    and value.get("item_id") == item_id
                    and value.get("provider_revision") == item.revision,
                    "No current Coordinator admission",
                )
                self.provider.transition(
                    item_id,
                    item.revision,
                    "Starting",
                    self.authority(decision, item_id, operation="new"),
                    validate=validate_transition,
                )
                item = self.provider.item(item_id)
        acceptance_path = self._stage_path(item_id, "accept")
        if item.state == "Running":
            require(
                acceptance_path.exists(),
                "Running item has no retained acceptance evidence; replacement is prohibited",
            )
            acceptance = json.loads(acceptance_path.read_text())
            require(
                acceptance.get("session", {}).get("session_id") == item.owner,
                "Retained acceptance differs from the canonical owner",
            )
            self.validate_invocation_result(acceptance)
            self.guard(item_id)
        else:
            require(item.state == "Starting", "Only Starting may launch canonical acceptance")
            require(
                self._stage_path(item_id, "admit").exists(),
                "Starting reservation has no retained admission evidence; reconcile before launch",
            )
            self.guard(item_id)
            acceptance = await self.invoke(
                item_id,
                "accept",
                "orchestrator",
                "You are the canonical Dev Orchestrator for this item. Provider reservation is Starting. "
                "This invocation is read-only: do not implement, delegate, or mutate files. Accept ownership "
                'by returning only JSON {"item_id":' + json.dumps(item_id) + ',"accepted":true}. '
                "The harness will record Running for this exact session before resuming source work.\n"
                + assignment,
            )
        value = self.result_json(acceptance)
        require(
            value.get("accepted") is True and value.get("item_id") == item_id,
            "Orchestrator did not accept",
        )
        if item.state == "Starting":
            self.provider.transition(
                item_id,
                item.revision,
                "Running",
                self.authority(acceptance, item_id, accepted=True),
                validate=validate_transition,
            )
        item = self.provider.item(item_id)
        require(
            item.state == "Running" and item.owner == acceptance["session"]["session_id"],
            "Canonical Running assignment differs",
        )
        base_path = self._stage_path(item_id, "base")
        if not base_path.exists():
            atomic_json(
                base_path, {"commit": git(candidate_repo, "rev-parse", "HEAD")}, exclusive=True
            )
        base = json.loads(base_path.read_text())["commit"]
        prompt = (
            "Running is now recorded for your exact session. Implement the Work Item in this candidate "
            "repository only. You own task organization and may use native delegation. Do not edit backlog, "
            "PROJECT.yaml, .codex, tests, or any path outside this repository. Allowed implementation paths: "
            + json.dumps(list(workflow["allowed_paths"]))
            + ". Run the specified checks and create one candidate "
            "commit. Arrange independent review using a fresh native spawn_agent with fork_context=false "
            '(or fork_turns="none" if that is the available schema). Give the reviewer the exact commit SHA, '
            "objective, and check command. The reviewer must not edit files, must review that exact candidate, "
            'and must return JSON {"candidate":"<SHA>","verdict":"ACCEPT or REJECT",'
            '"unresolved_findings":[]}. Wait for its result. Do not claim provider completion or modify '
            'the authoritative provider. Return only JSON {"item_id":'
            + json.dumps(item_id)
            + ',"candidate":"<SHA>","reviewer_session":"<native child ID>",'
            '"request_completion":true}. The harness separately verifies review, tests, delivery and '
            "provider evidence before advancement. Checks: "
            + json.dumps([list(a) for a in workflow["checks"]])
            + "\nWork item:\n"
            + assignment
        )
        prompt += "\nIf an exact operator answer is required before implementation, return only JSON with item_id and question {question_id, text}. Leave the candidate clean at its base commit. Do not implement before approval."
        continuation_path = self._stage_path(item_id, "continuation")
        stage = "produce-review"
        if continuation_path.exists():
            continuation = json.loads(continuation_path.read_text())
            stage = continuation["stage"]
            prompt += "\nPersisted canonical approval: " + json.dumps(continuation["approval"])
        self.guard(item_id)
        produced = await self.invoke(
            item_id,
            stage,
            "orchestrator",
            prompt,
            session=self.session(acceptance),
            read_only=False,
        )
        value = self.result_json(produced)
        if value.get("question"):
            require(
                value.get("item_id") == item_id and isinstance(value["question"], dict),
                "Unbound question",
            )
            require(
                not git(candidate_repo, "status", "--porcelain")
                and git(candidate_repo, "rev-parse", "HEAD") == base,
                "Question boundary contains unapproved source work",
            )
            current = self.provider.item(item_id)
            return self.provider.transition(
                item_id,
                current.revision,
                "User Action Required",
                self.authority(produced, item_id, question=value["question"]),
                validate=validate_transition,
            )
        require(
            value.get("item_id") == item_id and value.get("request_completion") is True,
            "Canonical completion request missing",
        )
        candidate = value.get("candidate")
        from .native_evidence import verify_native_review

        review = verify_native_review(
            produced["session"]["native_session_id"],
            value.get("reviewer_session"),
            candidate,
            self.native_sessions_root(produced["binding"]),
        )
        atomic_json(self._stage_path(item_id, "review"), review)
        checks = self.checks(candidate_repo, item_id, candidate, "source-checks")
        validate_candidate(
            candidate_repo,
            candidate,
            base,
            workflow["allowed_paths"],
            produced["session"]["native_session_id"],
            review,
            checks,
        )
        self.guard(item_id)
        from .delivery import integrate

        delivery = integrate(self, item_id, candidate_repo, candidate, base, review, checks)
        current = self.provider.item(item_id)
        result = self.provider.transition(
            item_id,
            current.revision,
            "Completed",
            self.authority(produced, item_id, delivery=delivery),
            validate=validate_transition,
        )
        return {"item_id": item_id, **result, "delivery": delivery}
