"""Foreground execution of one selected workflow, with durable generation boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import re
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
from .provider import AgentProvider, FileProvider, Item, TransitionBlocked, blob, git
from .runtime import AgentRequest, SessionHandle
from .telemetry import TelemetryReceiver
from .workflow import require, validate_candidate, validate_transition


class Application:
    def __init__(self, config_path, *, control_only=False):
        self.config_path = Path(config_path).resolve()
        self.config = (load_control_config if control_only else load_config)(self.config_path)
        provider_type = (
            AgentProvider
            if self.config.data.get("provider_interaction", "agent") == "agent"
            else FileProvider
        )
        self.provider = provider_type(self.config.repository, self.config.operational_root)
        self.root = self.config.operational_root
        self.projection_lock = threading.Lock()
        self.trace_cache = {}

    async def refresh_provider(self):
        """Refresh on material file-provider changes; ordinary projections remain model-free."""
        if not isinstance(self.provider, AgentProvider):
            return
        async with async_operation_lock(self.root / "provider-refresh.lock"):
            revision = self.provider.source_revision()
            current = load_config(self.config_path)
            observer = digest([current.file_digest, current.binding("coordinator").relevant_digest])
            if self.provider.cache_path.exists():
                cached = json.loads(self.provider.cache_path.read_text())
                if (
                    cached["source_revision"] == revision
                    and cached.get("observer_digest") == observer
                ):
                    return
            result = await self.invoke(
                "provider-inventory",
                "observe-" + digest([revision, observer]),
                "coordinator",
                "Observe the authoritative file provider using its selected management skills. "
                "Do not mutate or dispatch. Classify groups and historical archive debt; return only "
                "actual work items, preserving canonical ownership and unknown estimates. "
                "Return compact JSON {items:[{item_id,path,state,owner,original_high}], "
                "policy:{eligible,mode,primary_branch,evidence},questions:{},archive_debt:[], "
                "non_items:[{path,kind:group/index/archive_debt/supporting_document,reason}], "
                "dependencies:{item_id:[required_item_ids]}, "
                "transition_paths:{item_id:{Completed:[exact_source_and_archive_paths]}}}. "
                "Determine mode and eligibility from current Coordinator/crisis authority; do not "
                "infer expiry. Policy evidence must be [{path,sha256,excerpt,supports:[mode/admission]}], "
                "citing exact current source bytes and the authority for both mode and admission. "
                "Do not copy document contents: the harness reads the referenced bytes and computes "
                "revision hashes. Classify every existing backlog Markdown file "
                "exactly once as an item or non_item with a reason. Omit Future Ideas entirely. "
                "If resource claims are selected, report helper:{discovery:native_tool_catalog, "
                "available:boolean,tools:[exact_exposed_tool_names]} based on tools actually exposed "
                "in this invocation. Do not infer availability from configuration text and do not "
                "invoke any claim operation as a probe. Report unavailable when unproven.",
                purpose="provider",
            )
            self.validate_call_limits(
                result, load_config(self.config_path).data["coordinator_limits"]
            )
            value = self.result_json(result)
            self.provider.validate_inventory(value)
            for row in value["items"]:
                path = self.config.repository / row["path"]
                require(
                    not Path(row["path"]).is_absolute()
                    and ".." not in Path(row["path"]).parts
                    and path.resolve().is_relative_to(self.config.repository / "backlog")
                    and not path.is_symlink(),
                    "Unsafe observed item path",
                )
                content = path.read_bytes()
                row.setdefault("content", content.decode("utf-8"))
                row.setdefault(
                    "revision", sha256(row["path"].encode() + b"\0" + content).hexdigest()
                )
            items = [Item(**row) for row in value["items"]]
            require(
                len({item.item_id for item in items}) == len(items), "Duplicate provider identity"
            )
            for item in items:
                path = self.config.repository / item.path
                require(
                    not Path(item.path).is_absolute()
                    and ".." not in Path(item.path).parts
                    and path.resolve().is_relative_to(self.config.repository / "backlog")
                    and "future-ideas" not in Path(item.path).parts,
                    "Unsafe observed item path",
                )
                require(path.read_bytes() == item.content.encode(), "Observed item content differs")
                require(
                    sha256(item.path.encode() + b"\0" + item.content.encode()).hexdigest()
                    == item.revision,
                    "Observed item revision differs",
                )
            self.provider.validate_policy(value["policy"])
            require(
                self.provider.source_revision() == revision, "Provider changed during observation"
            )
            atomic_json(
                self.provider.cache_path,
                {
                    **value,
                    "source_revision": revision,
                    "observer_digest": observer,
                    "invocation_id": result["invocation_id"],
                },
            )

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

    async def transition(self, item_id, expected_revision, target, authority, *, validate):
        if not isinstance(self.provider, AgentProvider):
            return self.provider.transition(
                item_id, expected_revision, target, authority, validate=validate
            )
        retained = []
        for path in (self.root / "provider-agent-operations").glob("*/requested.json"):
            record = json.loads(path.read_text())
            if (
                record["item"]["item_id"] == item_id
                and record["item"]["revision"] == expected_revision
                and record["target"] == target
                and record["authority"] == authority
            ):
                retained.append(record)
        require(len(retained) <= 1, "Provider operation is ambiguous")
        if not retained:
            self.validate_management_readiness(
                "coordinator" if authority["role"] == "operator" else authority["role"]
            )
        item = Item(**retained[0]["item"]) if retained else self.provider.item(item_id)
        require(item.revision == expected_revision, "Stale provider revision")
        validate(item, target, authority)
        routes = (
            {}
            if retained
            else self.provider.observation().get("transition_paths", {}).get(item_id, {})
        )
        paths = retained[0]["paths"] if retained else routes.get(target)
        if paths is None and target != "Completed":
            paths = [item.path]
        require(paths and item.path in paths, "Provider transition paths are missing")
        receipt = await self.invoke_provider_transition(item, target, authority, paths)
        await self.refresh_provider()
        current = self.provider.item(item_id)
        require(
            asdict(current) == receipt["after"], "Provider refresh differs from committed receipt"
        )
        return {**receipt, "item_id": item_id, "state": current.state, "revision": current.revision}

    def validate_management_readiness(self, role):
        """Check selected skills and native helper discovery before a new mutation."""
        import yaml

        current = load_config(self.config_path)
        binding = current.binding(role)
        profile = current.data["profiles"][binding.profile_name]
        require(
            tuple(profile["permissions"]) == ("workspace-write",),
            "Provider management role needs workspace-write permission",
        )
        required = {"manage-work-items", "manage-work-items-file"}
        project = yaml.safe_load((current.repository / "PROJECT.yaml").read_text())
        if project.get("resource_coordination", {}).get("selected") == "resource-claim":
            required |= {"resource-claim", "resource-claim-helper", "resource-claim-helper-mcp"}
            require(
                project.get("agent_claim_transport", {}).get("selected") == "mcp",
                "Selected claim helper needs a supported native CLI capability",
            )
            options = current.data["agent_clis"][binding.cli_name].get("adapter_options", {})
            require(
                options.get("load_user_config") is True,
                "Configured native MCP helper is not loaded by this CLI",
            )
            observer = current.binding("coordinator")
            require(
                binding.origin == observer.origin,
                "Management CLI has no matching helper discovery binding",
            )
            observation = self.provider.observation()
            require(
                observation.get("observer_digest")
                == digest([current.file_digest, observer.relevant_digest]),
                "Helper discovery configuration is stale",
            )
            helper = observation.get("helper", {})
            required_tools = {"claim_acquire", "claim_release", "claim_heartbeat", "claim_status"}
            observed = {name.rsplit("__", 1)[-1] for name in helper.get("tools", [])}
            require(
                helper.get("discovery") == "native_tool_catalog"
                and helper.get("available") is True
                and required_tools <= observed,
                "Selected claim helper has no native tool-discovery evidence",
            )
        root = Path(current.data["methodology_root"])
        require(
            all((root / "skills" / name / "SKILL.md").is_file() for name in required),
            "Provider management skills are missing",
        )

    async def enforce_guard(self, item_id):
        try:
            return self.guard(item_id)
        except TransitionBlocked:
            if isinstance(self.provider, AgentProvider):
                item = self.provider.item(item_id)
                view = self.usage_view(item_id)
                admission = self._stage_path(item_id, "admit")
                if (
                    not view["may_generate"]
                    and item.state in {"Starting", "Running"}
                    and admission.exists()
                ):
                    await self.transition(
                        item_id,
                        item.revision,
                        "Holding",
                        self.authority(
                            json.loads(admission.read_text()),
                            item_id,
                            incident="usage_unknown"
                            if view["status"] == "unknown"
                            else "usage_limit",
                            policy="configured original-estimate generation guard",
                            usage=view,
                        ),
                        validate=validate_transition,
                    )
            raise

    def verify_agent_admission(self, item_id, revision, authority, assignment):
        matches = []
        for path in (self.root / "provider-agent-operations").glob("*/requested.json"):
            value = json.loads(path.read_text())
            if (
                value["item"]["item_id"] == item_id
                and value["item"]["revision"] == revision
                and value["target"] == "Starting"
                and value["authority"] == authority
            ):
                matches.append((path, value))
        require(len(matches) == 1, "Starting reservation has no unique agent admission operation")
        path, record = matches[0]
        require(record["item"]["content"] == assignment, "Admission assignment differs")
        receipt_path = path.parent / "receipt.json"
        require(receipt_path.exists(), "Agent admission receipt is unresolved")
        receipt = json.loads(receipt_path.read_text())
        require(
            receipt.get("advancement_verified") is True,
            "Provider admission effect is recorded but advancement remains fenced",
        )
        self.verify_provider_receipt(
            record,
            {"operation_id": record["stage_operation"], "before_revision": revision, **receipt},
        )

    async def invoke_provider_transition(self, item, target, authority, paths):
        """Submit one agent-managed operation; verify its commit before returning a receipt."""
        validate_transition(item, target, authority)
        archive_paths = [path for path in paths if path != item.path]
        if target == "Completed":
            require(
                len(paths) == 2 and len(archive_paths) == 1,
                "Completion requires exact source and archive paths",
            )
        expected_path = archive_paths[0] if target == "Completed" else item.path
        if authority["role"] == "operator":
            question, answer = authority["question"], authority["answer"]
            key = digest([question["question_id"], item.revision, answer["text"]])
            path = self._stage_path(item.item_id, "answer-operation-" + key)
            require(path.exists(), "Persisted operator answer is missing")
            saved = json.loads(path.read_text())
            require(
                saved["before_revision"] == item.revision
                and saved["question"] == question
                and saved["answer"] == answer
                and answer["digest"] == digest(answer["text"]),
                "Operator answer evidence differs",
            )
            executing_role = "coordinator"
        else:
            decisions = []
            for path in self._stage_path(item.item_id, "unused").parent.glob("*.json"):
                value = json.loads(path.read_text())
                if isinstance(value, dict) and value.get("invocation_id") == authority.get(
                    "invocation_id"
                ):
                    decisions.append(value)
            require(len(decisions) == 1, "Provider decision-owner evidence is absent or ambiguous")
            decision = decisions[0]
            self.validate_invocation_result(decision)
            require(
                decision["role"] == authority["role"]
                and decision["session"]["session_id"] == authority.get("session_id")
                and decision["session"]["native_session_id"] == authority.get("native_session_id"),
                "Provider decision owner differs from observed invocation",
            )
            executing_role = authority["role"]
        stage = "provider-" + digest([asdict(item), target, authority, paths])
        async with async_operation_lock(self.config.repository / ".git/agentic-provider.lock"):
            request_path = self._stage_path(item.item_id, stage + "-request")
            if request_path.exists():
                record = json.loads(request_path.read_text())
            else:
                head = git(self.config.repository, "rev-parse", "HEAD")
                require(
                    blob(self.config.repository, head, item.path) == item.content.encode(),
                    "Observed provider content differs from committed source",
                )
                prompt = (
                    "Perform only this authorized provider transition using the project's selected "
                    "management skills. Manage configured claims yourself. Recheck the expected "
                    "revision at the write boundary; reject a mismatch. Do not implement source work. "
                    "You execute this decision on behalf of its recorded owner; you are not the canonical "
                    "item session. Preserve that owner and make no additional lifecycle decisions. "
                    "Commit only the declared provider paths. If the outcome is uncertain, report it; "
                    "do not repeat the mutation. Return JSON with operation_id (the stage operation "
                    "below), before_revision, commit, and after (item_id, path, revision, state, owner, "
                    "original_high, content). The revision is SHA256(path UTF-8 + NUL + content UTF-8).\n"
                    + json.dumps(
                        {
                            "operation_id": item.item_id + ":" + stage,
                            "item": asdict(item),
                            "target": target,
                            "authority": authority,
                            "paths": paths,
                        },
                        sort_keys=True,
                    )
                )
                record = {
                    "repository": str(self.config.repository),
                    "head": head,
                    "stage_operation": item.item_id + ":" + stage,
                    "prompt_digest": digest(prompt),
                    "prompt": prompt,
                    "item": asdict(item),
                    "target": target,
                    "authority": authority,
                    "executing_role": executing_role,
                    "paths": paths,
                    "expected_path": expected_path,
                }
                atomic_json(request_path, record, exclusive=True)
            operation = digest(record)
            evidence = self.root / "provider-agent-operations" / component(operation)
            if not (evidence / "requested.json").exists():
                atomic_json(evidence / "requested.json", record, exclusive=True)
            result = await self.invoke(
                item.item_id,
                stage,
                executing_role,
                record["prompt"],
                read_only=False,
                purpose="provider",
                provider_operation=operation,
            )
            value = self.result_json(result)
            receipt = self.verify_provider_receipt(record, value)
            receipt["decision_owner"] = authority.get("session_id", authority["invocation_id"])
            receipt["executing_invocation"] = result["invocation_id"]
            receipt["executing_session"] = result["session"]
            receipt["usage_evidence"] = result["evidence_path"]
            receipt["advancement_verified"] = False
            if not (evidence / "receipt.json").exists():
                atomic_json(evidence / "receipt.json", receipt, exclusive=True)
            self.validate_call_limits(
                result, load_config(self.config_path).data["administrative_review_limits"]
            )
            receipt["advancement_verified"] = True
            atomic_json(evidence / "receipt.json", receipt)
            return receipt

    def reconcile_agent_provider(self, request_path):
        """Record an immutable effect even when invocation policy evidence cannot permit advancement."""
        record = json.loads(request_path.read_text())
        destination = request_path.parent / "receipt.json"
        if destination.exists():
            receipt = json.loads(destination.read_text())
            self.verify_provider_receipt(
                record,
                {
                    "operation_id": record["stage_operation"],
                    "before_revision": record["item"]["revision"],
                    **receipt,
                },
            )
            return receipt
        item_id = record["item"]["item_id"]
        stage = record["stage_operation"][len(item_id) + 1 :]
        saved = self._stage_path(item_id, stage)
        if saved.exists():
            result = json.loads(saved.read_text())
        else:
            root = EvidenceStore(self.root, "item:" + item_id).run
            candidates = list(
                (root / "operations" / component(record["stage_operation"])).glob(
                    "invocations/*/events.jsonl"
                )
            )
            require(len(candidates) == 1, "Provider effect has no unique invocation evidence")
            events, _, partial = read_jsonl(candidates[0])
            require(not partial, "Provider event evidence is partial")
            messages = [
                event["text"] for event in events if event.get("item_type") == "agent_message"
            ]
            require(messages, "Provider result evidence is missing")
            result = {"text": messages[-1]}
        receipt = self.verify_provider_receipt(record, self.result_json(result))
        receipt["advancement_verified"] = False
        atomic_json(destination, receipt, exclusive=True)
        return receipt

    def verify_provider_receipt(self, record, value):
        """Check immutable provider evidence without parsing its lifecycle document format."""
        require(
            value.get("operation_id") == record["stage_operation"], "Provider operation differs"
        )
        require(
            value.get("before_revision") == record["item"]["revision"], "Provider revision differs"
        )
        after = Item(**value["after"])
        require(after.item_id == record["item"]["item_id"], "Provider item differs")
        require(after.state == record["target"], "Provider transition is incomplete")
        expected_owner = record["item"]["owner"]
        if (record["item"]["state"], record["target"]) == ("Starting", "Running"):
            expected_owner = record["authority"]["session_id"]
        require(after.owner == expected_owner, "Provider operation changed canonical owner")
        expected_path = record.get("expected_path", record["item"]["path"])
        require(after.path == expected_path, "Provider result path differs from destination")
        commit = value.get("commit")
        require(
            isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", commit),
            "Provider commit identity is missing",
        )
        require(
            git(self.config.repository, "rev-parse", commit + "^") == record["head"],
            "Provider commit parent differs",
        )
        require(
            git(self.config.repository, "merge-base", "HEAD", commit) == commit,
            "Provider commit is not in authoritative history",
        )
        changed = git(
            self.config.repository, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
        ).splitlines()
        require(
            changed and set(changed) <= set(record["paths"]), "Provider commit exceeds operation"
        )
        if record["target"] == "Completed":
            require(set(changed) == set(record["paths"]), "Provider archive paths are incomplete")
            require(
                not git(
                    self.config.repository,
                    "ls-tree",
                    "--name-only",
                    commit,
                    "--",
                    record["item"]["path"],
                ),
                "Completed source was not archived",
            )
        content = blob(self.config.repository, commit, after.path)
        require(content == after.content.encode(), "Provider receipt content differs from commit")
        require(
            sha256(after.path.encode() + b"\0" + content).hexdigest() == after.revision,
            "Provider resulting revision differs",
        )
        return {"operation": digest(record), "commit": commit, "after": asdict(after)}

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

    async def invoke(
        self,
        item_id,
        stage,
        role,
        prompt,
        *,
        session=None,
        read_only=True,
        purpose="implementation",
        provider_operation=None,
    ):
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
                    purpose=purpose,
                    provider_operation=provider_operation,
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
        self,
        item_id,
        stage,
        role,
        prompt,
        *,
        session=None,
        read_only=True,
        reservation=None,
        purpose="implementation",
        provider_operation=None,
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
        require(purpose in {"implementation", "provider"}, "Unknown invocation purpose")
        if purpose == "provider":
            data = plain(snapshot.data)
            data["workspace"] = str(snapshot.repository)
            snapshot = replace(snapshot, data=freeze(data))
        elif "candidate_root" in snapshot.data:
            data = plain(snapshot.data)
            data["workspace"] = str(self.candidate_repository(item_id))
            snapshot = replace(snapshot, data=freeze(data))
        binding = snapshot.binding(role)
        saved = self._stage_path(item_id, stage)
        # Preserve existing implementation receipts; provider effects bind their workspace
        # and access mode so a saved stage cannot be reused across execution purposes.
        request_hash = (
            digest([purpose, str(snapshot.repository), read_only, provider_operation, prompt])
            if purpose == "provider"
            else digest(prompt)
        )
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
            context_path = path / "execution-context.json"
            context = {"purpose": purpose, "provider_operation": provider_operation}
            if context_path.exists():
                require(
                    json.loads(context_path.read_text()) == context, "Invocation purpose changed"
                )
            else:
                atomic_json(context_path, context, exclusive=True)
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
                purpose=purpose,
                provider_operation=provider_operation,
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
            "purpose": purpose,
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
            "purpose": (
                json.loads((path / "execution-context.json").read_text())["purpose"]
                if (path / "execution-context.json").exists()
                else "implementation"
            ),
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
                and value.get("purpose", "implementation") != "provider"
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
                and not (
                    (path.parent / "execution-context.json").exists()
                    and json.loads((path.parent / "execution-context.json").read_text()).get(
                        "purpose"
                    )
                    == "provider"
                )
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
        return await self.transition(
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
            persisted = await self.transition(
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
            await self.enforce_guard(item_id)
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
            result = await self.transition(
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
        for request in sorted((self.root / "provider-agent-operations").glob("*/requested.json")):
            try:
                receipt = self.reconcile_agent_provider(request)
                results.append(
                    {
                        "provider_operation": request.parent.name,
                        "outcome": "returned"
                        if receipt.get("advancement_verified")
                        else "unresolved",
                        "effect_verified": True,
                        "receipt": receipt,
                    }
                )
            except (ValueError, OSError, KeyError, TypeError) as exc:
                results.append(
                    {
                        "provider_operation": request.parent.name,
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
                if isinstance(self.provider, AgentProvider):
                    await self.refresh_provider()
                    project_mode = self.provider.policy()["mode"]
                    self.validate_management_readiness("coordinator")
                    self.validate_management_readiness("orchestrator")
                else:
                    self.provider.policy()
                    import yaml

                    project = yaml.safe_load((self.config.repository / "PROJECT.yaml").read_text())
                    project_mode = project["execution_mode"]
                require(
                    project_mode == mode,
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
                    "provider_revision": item.revision,
                    "provider_path": item.path,
                    "original_high": item.original_high,
                    "workflow": self.item_workflow(item_id),
                    "candidate_root": self.config.data.get("candidate_root"),
                    "workspace": self.config.data["workspace"],
                },
                exclusive=True,
            )
        frozen_assignment = json.loads(assignment_path.read_text())
        assignment = frozen_assignment["content"]
        if item.state == "Ready":
            require(
                frozen_assignment.get("provider_revision") == item.revision
                and assignment == item.content
                and frozen_assignment.get("original_high") == item.original_high,
                "Ready provider record differs from the frozen assignment; reconcile before admission",
            )
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
                await self.transition(
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
            await self.enforce_guard(item_id)
        else:
            require(item.state == "Starting", "Only Starting may launch canonical acceptance")
            require(
                self._stage_path(item_id, "admit").exists(),
                "Starting reservation has no retained admission evidence; reconcile before launch",
            )
            admission = json.loads(self._stage_path(item_id, "admit").read_text())
            admitted = self.result_json(admission)
            source_revision = frozen_assignment.get("provider_revision")
            require(
                source_revision
                and admitted.get("operation") == "new"
                and admitted.get("item_id") == item_id
                and admitted.get("provider_revision") == source_revision,
                "Retained admission differs from the frozen assignment",
            )
            self.validate_invocation_result(admission)
            admission_authority = self.authority(admission, item_id, operation="new")
            if isinstance(self.provider, AgentProvider):
                self.verify_agent_admission(
                    item_id, source_revision, admission_authority, assignment
                )
            else:
                record = (
                    self.root
                    / "provider-operations"
                    / component(digest([item_id, source_revision, "Starting", admission_authority]))
                )
                require(
                    (record / "requested.json").exists(),
                    "Starting reservation has no bound provider admission operation",
                )
                request = json.loads((record / "requested.json").read_text())
                require(
                    request.get("before_revision") == source_revision
                    and request.get("authority") == admission_authority
                    and request.get("item_id") == item_id
                    and request.get("target") == "Starting",
                    "Provider admission operation differs from retained admission",
                )
                from .provider import blob

                require(
                    frozen_assignment.get("provider_path") in request["paths"]
                    and blob(
                        self.config.repository, request["head"], frozen_assignment["provider_path"]
                    ).decode()
                    == assignment,
                    "Frozen assignment differs from the admitted provider content",
                )
                self.provider.reconcile_operation(record)
            await self.enforce_guard(item_id)
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
            await self.transition(
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
            "repository only. You own task organization and may use native delegation. "
            "Use the project's configured claim helper yourself when its coordination policy requires claims; "
            "the harness does not acquire, renew, or release claims for you. Claim operations do not authorize "
            "source edits outside the allowed implementation paths or provider lifecycle changes. "
            "Allowed implementation paths: "
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
        await self.enforce_guard(item_id)
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
            return await self.transition(
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
        await self.enforce_guard(item_id)
        from .delivery import integrate

        if isinstance(self.provider, AgentProvider):
            delivery = await asyncio.to_thread(
                integrate, self, item_id, candidate_repo, candidate, base, review, checks
            )
            await self.refresh_provider()
        else:
            delivery = integrate(self, item_id, candidate_repo, candidate, base, review, checks)
        current = self.provider.item(item_id)
        result = await self.transition(
            item_id,
            current.revision,
            "Completed",
            self.authority(produced, item_id, delivery=delivery),
            validate=validate_transition,
        )
        return {"item_id": item_id, **result, "delivery": delivery}
