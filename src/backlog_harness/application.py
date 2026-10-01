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
from .workflow import preparation_question_content, require, validate_candidate, validate_transition


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

    @staticmethod
    def provider_observer_digest(current):
        return digest(
            [
                str(current.repository),
                current.data.get("methodology_root"),
                current.data.get("provider"),
                current.data.get("provider_interaction"),
                current.binding("coordinator").relevant_digest,
            ]
        )

    async def refresh_provider(self):
        """Refresh on material file-provider changes; ordinary projections remain model-free."""
        if not isinstance(self.provider, AgentProvider):
            return
        async with async_operation_lock(self.root / "provider-refresh.lock"):
            revision = self.provider.source_revision()
            current = load_config(self.config_path)
            observer = self.provider_observer_digest(current)
            if self.provider.cache_path.exists():
                cached = json.loads(self.provider.cache_path.read_text())
                legacy_observer = digest(
                    [current.file_digest, current.binding("coordinator").relevant_digest]
                )
                if cached.get("observer_digest") == legacy_observer:
                    self.provider.validate_policy(cached["policy"])
                    cached["observer_digest"] = observer
                    atomic_json(self.provider.cache_path, cached)
                if (
                    cached["source_revision"] == revision
                    and cached.get("observer_digest") == observer
                ):
                    return
                if (
                    cached.get("source_manifest") == self.provider.source_manifest()
                    and cached.get("observer_digest") == observer
                ):
                    self.provider.validate_policy(cached["policy"])
                    cached["source_revision"] = revision
                    atomic_json(self.provider.cache_path, cached)
                    return
            result = await self.invoke(
                "provider-inventory",
                "observe-" + digest([revision, observer]),
                "coordinator",
                "Observe the authoritative file provider using its selected management skills. "
                "Do not mutate or dispatch. Classify groups and historical archive debt; return only "
                "actual work items, preserving canonical ownership and unknown estimates. "
                "Return compact JSON {items:[{item_id,path,state,owner,original_high}], "
                "policy:{eligible:boolean,mode,primary_branch,evidence},questions:{},archive_debt:[], "
                "non_items:[{path,kind:group/index/archive_debt/supporting_document,reason}], "
                "dependencies:{item_id:[required_item_ids]}, "
                "dependency_omissions:unknown, "
                "transition_paths:{item_id:{Completed:[exact_source_and_archive_paths]}}}. "
                "Omitted dependency entries are unknown and cannot "
                "authorize execution; use [] only for explicitly established no dependencies. "
                "policy.eligible is GLOBAL NEW admission, not item-local eligibility or a list of owners. "
                "An item-local user wait does not establish a global pause; cite global authority. "
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
            value = self.result_json(result)
            identities = {row["item_id"] for row in value.get("items", [])}
            dependencies = value.get("dependencies", {})
            ambiguous_dependencies = (
                isinstance(dependencies, dict)
                and bool(identities - dependencies.keys())
                and value.get("dependency_omissions") != "unknown"
            )
            if type(value.get("policy", {}).get("eligible")) is not bool or ambiguous_dependencies:
                self.validate_invocation_result(result)
                self.validate_call_limits(result, current.data["coordinator_limits"])
                clarification = await self.invoke(
                    "provider-inventory",
                    "clarify-" + digest([revision, result["invocation_id"]]),
                    "coordinator",
                    "Clarify only your retained provider observation. Do not repeat inventory, "
                    "mutate, dispatch, or invoke claims. Return JSON "
                    "{dependency_omissions:unknown|none,admission_open:boolean,reason:string}. "
                    "Use none only if every omitted dependency entry was established to have "
                    "no dependencies; otherwise use unknown. admission_open concerns NEW "
                    "admission under the cited authority, not continuation of existing owners.",
                    session=self.session(result),
                    purpose="provider",
                )
                self.accept_provider_clarification(result, clarification, revision, observer)
            else:
                self._accept_provider_observation(result, revision, observer)

    async def reassess_policy(self):
        """Reassess scheduling authority without repeating or replacing inventory."""
        require(
            isinstance(self.provider, AgentProvider), "Policy reassessment requires agent provider"
        )
        async with async_operation_lock(self.root / "provider-refresh.lock"):
            observed = self.provider.observation()
            revision = observed["source_revision"]
            result = await self.invoke(
                "provider-policy",
                "reassess-" + digest([revision, observed["policy"]]),
                "coordinator",
                "Reassess global admission from current source authority. Read-only: no mutation, "
                "delegation, claims, browser use or launches. The prior projection may confuse an "
                "item-local wait with a global pause. An item awaiting user action and a stopped "
                "owner do not themselves establish a global dispatch pause. Determine whether "
                "current crisis rules permit other independent work, preserving serial execution "
                "and all item-local restrictions. Do not infer permission from this request. "
                "Return JSON {source_revision,reason,policy:{eligible:boolean,mode,primary_branch,"
                "evidence:[{path,sha256,excerpt,supports:[mode/admission]}]}}. Cite exact source "
                "bytes establishing global authority, not merely one item's eligibility.\n"
                + json.dumps({"source_revision": revision, "prior_policy": observed["policy"]}),
                purpose="provider",
            )
            self.validate_invocation_result(result)
            self.validate_call_limits(result, self.config.data["coordinator_limits"])
            answer = self.result_json(result)
            require(
                answer.get("source_revision") == revision
                and isinstance(answer.get("reason"), str)
                and answer["reason"].strip(),
                "Policy reassessment identity or reason is missing",
            )
            self.provider.validate_policy(answer.get("policy", {}))
            require(
                self.provider.source_revision() == revision,
                "Provider changed during policy reassessment",
            )
            observed["policy"] = answer["policy"]
            observed["policy_reassessment"] = {
                "invocation_id": result["invocation_id"],
                "reason": answer["reason"],
            }
            atomic_json(self.provider.cache_path, observed)
            return observed["policy"]

    def accept_provider_clarification(self, original, clarification, revision, observer):
        """Bind a small same-session schema clarification to the retained full inventory."""
        for result in (original, clarification):
            self.validate_invocation_result(result)
            self.validate_call_limits(result, self.config.data["coordinator_limits"])
        require(
            original["session"] == clarification["session"]
            and original["binding"] == clarification["binding"],
            "Provider clarification must preserve the originating session and binding",
        )
        value = self.result_json(original)
        answer = self.result_json(clarification)
        require(
            answer.get("dependency_omissions") in {"unknown", "none"}
            and type(answer.get("admission_open")) is bool
            and isinstance(answer.get("reason"), str)
            and answer["reason"].strip(),
            "Provider clarification semantics are incomplete",
        )
        if answer["dependency_omissions"] == "none":
            value.pop("dependency_omissions", None)
            for row in value["items"]:
                value["dependencies"].setdefault(row["item_id"], [])
        else:
            value["dependency_omissions"] = "unknown"
        value["policy"]["eligible"] = answer["admission_open"]
        value["observation_invocations"] = [
            original["invocation_id"],
            clarification["invocation_id"],
        ]
        self._accept_provider_observation(clarification, revision, observer, value=value)

    def _accept_provider_observation(self, result, revision, observer, *, value=None):
        """Validate and cache one read-only provider inventory result."""
        self.validate_call_limits(result, load_config(self.config_path).data["coordinator_limits"])
        value = self.result_json(result) if value is None else value
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
            row.setdefault("revision", sha256(row["path"].encode() + b"\0" + content).hexdigest())
        items = [Item(**row) for row in value["items"]]
        require(len({item.item_id for item in items}) == len(items), "Duplicate provider identity")
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
        require(self.provider.source_revision() == revision, "Provider changed during observation")
        atomic_json(
            self.provider.cache_path,
            {
                **value,
                "source_revision": revision,
                "observer_digest": observer,
                "invocation_id": result["invocation_id"],
                "source_manifest": self.provider.source_manifest(),
            },
        )
        return value

    def _stage_path(self, item_id, stage):
        return self.root / "workflow-evidence" / component(item_id) / (stage + ".json")

    def item_workflow(self, item_id):
        assignment = self._stage_path(item_id, "assignment")
        if assignment.exists():
            frozen = json.loads(assignment.read_text())
            if "workflow" in frozen:
                return frozen["workflow"]
        recovery = self.recovery_record(item_id)
        if recovery:
            return recovery["workflow"]
        from .estimation import prepared_workflow

        return prepared_workflow(self, item_id, self.config)

    def recovery_record(self, item_id):
        path = self._stage_path(item_id, "recovery")
        return json.loads(path.read_text()) if path.exists() else None

    def execution_policy(self, item_id):
        if not isinstance(self.provider, AgentProvider):
            return self.provider.policy()
        policy = self.provider.observation()["policy"]
        self.provider.validate_policy(policy)
        if policy["eligible"] is not True:
            recovery = self.recovery_record(item_id)
            require(recovery is not None, "No item-specific recovery authorization")
            decision = json.loads(self._stage_path(item_id, recovery["decision_stage"]).read_text())
            self.validate_invocation_result(decision)
            answer = self.result_json(decision)
            require(
                answer.get("item_id") == item_id
                and answer.get("continuation_authorized") is True
                and answer.get("operation") == "redispatch"
                and recovery["decision_invocation"] == decision["invocation_id"],
                "Recovery continuation authority differs",
            )
            for name, expected in recovery["policy_sources"].items():
                require(
                    sha256((self.config.repository / name).read_bytes()).hexdigest() == expected,
                    "Recovery policy source changed",
                )
        return policy

    async def transition(
        self, item_id, expected_revision, target, authority, *, validate, declared_paths=None
    ):
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
        paths = retained[0]["paths"] if retained else declared_paths or routes.get(target)
        if (
            not retained
            and target == "User Action Required"
            and (item.state == "Running" or authority.get("operation") == "await-user")
        ):
            from .recovery_flow import question_handoff_paths

            paths = question_handoff_paths(self.config.repository, item, paths)
        if paths is None and target != "Completed":
            paths = [item.path]
        require(paths and item.path in paths, "Provider transition paths are missing")
        before = json.loads(self.provider.cache_path.read_text())
        manifest = retained[0]["source_manifest"] if retained else self.provider.source_manifest()
        receipt = await self.invoke_provider_transition(item, target, authority, paths)
        self.advance_provider_projection(before, manifest, receipt, paths)
        current = self.provider.item(item_id)
        require(
            asdict(current) == receipt["after"], "Provider refresh differs from committed receipt"
        )
        return {**receipt, "item_id": item_id, "state": current.state, "revision": current.revision}

    def advance_provider_projection(self, before, manifest, receipt, paths):
        """Project one verified effect without asking an agent to reread unrelated history."""
        current = self.provider.source_manifest()
        changed = {p for p in set(manifest) | set(current) if manifest.get(p) != current.get(p)}
        require(changed <= set(paths), "Unrelated provider changes require fresh observation")
        value = dict(before)
        value["items"] = [
            receipt["after"] if row["item_id"] == receipt["after"]["item_id"] else row
            for row in before["items"]
        ]
        value["policy"] = receipt.get("policy", before["policy"])
        if receipt["after"]["state"] == "User Action Required" and receipt.get("question"):
            value["questions"] = {
                **value.get("questions", {}),
                receipt["after"]["item_id"]: {
                    **receipt["question"],
                    "item_id": receipt["after"]["item_id"],
                    "item_revision": receipt["after"]["revision"],
                    "owner": receipt["after"]["owner"],
                    "answer": None,
                    "disposition": None,
                },
            }
        self.provider.validate_inventory(value)
        self.provider.validate_policy(value["policy"])
        value.update(
            source_revision=self.provider.source_revision(),
            source_manifest=current,
            last_provider_receipt=receipt["operation"],
        )
        atomic_json(self.provider.cache_path, value)

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
                observation.get("observer_digest") == self.provider_observer_digest(current),
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

    def admission_path(self, item_id):
        original = self._stage_path(item_id, "admit")
        confirmed = self._stage_path(item_id, "admit-confirmed")
        if confirmed.exists():
            before, after = (json.loads(path.read_text()) for path in (original, confirmed))
            require(
                before["session"] == after["session"] and before["binding"] == after["binding"],
                "Admission clarification identity differs",
            )
            return confirmed
        return original

    def admission_dependencies(self, item_id):
        if not isinstance(self.provider, AgentProvider):
            return []
        names = self.provider.observation().get("dependencies", {}).get(item_id)
        require(names is not None, "Admission dependencies are unknown")
        return [asdict(self.provider.item(name)) for name in names]

    async def enforce_guard(self, item_id):
        try:
            return self.guard(item_id)
        except TransitionBlocked:
            if isinstance(self.provider, AgentProvider):
                item = self.provider.item(item_id)
                view = self.usage_view(item_id)
                admission = self.admission_path(item_id)
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
        if receipt.get("advancement_verified") is not True:
            from .recovery_flow import verify_starting_effect

            reconciliation = path.parent / "effect-reconciliation.json"
            require(
                reconciliation.exists(),
                "Provider admission effect is recorded but advancement remains fenced",
            )
            verify_starting_effect(self, record, receipt, json.loads(reconciliation.read_text()))
        self.verify_provider_receipt(
            record,
            {"operation_id": record["stage_operation"], "before_revision": revision, **receipt},
        )

    def validate_stopped_owner_decision(self, item, authority):
        from .recovery import _validate_runtime_record, validate_active_owner_binding

        recovery = authority["recovery"]
        require(not recovery.get("candidate"), "No-change recovery cannot supply a candidate")
        for runtime in recovery["runtime_evidence"]:
            _validate_runtime_record(runtime)
            validate_active_owner_binding(item, runtime, recovery.get("owner_binding"))
        decision_path = self._stage_path(item.item_id, recovery["decision_stage"])
        decision = json.loads(decision_path.read_text())
        self.validate_invocation_result(decision)
        value = self.result_json(decision)
        require(
            decision["invocation_id"] == authority["invocation_id"]
            and value.get("item_id") == item.item_id
            and value.get("provider_revision") == item.revision
            and value.get("previous_owner") == item.owner
            and value.get("ownership_ended") is True
            and value.get("no_source_changes") is True
            and value.get("operation") == "redispatch"
            and value.get("runtime_digest") == digest(recovery["runtime_evidence"])
            and value.get("owner_binding_digest") == digest(recovery["owner_binding"]),
            "No-change recovery decision differs",
        )

    async def invoke_provider_transition(self, item, target, authority, paths):
        """Submit one agent-managed operation; verify its commit before returning a receipt."""
        validate_transition(item, target, authority)
        if authority.get("recovery", {}).get("no_source_changes") is True and not authority[
            "recovery"
        ].get("repair_effect"):
            self.validate_stopped_owner_decision(item, authority)
        if authority.get("operation") == "await-user":
            outcomes = [
                json.loads(p.read_text())
                for p in self._stage_path(item.item_id, "unused").parent.glob("*.json")
            ]
            outcomes = [
                v
                for v in outcomes
                if isinstance(v, dict)
                and v.get("invocation_id") == authority.get("outcome_evidence")
            ]
            require(len(outcomes) == 1, "Question handoff lacks a unique canonical outcome")
            outcome = outcomes[0]
            self.validate_invocation_result(outcome)
            value = self.result_json(outcome)
            if item.state == "Ready":
                from .estimation import validate_preparation_invocation

                prepared = json.loads(self._stage_path(item.item_id, "preparation").read_text())
                validate_preparation_invocation(
                    outcome, prepared["invocation_config_digest"], self.root
                )
                require(
                    prepared["item"] == asdict(item)
                    and prepared["decision"] == outcome
                    and outcome.get("role") == "coordinator"
                    and value.get("item_id") == item.item_id
                    and value.get("provider_revision") == item.revision
                    and (value.get("blocked") or value.get("status") == "blocked"),
                    "Question handoff preparation outcome differs",
                )
            else:
                require(
                    outcome.get("role") == "orchestrator"
                    and outcome["session"]["session_id"] == item.owner
                    and value.get("item_id") == item.item_id
                    and (
                        (value.get("request_completion") is False and value.get("blockers"))
                        or value.get("question") == authority.get("question")
                    )
                    and (
                        item.state != "User Action Required"
                        or (
                            all(
                                (self.provider.question(item) or {}).get(key)
                                == authority.get("question").get(key)
                                for key in ("question_id", "text")
                            )
                            and not (self.provider.question(item) or {}).get("answer")
                        )
                    ),
                    "Question handoff canonical outcome differs",
                )
        if (item.state, target) == ("Ready", "Ready") and authority.get(
            "operation"
        ) == "redispatch":
            effects = []
            for path in (self.root / "provider-agent-operations").glob("*/incomplete-effect.json"):
                effect = json.loads(path.read_text())
                if effect["commit"] == authority["recovery"]["repair_effect"]:
                    effects.append((path, effect))
            require(len(effects) == 1, "Owner correction lacks a unique incomplete effect")
            path, effect = effects[0]
            prior = json.loads((path.parent / "requested.json").read_text())
            prior_authority = {
                **authority,
                "recovery": {
                    k: v for k, v in authority["recovery"].items() if k != "repair_effect"
                },
            }
            require(
                prior["authority"] == prior_authority and effect["after"] == asdict(item),
                "Owner correction differs from original authority or effect",
            )
            self.verify_provider_receipt(
                prior,
                {
                    "operation_id": prior["stage_operation"],
                    "before_revision": prior["item"]["revision"],
                    **effect,
                },
                preserved_owner=True,
            )
        archive_paths = [path for path in paths if path != item.path]
        if target == "Completed":
            require(
                len(paths) == 2 and len(archive_paths) == 1,
                "Completion requires exact source and archive paths",
            )
        expected_path = (
            archive_paths[0]
            if target in {"Completed", "User Action Required"} and archive_paths
            else item.path
        )
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
            decisions = {}
            for path in self._stage_path(item.item_id, "unused").parent.glob("*.json"):
                value = json.loads(path.read_text())
                if isinstance(value, dict) and value.get("invocation_id") == authority.get(
                    "invocation_id"
                ):
                    decisions[digest(value)] = value
            require(len(decisions) == 1, "Provider decision-owner evidence is absent or ambiguous")
            decision = next(iter(decisions.values()))
            self.validate_invocation_result(decision)
            require(
                decision["role"] == authority["role"]
                and decision["session"]["session_id"] == authority.get("session_id")
                and decision["session"]["native_session_id"] == authority.get("native_session_id"),
                "Provider decision owner differs from observed invocation",
            )
            if authority.get("operation") == "record-estimate":
                estimated = self.result_json(decision)
                require(
                    estimated.get("item_id") == item.item_id
                    and estimated.get("provider_revision") == item.revision
                    and estimated.get("estimate") == authority.get("estimate")
                    and estimated.get("prospective_high") == authority.get("prospective_high"),
                    "Estimate authority differs from observed Coordinator decision",
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
                    "item session. Use the exact target owner supplied below; preserve former owners "
                    "as history, not as the active Owner field. Make no additional lifecycle decisions. "
                    "Commit only the declared provider paths. If the outcome is uncertain, report it; "
                    "do not repeat the mutation. Return JSON with operation_id (the stage operation "
                    "below), before_revision, commit, and after (item_id, path, state, owner, "
                    "original_high). Omit document content and revision; the harness hydrates them "
                    "from your exact committed bytes. Also return policy with eligible (GLOBAL NEW admission, "
                    "never inferred solely from this item-local wait), "
                    "For User Action Required, record the supplied question_id and text verbatim in the "
                    "provider record and return question exactly as supplied in authority. "
                    "For a Ready preparation question, replace only the Status: Ready header with "
                    "Status: User Action Required and append the question after all existing content; "
                    "preserve every other original byte, requirement, owner, history and candidate reference. "
                    "Use ready_question_content exactly when supplied; append no other prose or history. "
                    "mode, primary_branch, and evidence [{path,sha256,excerpt,supports:[mode/admission]}] "
                    "for current source after your transition. Preserve original_high and dependencies. "
                    "For record-estimate, append the dated prospective estimate and exactly "
                    "Prospective Execution High: N using authority.prospective_high for N; keep "
                    "historical original estimate and historical usage explicitly unknown. "
                    "The revision is SHA256(path UTF-8 + NUL + content UTF-8).\n"
                    + json.dumps(
                        {
                            "operation_id": item.item_id + ":" + stage,
                            "item": asdict(item),
                            "target": target,
                            "authority": authority,
                            "paths": paths,
                            "ready_question_content": preparation_question_content(
                                item.content, authority["question"]
                            )
                            if item.state == "Ready" and target == "User Action Required"
                            else None,
                            "target_owner": "Unowned"
                            if target == "Ready"
                            else authority["session_id"]
                            if target == "Running" and item.state == "Starting"
                            else item.owner,
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
                    "source_manifest": self.provider.source_manifest(),
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
            if (item.state, target) == ("Running", "Ready") and value.get("after", {}).get(
                "owner"
            ) == item.owner:
                incomplete = self.verify_provider_receipt(record, value, preserved_owner=True)
                incomplete["advancement_verified"] = False
                incomplete["repair_required"] = "release_active_owner"
                atomic_json(evidence / "incomplete-effect.json", incomplete)
                self.validate_call_limits(
                    result, load_config(self.config_path).data["administrative_review_limits"]
                )
                corrected = {
                    **authority,
                    "recovery": {**authority["recovery"], "repair_effect": incomplete["commit"]},
                }
            else:
                receipt = self.verify_provider_receipt(record, value)
                receipt["decision_owner"] = authority.get("session_id", authority["invocation_id"])
                receipt["executing_invocation"] = result["invocation_id"]
                receipt["executing_session"] = result["session"]
                receipt["usage_evidence"] = result["evidence_path"]
                receipt["advancement_verified"] = False
                atomic_json(evidence / "receipt.json", receipt)
                self.validate_call_limits(
                    result, load_config(self.config_path).data["administrative_review_limits"]
                )
                if "policy" in receipt:
                    self.provider.validate_policy(receipt["policy"])
                receipt["advancement_verified"] = True
                atomic_json(evidence / "receipt.json", receipt)
                return receipt

        corrected_receipt = await self.invoke_provider_transition(
            Item(**incomplete["after"]), target, corrected, paths
        )
        atomic_json(evidence / "resolution.json", {"corrected_by": corrected_receipt["operation"]})
        return corrected_receipt

    def reconcile_agent_provider(self, request_path):
        """Record an immutable effect even when invocation policy evidence cannot permit advancement."""
        record = json.loads(request_path.read_text())
        resolution = request_path.parent / "resolution.json"
        if resolution.exists():
            effect = json.loads((request_path.parent / "incomplete-effect.json").read_text())
            self.verify_provider_receipt(
                record,
                {
                    "operation_id": record["stage_operation"],
                    "before_revision": record["item"]["revision"],
                    **effect,
                },
                preserved_owner=True,
            )
            operation = json.loads(resolution.read_text())["corrected_by"]
            correction_path = (
                self.root / "provider-agent-operations" / component(operation) / "requested.json"
            )
            correction = json.loads(correction_path.read_text())
            require(
                correction["item"] == effect["after"]
                and correction["authority"].get("recovery", {}).get("repair_effect")
                == effect["commit"],
                "Owner correction resolution differs",
            )
            receipt = self.reconcile_agent_provider(correction_path)
            require(receipt["after"]["owner"] == "Unowned", "Owner release is incomplete")
            return {
                "operation": digest(record),
                "incomplete_effect": effect,
                "correction": receipt,
                "advancement_verified": receipt.get("advancement_verified", False),
            }
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

    def verify_provider_receipt(self, record, value, *, preserved_owner=False):
        """Check immutable provider evidence without parsing its lifecycle document format."""
        require(
            value.get("operation_id") == record["stage_operation"], "Provider operation differs"
        )
        require(
            value.get("before_revision") == record["item"]["revision"], "Provider revision differs"
        )
        commit = value.get("commit")
        require(
            isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", commit),
            "Provider commit identity is missing",
        )
        row = dict(value["after"])
        expected_path = record.get("expected_path", record["item"]["path"])
        require(row.get("path") == expected_path, "Provider result path differs from destination")
        committed = blob(self.config.repository, commit, expected_path)
        row.setdefault("content", committed.decode())
        row.setdefault("revision", sha256(expected_path.encode() + b"\0" + committed).hexdigest())
        after = Item(**row)
        require(after.item_id == record["item"]["item_id"], "Provider item differs")
        require(after.state == record["target"], "Provider transition is incomplete")
        expected_owner = record["item"]["owner"]
        if (record["item"]["state"], record["target"]) == ("Starting", "Running"):
            expected_owner = record["authority"]["session_id"]
        if record["target"] == "Ready" and record["authority"].get("operation") == "redispatch":
            require(
                not preserved_owner or record["item"]["state"] == "Running",
                "Only the initial recovery effect may retain its prior owner",
            )
            if not preserved_owner:
                expected_owner = "Unowned"
        unowned_estimate = (
            record.get("authority", {}).get("operation") == "record-estimate"
            and expected_owner in {None, "Unowned"}
            and after.owner in {None, "Unowned"}
        )
        require(
            after.owner == expected_owner or unowned_estimate,
            "Provider operation changed canonical owner",
        )
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
        if record["target"] == "Completed" or expected_path != record["item"]["path"]:
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
        require(after.original_high == record["item"]["original_high"], "Original estimate changed")
        if record.get("authority", {}).get("operation") == "record-estimate":
            require(
                "Prospective Execution High: " + str(record["authority"]["prospective_high"])
                in after.content.splitlines(),
                "Committed prospective estimate is missing",
            )
        receipt = {"operation": digest(record), "commit": commit, "after": asdict(after)}
        if record["target"] == "User Action Required":
            question = record["authority"].get("question")
            require(
                isinstance(question, dict)
                and value.get("question") == question
                and isinstance(question.get("question_id"), str)
                and bool(question["question_id"].strip())
                and isinstance(question.get("text"), str)
                and bool(question["text"].strip())
                and question.get("question_id") in after.content
                and question.get("text") in after.content,
                "Committed provider question differs from the authorized question",
            )
            if record["item"]["state"] == "Ready":
                require(
                    after.content
                    == preparation_question_content(record["item"]["content"], question),
                    "Ready question changed existing content or appended unauthorized content",
                )
            for series_path in record["paths"][2:]:
                series_content = blob(self.config.repository, commit, series_path).decode()
                archived_link = os.path.relpath(after.path, str(Path(series_path).parent))
                require(
                    "](" + archived_link + ")" in series_content
                    and "](" + Path(record["item"]["path"]).name + ")" not in series_content,
                    "Question archive series linkage differs",
                )
            receipt["question"] = question
        if "policy" in value:
            receipt["policy"] = value["policy"]
        return receipt

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
        operation_id=None,
        continuation=None,
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
                    operation_id=operation_id,
                    continuation=continuation,
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
        operation_id=None,
        continuation=None,
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
            recovery = self.recovery_record(item_id)
            selected = recovery["workflow"] if recovery else plain(snapshot.data["workflow"])
            if recovery:
                require(
                    plain(snapshot.data["workflow"]) == recovery["config_workflow"],
                    "Recovery workflow configuration changed",
                )
            if not recovery:
                from .estimation import prepared_workflow

                selected = prepared_workflow(self, item_id, snapshot)
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
        require(purpose in {"implementation", "provider", "proof"}, "Unknown invocation purpose")
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
            if purpose != "implementation"
            else digest(prompt)
        )
        if saved.exists() and continuation is None:
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
        operation = operation_id or item_id + ":" + stage
        operation_path = store.run / "operations" / component(operation)
        if continuation is not None:
            require(operation_id is not None, "Continuation operation identity is required")
            original = [
                path.parent
                for path in operation_path.glob("invocations/*/intent.json")
                if json.loads(path.read_text()).get("action") != "resume-provider-observation"
            ]
            require(len(original) == 1, "Original invocation evidence is absent or ambiguous")
            require(
                EvidenceStore.reconcile(original[0])["invocation_id"]
                == continuation["original_invocation_id"],
                "Continuation original invocation differs",
            )
            continuation_id = "provider-observation-continuation-" + digest(continuation)
            candidates = []
            for intent_path in operation_path.glob("invocations/*/intent.json"):
                intent = json.loads(intent_path.read_text())
                if intent.get("action") != "resume-provider-observation":
                    continue
                require(
                    intent.get("invocation_id") == continuation_id,
                    "Provider observation continuation identity differs",
                )
                continuation_path = intent_path.parent / "continuation.json"
                if continuation_path.exists():
                    require(
                        json.loads(continuation_path.read_text()) == continuation,
                        "Provider observation continuation evidence differs",
                    )
                else:
                    require(
                        intent.get("request_digest") == request_hash
                        and intent.get("binding") == asdict(binding),
                        "Incomplete provider continuation intent differs",
                    )
                    atomic_json(continuation_path, continuation, exclusive=True)
                candidates.append(intent_path.parent)
            require(len(candidates) <= 1, "Provider observation continuation is ambiguous")
            if candidates:
                path = candidates[0]
                prior = EvidenceStore.reconcile(path)
                require(prior["request_digest"] == request_hash, "Continuation request differs")
                invocation_id = prior["invocation_id"]
                if prior["outcome"] != "not_submitted":
                    recovered = self.recover_invocation(path)
                    require(
                        recovered is not None,
                        "Provider observation continuation is unresolved; replacement is prohibited",
                    )
                    recovered["request_digest"] = continuation["semantic_request_digest"]
                    recovered["continuation"] = continuation
                    atomic_json(saved, recovered)
                    self.validate_invocation_result(recovered)
                    return recovered
                require(
                    prior["config_digest"] == snapshot.file_digest,
                    "Unsubmitted continuation configuration changed",
                )
                require(
                    prior["binding"] == asdict(binding),
                    "Unsubmitted continuation binding changed",
                )
            else:
                invocation_id = continuation_id
                path = store.begin(
                    operation,
                    invocation_id,
                    snapshot,
                    binding,
                    action="resume-provider-observation",
                    request_digest=request_hash,
                )
                atomic_json(path / "continuation.json", continuation, exclusive=True)
        elif operation_path.exists():
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
            require(
                prior["config_digest"] == snapshot.file_digest,
                "Unsubmitted intent configuration changed; explicit reconciliation required",
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
            context = {
                "purpose": purpose,
                "provider_operation": provider_operation,
                "read_only": read_only,
            }
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
                timeout_seconds=snapshot.data.get("invocation_timeout_seconds", 180),
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
        if continuation is not None:
            result["continuation_request_digest"] = result["request_digest"]
            result["request_digest"] = continuation["semantic_request_digest"]
            result["continuation"] = continuation
            atomic_json(saved, result)
        else:
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

    def result_json(self, result):
        def parse(text):
            text = text.strip()
            if text.startswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            return json.loads(text)

        try:
            return parse(result["text"])
        except (ValueError, TypeError, AttributeError) as exc:
            if result.get("purpose") == "provider" and len(result.get("text") or "") == 16000:
                # Older adapters clipped final JSON. Recover only an exact completed native
                # response bound to this invocation; preserve the clipped historical evidence.
                from datetime import datetime, timedelta

                from .native_evidence import native_records

                self.validate_invocation_result(result)
                path = Path(result["evidence_path"])
                require(
                    path.resolve().is_relative_to(self.root.resolve()), "Unsafe result evidence"
                )
                prior = EvidenceStore.reconcile(path)
                recorded_session = json.loads((path / "session.json").read_text())
                require(
                    prior["invocation_id"] == result["invocation_id"]
                    and recorded_session["native_session_id"]
                    == result["session"]["native_session_id"],
                    "Native result identity differs",
                )
                records, native_hash = native_records(
                    recorded_session["native_session_id"],
                    self.native_sessions_root(result["binding"]),
                )
                start = datetime.fromisoformat(prior["created_at"])
                end = datetime.fromisoformat(result["events"][-1]["at"]) + timedelta(seconds=5)
                matches = [
                    record["payload"]["last_agent_message"]
                    for record in records
                    if record.get("payload", {}).get("type") == "task_complete"
                    and not record["payload"].get("error")
                    and start <= datetime.fromisoformat(record["timestamp"]) <= end
                    and isinstance(record["payload"].get("last_agent_message"), str)
                    and record["payload"]["last_agent_message"].startswith(result["text"])
                ]
                require(len(matches) == 1, "Exact completed native response is absent or ambiguous")
                value = parse(matches[0])
                atomic_json(
                    path / "native-response-recovery.json",
                    {
                        "invocation_id": result["invocation_id"],
                        "native_session_id": recorded_session["native_session_id"],
                        "native_evidence_sha256": native_hash,
                        "response_sha256": sha256(matches[0].encode()).hexdigest(),
                        "text": matches[0],
                    },
                )
                return value
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
            **(
                {
                    "accounting_scope": "recovery_remaining_work",
                    "historical_usage": "unknown",
                    "historical_original_high": self.recovery_record(item_id)[
                        "historical_original_high"
                    ],
                }
                if self.recovery_record(item_id)
                else {
                    "accounting_scope": "prospective_execution",
                    "historical_usage": "unknown",
                    "historical_original_high": None,
                }
                if frozen_path.exists()
                and json.loads(frozen_path.read_text()).get("prospective_estimate_operation")
                and json.loads(frozen_path.read_text()).get("historical_original_high") is None
                else {}
            ),
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
            admit_path = self.admission_path(item_id)
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

    def _interrupted_provider_usage(self, path, prior, session):
        """Return complete interrupted usage or reject evidence with uncovered output."""
        from datetime import datetime

        from .native_evidence import child_usage, native_records
        from .telemetry import Sink, spans

        report_path = path / "telemetry-report.json"
        telemetry_record = path / "telemetry.json"
        require(
            report_path.exists() and telemetry_record.exists(),
            "Interrupted provider usage evidence is missing",
        )
        report = json.loads(report_path.read_text())
        telemetry_path = Path(json.loads(telemetry_record.read_text())["path"])
        require(
            telemetry_path.resolve().is_relative_to(self.root.resolve()),
            "Interrupted telemetry escaped evidence root",
        )
        sink = Sink(telemetry_path, {})
        require(
            report.get("span_count") == len(sink.seen)
            and report.get("rejected_exports") == 0
            and report.get("evidence_sha256") == sink.evidence_digest(),
            "Interrupted provider telemetry is incomplete or changed",
        )
        rows, _, partial = read_jsonl(telemetry_path)
        require(not partial, "Interrupted provider telemetry is partial")
        total = 0
        latest_usage_end = None
        seen = {}
        for payload in rows:
            for _, _, span in spans(payload):
                identity = span.get("traceId"), span.get("spanId")
                fingerprint = digest(span)
                require(
                    identity not in seen or seen[identity] == fingerprint,
                    "Interrupted provider telemetry has conflicting duplicate spans",
                )
                if identity in seen:
                    continue
                seen[identity] = fingerprint
                attributes = {value["key"]: value["value"] for value in span.get("attributes", [])}
                if "gen_ai.usage.output_tokens" not in attributes:
                    continue
                output = attributes["gen_ai.usage.output_tokens"].get("intValue")
                end = span.get("endTimeUnixNano")
                require(
                    isinstance(output, (int, str))
                    and str(output).isdigit()
                    and isinstance(end, (int, str))
                    and str(end).isdigit(),
                    "Interrupted provider telemetry usage is invalid",
                )
                total += int(output)
                latest_usage_end = max(latest_usage_end or 0, int(end))
        require(latest_usage_end is not None, "Interrupted provider usage is unknown")

        records, native_digest = native_records(
            session["native_session_id"], self.native_sessions_root(prior["binding"])
        )
        native_paths = list(
            self.native_sessions_root(prior["binding"]).glob(
                f"*/*/*/*{session['native_session_id']}.jsonl"
            )
        )
        require(len(native_paths) == 1, "Exact native session evidence is absent or ambiguous")
        counters = [
            record["payload"]["thread_token_usage"].get("output_tokens")
            for record in records
            if record.get("type") == "token_usage_record"
            and record.get("payload", {}).get("session_id") == session["native_session_id"]
            and isinstance(record.get("payload", {}).get("thread_token_usage"), dict)
        ]
        visible = [
            record["payload"]["info"]["total_token_usage"].get("output_tokens")
            for record in records
            if record.get("type") == "event_msg"
            and record.get("payload", {}).get("type") == "token_count"
            and isinstance(record.get("payload", {}).get("info"), dict)
        ]
        require(
            counters
            and visible
            and type(counters[-1]) is int
            and counters[-1] >= 0
            and counters[-1] == visible[-1],
            "Interrupted native usage is unknown",
        )
        # Child response spans cannot establish coverage for the parent's later output.
        parent_usage_records = [
            record
            for record in records
            if record.get("type") == "token_usage_record"
            and record.get("payload", {}).get("session_id") == session["native_session_id"]
            and isinstance(record.get("payload", {}).get("thread_token_usage"), dict)
        ]
        parent_usage_end = int(
            datetime.fromisoformat(parent_usage_records[-1]["timestamp"]).timestamp()
            * 1_000_000_000
        )
        for record in records:
            payload = record.get("payload", {})
            item = payload.get("item", {})
            started = payload.get("started_at_ms")
            if (
                record.get("type") == "event_msg"
                and payload.get("type") in {"item_started", "item_completed"}
                and item.get("type") in {"Reasoning", "AgentMessage"}
                and isinstance(started, int)
                and started * 1_000_000 > parent_usage_end
            ):
                require(False, "Interrupted native usage has uncovered model output")
        outcomes, _, outcomes_partial = read_jsonl(path / "outcomes.jsonl")
        require(outcomes and not outcomes_partial, "Interrupted invocation outcome is incomplete")
        children, child_sessions = child_usage(
            session["native_session_id"],
            prior["created_at"],
            outcomes[-1]["at"],
            self.native_sessions_root(prior["binding"]),
        )
        require(children is not None, "Interrupted child usage is unknown")
        require(total == counters[-1] + children, "Interrupted native and telemetry usage differ")
        return {
            "output_tokens": counters[-1],
            "child_output_tokens": children,
            "child_sessions": child_sessions,
            "native_evidence_sha256": native_digest,
            "native_evidence_bytes": native_paths[0].stat().st_size,
            "telemetry_evidence_sha256": report["evidence_sha256"],
        }

    def _validate_interrupted_usage_receipt(self, path, prior, session, usage):
        """Validate the immutable evidence prefixes bound before a continuation launch."""
        from .telemetry import Sink

        telemetry_path = Path(json.loads((path / "telemetry.json").read_text())["path"])
        require(
            Sink(telemetry_path, {}).evidence_digest() == usage["telemetry_evidence_sha256"],
            "Interrupted provider telemetry changed after recovery preflight",
        )
        native_paths = list(
            self.native_sessions_root(prior["binding"]).glob(
                f"*/*/*/*{session['native_session_id']}.jsonl"
            )
        )
        require(len(native_paths) == 1, "Exact native session evidence is absent or ambiguous")
        length = usage.get("native_evidence_bytes")
        data = native_paths[0].read_bytes()
        require(
            type(length) is int
            and len(data) >= length
            and sha256(data[:length]).hexdigest() == usage["native_evidence_sha256"],
            "Interrupted native evidence changed after recovery preflight",
        )

    async def resume_provider_observation(self, operation_id, native_session_id):
        """Resume one stopped read-only provider observation in its exact native session."""
        require(
            isinstance(self.provider, AgentProvider),
            "Provider observation recovery needs agent interaction",
        )
        require(
            operation_id.startswith("provider-inventory:observe-") and operation_id.count(":") == 1,
            "Provider observation operation identity is invalid",
        )
        stage = operation_id.split(":", 1)[1]
        lock_path = self.root / "locks" / (component(operation_id) + ".lock")
        with operation_lock(lock_path):
            snapshot = load_config(self.config_path)
            require(
                snapshot.repository == self.config.repository
                and snapshot.operational_root == self.root,
                "Repository or evidence root changed during recovery",
            )
            operation_path = (
                self.root
                / "runs"
                / component("item:provider-inventory")
                / "operations"
                / component(operation_id)
            )
            originals = [
                value.parent
                for value in operation_path.glob("invocations/*/intent.json")
                if json.loads(value.read_text()).get("action") != "resume-provider-observation"
            ]
            require(len(originals) == 1, "Original provider observation is absent or ambiguous")
            path = originals[0]
            prior = EvidenceStore.reconcile(path)
            require(
                prior["operation_id"] == operation_id
                and prior["action"] == stage
                and prior["binding"]["role"] == "coordinator",
                "Provider observation identity or role differs",
            )
            require(
                prior["outcome"] == "unresolved" and not prior["partial"],
                "Provider observation is not an unresolved complete record",
            )
            require(
                self.process_stopped(path),
                "Provider observation process is still live or unknown",
            )
            context = json.loads((path / "execution-context.json").read_text())
            require(
                context.get("purpose") == "provider"
                and context.get("provider_operation") is None
                and context.get("read_only", True) is True,
                "Provider observation was not read-only",
            )
            session = json.loads((path / "session.json").read_text())
            require(
                session.get("native_session_id") == native_session_id
                and session.get("binding") == prior["binding"],
                "Provider observation native session identity differs",
            )
            observer_binding = snapshot.binding("coordinator")
            provider_data = plain(snapshot.data)
            provider_data["workspace"] = str(snapshot.repository)
            binding = replace(snapshot, data=freeze(provider_data)).binding("coordinator")
            require(asdict(binding) == prior["binding"], "Provider observation binding changed")
            revision = self.provider.source_revision()
            original_observer = digest([prior["config_digest"], observer_binding.relevant_digest])
            require(
                stage == "observe-" + digest([revision, original_observer]),
                "Provider source changed since the interrupted observation",
            )
            continuations = list(operation_path.glob("invocations/*/continuation.json"))
            require(len(continuations) <= 1, "Provider observation continuation is ambiguous")
            if continuations:
                retained = json.loads(continuations[0].read_text())
                require(
                    retained.get("original_invocation_id") == prior["invocation_id"]
                    and retained.get("native_session_id") == native_session_id
                    and retained.get("semantic_request_digest") == prior["request_digest"],
                    "Provider observation continuation receipt differs",
                )
                usage = retained["interrupted_usage"]
                self._validate_interrupted_usage_receipt(path, prior, session, usage)
            else:
                usage = self._interrupted_provider_usage(path, prior, session)
            limits = snapshot.data["coordinator_limits"]
            require(
                usage["output_tokens"] < limits["generated_tokens"],
                "Interrupted provider usage exhausted the call budget",
            )
            saved = self._stage_path("provider-inventory", stage)
            require(saved.exists(), "Interrupted provider stage receipt is missing")
            original_result = json.loads(saved.read_text())
            if original_result.get("continuation"):
                require(
                    original_result["continuation"].get("original_invocation_id")
                    == prior["invocation_id"]
                    and original_result.get("request_digest") == prior["request_digest"],
                    "Recovered provider stage receipt differs",
                )
            else:
                require(
                    original_result.get("invocation_id") == prior["invocation_id"]
                    and original_result.get("request_digest") == prior["request_digest"],
                    "Interrupted provider stage receipt differs",
                )
            continuation = {
                "version": 1,
                "original_invocation_id": prior["invocation_id"],
                "native_session_id": native_session_id,
                "semantic_request_digest": prior["request_digest"],
                "interrupted_usage": usage,
            }
            prompt = (
                "Continue the interrupted read-only provider observation in this exact native "
                "session. Do not mutate, dispatch, or perform claim operations. Finish the "
                "original request and return only its requested JSON object."
            )
            async with self.capacity_slot() as reservation:
                result = await self._invoke(
                    "provider-inventory",
                    stage,
                    "coordinator",
                    prompt,
                    session=SessionHandle(
                        session["session_id"], session["native_session_id"], binding
                    ),
                    read_only=True,
                    reservation=reservation,
                    purpose="provider",
                    operation_id=operation_id,
                    continuation=continuation,
                )
            self.validate_call_limits(result, limits)
            completed = [
                event["usage"]["output_tokens"]
                for event in result["events"]
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict)
            ]
            require(
                completed and completed[-1] >= usage["output_tokens"],
                "Resumed provider cumulative usage is invalid",
            )
            from .analytics import invocation_usage
            from .native_evidence import child_usage

            children, _ = child_usage(
                native_session_id,
                result["events"][0]["at"],
                result["events"][-1]["at"],
                self.native_sessions_root(result["binding"]),
            )
            require(
                children is not None
                and invocation_usage(result, usage["output_tokens"], child_outputs=children)
                is not None,
                "Resumed provider usage is unknown or conflicting",
            )
            observer = digest([snapshot.file_digest, observer_binding.relevant_digest])
            value = self._accept_provider_observation(result, revision, observer)
            return {
                "operation_id": operation_id,
                "original_invocation_id": prior["invocation_id"],
                "continuation_invocation_id": result["invocation_id"],
                "native_session_id": native_session_id,
                "source_revision": revision,
                "item_count": len(value["items"]),
                "policy": value["policy"],
                "output_tokens": completed[-1],
            }

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
                    project_mode = self.execution_policy(item_id)["mode"]
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

    def assignment_estimate(self, item):
        if item.original_high is not None:
            return item.original_high
        path = self._stage_path(item.item_id, "prospective-estimate")
        require(path.exists(), "Execution estimate is missing")
        value = json.loads(path.read_text())
        require(
            value["provider_revision"] == item.revision
            and value["receipt"].get("advancement_verified") is True
            and value["receipt"]["after"]["revision"] == item.revision,
            "Prospective estimate evidence is stale or unverified",
        )
        receipt = value["receipt"]
        record = json.loads(
            (
                self.root
                / "provider-agent-operations"
                / component(receipt["operation"])
                / "requested.json"
            ).read_text()
        )
        require(
            record.get("authority", {}).get("operation") == "record-estimate"
            and record["authority"].get("prospective_high") == value["prospective_high"]
            and receipt["after"] == asdict(item),
            "Prospective estimate binding differs",
        )
        require(receipt["operation"] == digest(record), "Prospective estimate operation differs")
        validate_transition(Item(**record["item"]), "Ready", record["authority"])
        decision = json.loads(self._stage_path(item.item_id, "estimate-decision").read_text())
        self.validate_invocation_result(decision)
        estimated = self.result_json(decision)
        require(
            decision.get("role") == "coordinator"
            and decision.get("invocation_id") == record["authority"].get("invocation_id")
            and estimated.get("item_id") == item.item_id
            and estimated.get("provider_revision") == record["item"]["revision"]
            and estimated.get("estimate") == record["authority"].get("estimate")
            and estimated.get("prospective_high") == value["prospective_high"],
            "Prospective estimate decision differs",
        )
        self.verify_provider_receipt(
            record,
            {
                "operation_id": record["stage_operation"],
                "before_revision": record["item"]["revision"],
                "commit": receipt["commit"],
                "after": receipt["after"],
            },
        )
        return value["prospective_high"]

    async def _run_item(self, item_id):
        self.reconcile()
        self.execution_policy(item_id)
        item = self.provider.item(item_id)
        if item.state == "Completed":
            return {"item_id": item_id, "state": item.state, "revision": item.revision}
        if isinstance(self.provider, AgentProvider):
            dependencies = self.provider.observation().get("dependencies", {})
            require(item_id in dependencies, "Selected item dependencies are unknown")
            require(
                all(
                    self.provider.item(name).state == "Completed" for name in dependencies[item_id]
                ),
                "Selected item dependencies are not completed",
            )
        require(
            not any(
                "result" not in json.loads(p.read_text())
                for p in self._stage_path(item_id, "unused").parent.glob("answer-operation-*.json")
            ),
            "A persisted answer is incomplete; use resume-answer before continuing",
        )
        from .estimation import prepare_item

        item = await prepare_item(self, item)
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
                    "original_high": self.assignment_estimate(item),
                    "historical_original_high": item.original_high,
                    "prospective_estimate_operation": (
                        json.loads(self._stage_path(item_id, "prospective-estimate").read_text())[
                            "receipt"
                        ]["operation"]
                        if item.original_high is None
                        else None
                    ),
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
                and frozen_assignment.get(
                    "historical_original_high", frozen_assignment.get("original_high")
                )
                == item.original_high,
                "Ready provider record differs from the frozen assignment; reconcile before admission",
            )
        workflow = self.item_workflow(item_id)
        candidate_repo = self.candidate_repository(item_id)
        require(
            candidate_repo != self.config.repository, "Candidate must be separate from provider"
        )
        require(frozen_assignment["original_high"] is not None, "Execution estimate is missing")
        if item.state == "Ready":
            async with async_operation_lock(self.root / "admission.lock"):
                if self.config.data["workflow"]["mode"] == "MULTITASK":
                    mine = set(workflow["allowed_paths"])
                    for other in self.provider.snapshot():
                        if other.item_id != item_id and other.state in {"Starting", "Running"}:
                            theirs = set(self.item_workflow(other.item_id)["allowed_paths"])
                            require(not mine & theirs, "Concurrent assignment scopes overlap")
                retained_admission = self._stage_path(item_id, "admit")
                if retained_admission.exists():
                    decision = json.loads(retained_admission.read_text())
                    self.validate_invocation_result(decision)
                    require(
                        decision.get("role") == "coordinator", "Retained admission actor differs"
                    )
                    prior = self.result_json(decision)
                    require(
                        prior.get("item_id") == item_id
                        and prior.get("provider_revision") == item.revision
                        and prior.get("operation") in {"new", "assess"},
                        "Retained admission identity differs",
                    )
                else:
                    decision = await self.invoke(
                        item_id,
                        "admit",
                        "coordinator",
                        "You are the Dev Backlog Coordinator. Decide whether to admit this user-authorized bounded "
                        "Work Item using its current provider record below. Do not implement, mutate files, or delegate. "
                        "Return only JSON with operation new or assess, item_id, provider_revision, and reason. "
                        + (
                            "This is a new native reservation for the SAME recovered item, not general new "
                            "backlog admission. Revalidate and apply this persisted Coordinator redispatch "
                            "authorization under the existing serial crisis reservation; global admission "
                            "remains closed for other items. Recovery authorization: "
                            + json.dumps(self.recovery_record(item_id))
                            + "\n"
                            if self.recovery_record(item_id)
                            else ""
                        )
                        + "Current authoritative dependency records override conditional historical queue prose:\n"
                        + json.dumps(self.admission_dependencies(item_id))
                        + "\n"
                        + f"Provider revision: {item.revision}\n\n{item.content}",
                    )
                self.validate_call_limits(decision, self.config.data["coordinator_limits"])
                value = self.result_json(decision)
                if value.get("operation") == "assess" and self.admission_dependencies(item_id):
                    decision = await self.invoke(
                        item_id,
                        "admit-confirmed",
                        "coordinator",
                        "Reassess only your retained admission decision against the current authoritative "
                        "dependency records below. Conditional historical queue prose is not a current "
                        "predecessor state. Verify accepted predecessor dispositions as well as status. "
                        "Do not mutate, implement, delegate, invoke claims, or launch anything. Return "
                        "the same JSON schema; operation new only when evidence warrants admission, "
                        "otherwise assess with the concrete remaining blocker.\n"
                        + json.dumps(
                            {
                                "item_id": item_id,
                                "provider_revision": item.revision,
                                "dependencies": self.admission_dependencies(item_id),
                            }
                        ),
                        session=self.session(decision),
                    )
                    self.validate_call_limits(decision, self.config.data["coordinator_limits"])
                    self.admission_path(item_id)
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
                self.admission_path(item_id).exists(),
                "Starting reservation has no retained admission evidence; reconcile before launch",
            )
            admission = json.loads(self.admission_path(item_id).read_text())
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
        recovery = self.recovery_record(item_id)
        if recovery:
            prompt = (
                "Running is recorded for your new native execution. Continue the preserved candidate; "
                "do not restart implementation or create a commit merely to satisfy the workflow. "
                "Agents own coordination and native delegation. Follow current claim-free crisis authority; "
                "do not invoke claims. Prior desktop execution is historical and must not resume. "
                "Finish only the remaining verification, review, approval and delivery gates bound in "
                "the recovery evidence and complete canonical item. Use browser tools only when "
                "those gates require them, preserving actual permission denials. Preserve historical "
                "semantic evidence and obtain fresh independent "
                "native review of the entire base..candidate scope and required acceptance checks. "
                "Use spawn_agent with fork_context=false (or fork_turns=none) for the reviewer; "
                "reviewer must return JSON {candidate,verdict:ACCEPT|REJECT,unresolved_findings:[]}. "
                "Wait for review. Do not mutate provider lifecycle. Return JSON "
                "{item_id,candidate,reviewer_session,request_completion:true}. If blocked, report exact "
                "evidence rather than claiming acceptance. Allowed source paths and commands: "
                + json.dumps(workflow)
                + "\nPreserved recovery evidence: "
                + json.dumps(recovery["packet"])
                + "\nWork item:\n"
                + assignment
            )
        if recovery and recovery["packet"].get("preserved_execution"):
            prompt += (
                "\nThe preserved candidate is immutable: no source production, corrections or new "
                "commit is authorized. Preserve prior attempts. Remaining work: "
                + recovery["packet"]["preserved_execution"]["remaining_work"]
                + " If an exact-candidate approval is required and absent, return item_id and "
                "candidate, reviewer_session, and question {question_id,text,candidate}, after completing "
                "the exact-candidate review and checks and identifying their evidence. "
                "Do not integrate or claim completion before approval."
            )
        continuation_path = self._stage_path(item_id, "continuation")
        stage = "produce-review"
        if not continuation_path.exists():
            from .recovery_flow import work_continuation

            followup = work_continuation(self, item_id, acceptance)
            if followup:
                stage = "continue-work-" + digest(followup)
                prompt += (
                    "\nContinue this same execution from its retained blocked result. "
                    "Preserve valid prior review and checks; do not repeat source production. "
                    "No source changes, candidate replacement, attempt reset, permission widening, "
                    "or bypass of required proof, usage limits, review, approval, or delivery gates. "
                    "Bounded continuation request: " + json.dumps(followup, sort_keys=True)
                )
        if not continuation_path.exists():
            from .recovery_flow import proof_continuation

            proof_followup = proof_continuation(self, item_id, acceptance)
            if proof_followup:
                stage = "continue-proof-" + digest(proof_followup["request"])
                prompt += (
                    "\nConsume this validated auxiliary proof in your original canonical session. "
                    "Reuse valid source review, checks and proof. Do not rerun production or proof. "
                    "The prior proof review is supporting evidence only. Arrange one fresh native child "
                    "with fork_turns=none (or fork_context=false) to review the retained proof package: "
                    "candidate binding, supporting hashes, Judge attribution, and captured versus "
                    "persisted contents. Do not repeat Judges, semantic proof or source review. "
                    "The reviewer must return JSON {candidate,verdict:ACCEPT|REJECT,unresolved_findings:[],"
                    "proof_result_digest:<exact digest from this handoff>}. Return its identity in "
                    "proof_reviewer_session alongside the retained source reviewer_session. "
                    "Finish only remaining required integration verification, then request exact-candidate "
                    "approval if absent. This handoff grants no new permissions or delivery authority. "
                    + json.dumps(proof_followup)
                )
        if continuation_path.exists():
            continuation = json.loads(continuation_path.read_text())
            stage = continuation["stage"]
            prompt += "\nPersisted canonical approval: " + json.dumps(continuation["approval"])
            proof_review_path = self._stage_path(item_id, "proof-review")
            if proof_review_path.exists():
                prompt += (
                    "\nReuse this retained fresh proof review; include its reviewer_task (or reviewer_session if absent) as "
                    "proof_reviewer_session in your response. Do not repeat proof or review: "
                    + proof_review_path.read_text()
                )
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
        approval_question = bool(
            recovery
            and recovery["packet"].get("preserved_execution", {}).get("candidate_approval_required")
            and value.get("question")
        )
        if value.get("question"):
            require(
                value.get("item_id") == item_id and isinstance(value["question"], dict),
                "Unbound question",
            )
            require(
                not git(candidate_repo, "status", "--porcelain")
                and git(candidate_repo, "rev-parse", "HEAD")
                == (recovery["packet"]["candidate"]["head"] if recovery else base),
                "Question boundary contains unapproved source work",
            )
            if not approval_question:
                current = self.provider.item(item_id)
                return await self.transition(
                    item_id,
                    current.revision,
                    "User Action Required",
                    self.authority(produced, item_id, question=value["question"]),
                    validate=validate_transition,
                )
        require(
            value.get("item_id") == item_id
            and (value.get("request_completion") is True or approval_question),
            "Canonical completion request missing"
            + (
                ": "
                + json.dumps({key: value[key] for key in ("blocker", "blockers") if value.get(key)})
                if value.get("blocker") or value.get("blockers")
                else ""
            ),
        )
        candidate = value.get("candidate")
        if self._stage_path(item_id, "proof-continuation").exists():
            from .recovery_flow import validated_auxiliary_proof

            proof_request, proof_result, _ = validated_auxiliary_proof(self, item_id, acceptance)
            require(proof_request["candidate"] == candidate, "Completion proof candidate differs")
            from .native_evidence import verify_native_review

            proof_review = verify_native_review(
                produced["session"]["native_session_id"],
                value.get("proof_reviewer_session"),
                candidate,
                self.native_sessions_root(produced["binding"]),
            )
            require(
                proof_review.get("proof_result_digest") == digest(proof_result),
                "Independent proof review is not bound to the retained proof result",
            )
            atomic_json(self._stage_path(item_id, "proof-review"), proof_review)
        if recovery and recovery["packet"].get("preserved_execution"):
            require(
                candidate == recovery["packet"]["candidate"]["head"],
                "Preserved candidate changed without authority",
            )
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
            preserved=recovery is not None,
        )
        if approval_question:
            require(
                value["question"]["candidate"] == candidate, "Approval question candidate differs"
            )
            current = self.provider.item(item_id)
            return await self.transition(
                item_id,
                current.revision,
                "User Action Required",
                self.authority(produced, item_id, question=value["question"]),
                validate=validate_transition,
            )
        if recovery and recovery["packet"].get("preserved_execution", {}).get(
            "candidate_approval_required"
        ):
            approved = (
                json.loads(continuation_path.read_text()).get("approval", {})
                if continuation_path.exists()
                else {}
            )
            require(
                approved.get("item_id") == item_id
                and approved.get("disposition") == "approve"
                and approved.get("question", {}).get("candidate") == candidate
                and approved.get("answer", {}).get("digest")
                == digest(approved.get("answer", {}).get("text")),
                "Exact preserved-candidate approval is required before delivery",
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
