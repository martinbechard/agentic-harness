"""Immutable per-invocation configuration; validation never contacts a model."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import yaml


class ConfigError(ValueError):
    """Current configuration cannot authorize the requested operation."""


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigError("Configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def digest(value) -> str:
    return sha256(
        json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _object(value, label):
    if not isinstance(value, dict) or not value:
        raise ConfigError(f"{label} must be a nonempty mapping")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a nonempty string")
    return value


def _path(value, label, *, directory=True):
    path = Path(_text(value, label))
    if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
        raise ConfigError(
            f"{label} must name an existing absolute {'directory' if directory else 'file'}"
        )
    return path.resolve()


def validate_workspace(workspace, repository, operational_root):
    """Writable candidate trees must not contain provider or harness evidence."""
    workspace, repository, operational_root = map(Path, (workspace, repository, operational_root))
    workspace, repository, operational_root = (
        workspace.resolve(),
        repository.resolve(),
        operational_root.resolve(),
    )
    if (
        workspace.is_relative_to(repository)
        or repository.is_relative_to(workspace)
        or operational_root.is_relative_to(workspace)
        or workspace.is_relative_to(operational_root)
    ):
        raise ConfigError("Workspace must be disjoint from provider and operational evidence")
    if (workspace / ".git").is_symlink() or not (workspace / ".git").is_dir():
        raise ConfigError("Workspace must be an independent candidate Git checkout")


def safe_source_path(value):
    _text(value, "allowed path")
    path = Path(value)
    if (
        path.is_absolute()
        or any(p in {".", "..", ".git", ".codex", "backlog"} for p in path.parts)
        or path.as_posix() != value
        or value == "PROJECT.yaml"
    ):
        raise ConfigError("Allowed paths must be normalized relative implementation files")
    return value


@dataclass(frozen=True)
class AgentBinding:
    role: str
    cli_name: str
    adapter: str
    executable: str
    executable_digest: str
    auth_profile: str
    profile_name: str
    profile_digest: str
    relevant_digest: str
    auth_context: str = ""
    permission_digest: str = ""

    @property
    def origin(self):
        return (
            self.adapter,
            self.cli_name,
            self.executable,
            self.executable_digest,
            self.auth_profile,
            self.auth_context,
        )


def resume_binding_compatible(previous, current):
    """Allow launch tuning to change while retaining session origin and permissions."""
    return previous.origin == current.origin and (
        previous.permission_digest == current.permission_digest
        if previous.permission_digest
        else previous.profile_digest == current.profile_digest
    )


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    file_digest: str
    loaded_at: str
    data: Mapping

    @property
    def repository(self):
        return Path(self.data["repository"])

    @property
    def operational_root(self):
        return Path(
            self.data.get("operational_root", str(self.repository / ".agent-ops/backlog-harness"))
        )

    def binding(self, role: str) -> AgentBinding:
        try:
            agent = self.data["agents"][role]
            cli = self.data["agent_clis"][agent["cli"]]
            profile = self.data["profiles"][agent["profile"]]
        except KeyError as exc:
            raise ConfigError(f"No explicit binding for role {role}") from exc
        executable = _path(cli["executable"], "executable", directory=False)
        root = Path(self.data["methodology_root"])
        skills = {}
        for name in profile.get("skills", ()):
            if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                raise ConfigError("Skill names must be single path components")
            path = root / "skills" / name / "SKILL.md"
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise ConfigError(f"Configured methodology skill is missing: {name}")
            skills[name] = sha256(path.read_bytes()).hexdigest()
        executable_digest = sha256(executable.read_bytes()).hexdigest()
        management_skills = {}
        if self.data.get("provider_interaction", "agent") == "agent" and role in {
            "coordinator",
            "orchestrator",
        }:
            for name in (
                "manage-work-items",
                "manage-work-items-file",
                "resource-claim",
                "resource-claim-helper",
                "resource-claim-helper-mcp",
            ):
                path = root / "skills" / name / "SKILL.md"
                management_skills[name] = (
                    sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                )
        auth_context = str(
            Path(
                cli.get("adapter_options", {}).get(
                    "codex_home", os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
                )
            ).resolve()
        )
        relevant = digest(
            {
                "agent": agent,
                "cli": cli,
                "profile": profile,
                "skills": skills,
                "management_skills": management_skills,
                "executable": executable_digest,
                "auth_context": auth_context,
                "user_config_digest": (
                    sha256((Path(auth_context) / "config.toml").read_bytes()).hexdigest()
                    if cli.get("adapter_options", {}).get("load_user_config", False)
                    else None
                ),
                "repository": self.data["repository"],
                "workspace": self.data["workspace"],
                "methodology_root": self.data["methodology_root"],
            }
        )
        return AgentBinding(
            role,
            agent["cli"],
            cli["adapter"],
            str(executable),
            executable_digest,
            cli["auth_profile"],
            agent["profile"],
            digest({"profile": profile, "skills": skills}),
            relevant,
            auth_context,
            digest(
                {
                    "role": profile["role"],
                    "load_user_config": cli.get("adapter_options", {}).get(
                        "load_user_config", False
                    ),
                    "permissions": profile["permissions"],
                    "tools": profile["tools"],
                    **({"artifact_output": True} if profile.get("artifact_output", False) else {}),
                }
            ),
        )


def load_config(path: Path, *, adapters=frozenset({"codex"})) -> ConfigSnapshot:
    path = path.resolve()
    try:
        first, second = path.read_bytes(), path.read_bytes()
        if first != second:
            raise ConfigError("Configuration changed while reading; no invocation launched")
        data = yaml.load(first, Loader=UniqueLoader)
        _object(data, "configuration")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ConfigError("Unsupported configuration version")
        _path(data.get("repository"), "repository")
        _path(data.get("methodology_root"), "methodology_root")
        workspace = _path(data.get("workspace"), "workspace")
        if data.get("provider") != "file":
            raise ConfigError("Only the file provider is implemented")
        if data.get("provider_interaction", "agent") not in {"direct", "agent"}:
            raise ConfigError("provider_interaction must be direct or agent")
        if (
            "operational_root" in data
            and not Path(_text(data["operational_root"], "operational_root")).is_absolute()
        ):
            raise ConfigError("operational_root must be absolute")
        operational_root = Path(
            data.get(
                "operational_root", str(Path(data["repository"]) / ".agent-ops/backlog-harness")
            )
        )
        validate_workspace(workspace, data["repository"], operational_root)
        workflow = _object(data.get("workflow"), "workflow")
        if (
            workflow.get("completion") != "main-branch"
            or workflow.get("mode") not in {"SOLO", "MULTITASK"}
            or workflow.get("primary_branch") not in {"main", "master"}
        ):
            raise ConfigError(
                "Workflow must explicitly select a supported execution mode and main-branch route"
            )
        excluded = data.get("excluded_items", {})
        if not isinstance(excluded, dict):
            raise ConfigError("excluded_items must be a mapping")
        for item_id, reason in excluded.items():
            _text(item_id, "excluded item identity")
            _text(reason, "excluded item reason")
        allowed = workflow.get("allowed_paths")
        if not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed):
            raise ConfigError("workflow.allowed_paths must be a nonempty unique list")
        for value in allowed:
            safe_source_path(value)
            source = workspace / value
            if not source.resolve().is_relative_to(workspace) or source.is_symlink():
                raise ConfigError("Allowed path escapes candidate workspace")
        checks = workflow.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ConfigError("workflow.checks must be a nonempty list")
        for argv in checks:
            if not isinstance(argv, list) or not argv:
                raise ConfigError("Each required check must be a nonempty argument list")
            for arg in argv:
                _text(arg, "check argument")
        if "candidate_root" in data:
            candidate_root = _path(data["candidate_root"], "candidate_root")
            if (
                candidate_root.is_relative_to(Path(data["repository"]).resolve())
                or Path(data["repository"]).resolve().is_relative_to(candidate_root)
                or operational_root.resolve().is_relative_to(candidate_root)
                or candidate_root.is_relative_to(operational_root.resolve())
            ):
                raise ConfigError("candidate_root must be disjoint from provider and evidence")
        preparation = workflow.get("preparation")
        if preparation is not None:
            _object(preparation, "workflow.preparation")
            if set(preparation) != {"allowed_roots", "check_commands"}:
                raise ConfigError("Preparation only configures source roots and check commands")
            roots = preparation["allowed_roots"]
            if not isinstance(roots, list) or not roots:
                raise ConfigError("Preparation needs source roots")
            for root in roots:
                safe_source_path(root)
            commands = preparation["check_commands"]
            if not isinstance(commands, list) or not commands:
                raise ConfigError("Preparation needs an explicit check command catalog")
            for argv in commands:
                if not isinstance(argv, list) or not argv:
                    raise ConfigError("Preparation check must be a nonempty argv")
                for arg in argv:
                    _text(arg, "preparation check argument")
        items = workflow.get("items", {})
        if not isinstance(items, dict):
            raise ConfigError("workflow.items must be a mapping")
        if workflow["mode"] == "MULTITASK" and (not items or "candidate_root" not in data):
            raise ConfigError("MULTITASK requires isolated candidate_root and explicit item scopes")
        for item_id, selected in items.items():
            _text(item_id, "item id")
            _object(selected, "item workflow")
            if set(selected) - {
                "allowed_paths",
                "checks",
                "engine",
                "proof_requirements",
                "review_requirements",
                "design_review",
                "candidate_approval_required",
            }:
                raise ConfigError(
                    "Item workflow may only override allowed_paths, checks, engine, proof_requirements, review_requirements, design_review and candidate_approval_required"
                )
            if (
                "candidate_approval_required" in selected
                and type(selected["candidate_approval_required"]) is not bool
            ):
                raise ConfigError("candidate_approval_required must be boolean")
            if "design_review" in selected:
                design = _object(selected["design_review"], "design_review")
                if set(design) != {
                    "provider_revision",
                    "preparation_digest",
                    "canonical_reference",
                    "acceptance_text",
                }:
                    raise ConfigError(
                        "Design review needs exact preparation binding and acceptance"
                    )
                for field, value in design.items():
                    _text(value, "design_review." + field)
            if "proof_requirements" in selected:
                proof = _object(selected["proof_requirements"], "proof_requirements")
                if set(proof) - {
                    "provider_revision",
                    "preparation_digest",
                    "requirements",
                    "verification_required",
                } or not {"provider_revision", "preparation_digest", "requirements"} <= set(proof):
                    raise ConfigError(
                        "Proof requirements need exact preparation binding and requirements"
                    )
                if (
                    "verification_required" in proof
                    and type(proof["verification_required"]) is not bool
                ):
                    raise ConfigError("verification_required must be boolean")
                for field in ("provider_revision", "preparation_digest"):
                    _text(proof.get(field), "proof_requirements." + field)
                requirements = proof["requirements"]
                if not isinstance(requirements, list) or not requirements:
                    raise ConfigError("Proof requirements must be nonempty")
                identifiers = set()
                for requirement in requirements:
                    if not isinstance(requirement, dict) or set(requirement) != {
                        "id",
                        "canonical_reference",
                        "evidence_kind",
                        "acceptance_text",
                    }:
                        raise ConfigError("Invalid explicit proof requirement")
                    for field in ("id", "canonical_reference", "acceptance_text"):
                        _text(requirement[field], "proof requirement " + field)
                    if requirement["id"] in identifiers or requirement["evidence_kind"] not in {
                        "browser",
                        "print",
                    }:
                        raise ConfigError(
                            "Proof requirement id must be unique and evidence_kind browser or print"
                        )
                    identifiers.add(requirement["id"])
            if "review_requirements" in selected:
                review = _object(selected["review_requirements"], "review_requirements")
                required = {
                    "provider_revision",
                    "preparation_digest",
                    "requirements",
                }
                correction = {
                    "original_preparation_digest",
                    "correction_resolution_digest",
                }
                if set(review) - required - correction or not required <= set(review):
                    raise ConfigError(
                        "Review requirements need exact preparation binding and requirements"
                    )
                present_correction = set(review) & correction
                if present_correction and present_correction != correction:
                    raise ConfigError(
                        "Corrected review requirements need the complete preparation lineage"
                    )
                for field in required - {"requirements"}:
                    _text(review.get(field), "review_requirements." + field)
                for field in correction:
                    if field in review:
                        _text(review[field], "review_requirements." + field)
                requirements = review["requirements"]
                if not isinstance(requirements, list) or not requirements:
                    raise ConfigError("Review requirements must be nonempty")
                identifiers, gates = set(), set()
                for requirement in requirements:
                    if not isinstance(requirement, dict) or set(requirement) != {
                        "id",
                        "canonical_reference",
                        "acceptance_text",
                        "required_gate",
                    }:
                        raise ConfigError("Invalid explicit review requirement")
                    for field, value in requirement.items():
                        _text(value, "review requirement " + field)
                    if requirement["id"] in identifiers or requirement["required_gate"] in gates:
                        raise ConfigError(
                            "Review requirement ids and required gates must be unique"
                        )
                    identifiers.add(requirement["id"])
                    gates.add(requirement["required_gate"])
            if selected.get("engine", "legacy") not in {"legacy", "langgraph"}:
                raise ConfigError("Item workflow engine must be legacy or langgraph")
            if not isinstance(selected.get("allowed_paths"), list) or not selected["allowed_paths"]:
                raise ConfigError("Item scope needs allowed_paths")
            for value in selected["allowed_paths"]:
                safe_source_path(value)
            if "checks" in selected:
                if not isinstance(selected["checks"], list) or not selected["checks"]:
                    raise ConfigError("Item checks must be nonempty")
                for argv in selected["checks"]:
                    if not isinstance(argv, list) or not argv:
                        raise ConfigError("Item check argv must be nonempty")
                    for arg in argv:
                        _text(arg, "item check argument")
        for name in ("poll_seconds", "runtime_observation_stale_seconds"):
            value = data.get(name)
            if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{name} must be positive and finite")
        timeout = data.get("invocation_timeout_seconds", 180)
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("invocation_timeout_seconds must be positive and finite")
        capacity = data.get("max_active_invocations")
        if type(capacity) is not int or not 1 <= capacity <= 10:
            raise ConfigError("max_active_invocations must be an integer from 1 to 10")
        multiplier = data.get("generation_guard_multiplier", 2.0)
        if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or multiplier <= 0:
            raise ConfigError("generation_guard_multiplier must be positive and finite")
        if multiplier != 2.0:
            approval = _object(data.get("generation_guard_approval"), "generation_guard_approval")
            if approval.get("value") != multiplier:
                raise ConfigError("Multiplier must match its exact approved value")
            _text(approval.get("reference"), "generation_guard_approval.reference")
        for name in ("coordinator_limits", "administrative_review_limits"):
            limits = _object(data.get(name), name)
            for key in ("turns", "generated_tokens"):
                if type(limits.get(key)) is not int or limits[key] <= 0:
                    raise ConfigError(f"{name}.{key} must be a positive integer")
        clis = _object(data.get("agent_clis"), "agent_clis")
        profiles = _object(data.get("profiles"), "profiles")
        agents = _object(data.get("agents"), "agents")
        for cli in clis.values():
            _object(cli, "CLI binding")
            if cli.get("adapter") not in adapters:
                raise ConfigError(f"Unsupported adapter: {cli.get('adapter')}")
            _path(cli.get("executable"), "executable", directory=False)
            _text(cli.get("auth_profile"), "auth_profile")
            if not isinstance(cli.get("adapter_options", {}), dict):
                raise ConfigError("adapter_options must be a mapping")
            options = cli.get("adapter_options", {})
            if set(options) - {
                "native_max_threads",
                "codex_home",
                "load_user_config",
                "disable_memories",
            }:
                raise ConfigError("Unsupported Codex adapter option")
            if type(options.get("disable_memories", True)) is not bool:
                raise ConfigError("disable_memories must be a boolean")
            if type(options.get("load_user_config", False)) is not bool:
                raise ConfigError("load_user_config must be a boolean")
            if (
                type(options.get("native_max_threads", 2)) is not int
                or not 1 <= options.get("native_max_threads", 2) <= 10
            ):
                raise ConfigError("native_max_threads must be an integer from 1 to 10")
            if "codex_home" in options:
                _path(options["codex_home"], "codex_home")
            elif cli["auth_profile"] != "chatgpt":
                raise ConfigError("Named authentication contexts require explicit codex_home")
        for profile in profiles.values():
            _object(profile, "profile")
            for key in ("role", "model", "effort"):
                _text(profile.get(key), f"profile.{key}")
            for key in ("skills", "tools", "permissions"):
                if not isinstance(profile.get(key), list) or any(
                    not isinstance(v, str) or not v for v in profile[key]
                ):
                    raise ConfigError(f"profile.{key} must be a list of strings")
            if type(profile.get("artifact_output", False)) is not bool:
                raise ConfigError("profile.artifact_output must be a boolean")
            if profile.get("artifact_output") and profile["permissions"] != ["workspace-write"]:
                raise ConfigError("Artifact output requires workspace-write permission")
            if profile["tools"] != ["native"]:
                raise ConfigError(
                    "Codex supports the native tool set; arbitrary tool filtering is unsupported"
                )
            if profile["permissions"] not in (["read"], ["workspace-write"]):
                raise ConfigError("Codex profile permissions must select read or workspace-write")
        if not {"coordinator", "orchestrator"} <= set(agents):
            raise ConfigError("Coordinator and Orchestrator bindings are required")
        for role, agent in agents.items():
            _object(agent, "agent")
            if agent.get("cli") not in clis or agent.get("profile") not in profiles:
                raise ConfigError("Agent references unknown CLI or profile")
            management_role = (
                role == "coordinator" and data.get("provider_interaction", "agent") == "agent"
            )
            if (
                role != "orchestrator"
                and not management_role
                and profiles[agent["profile"]]["permissions"] != ["read"]
            ):
                raise ConfigError("Harness control roles require read permissions")
        snapshot = ConfigSnapshot(path, sha256(first).hexdigest(), utcnow(), freeze(data))
        for role in agents:
            snapshot.binding(role)
        return snapshot
    except (OSError, yaml.YAMLError, TypeError) as exc:
        raise ConfigError(f"Cannot load configuration: {exc}") from exc


def load_control_config(path: Path) -> ConfigSnapshot:
    """Resolve current storage for inspection/control without authorizing generation."""
    try:
        return load_config(path)
    except ConfigError as exc:
        error = str(exc)
    path = path.resolve()
    try:
        first, second = path.read_bytes(), path.read_bytes()
        if first != second:
            raise ConfigError("Control configuration changed while reading")
        data = yaml.load(first, Loader=UniqueLoader)
        _object(data, "configuration")
        if data.get("version") != 1 or data.get("provider") != "file":
            raise ConfigError("Control storage identity is unsupported")
        _path(data.get("repository"), "repository")
        root = Path(
            data.get(
                "operational_root", str(Path(data["repository"]) / ".agent-ops/backlog-harness")
            )
        )
        if not root.is_absolute():
            raise ConfigError("Control evidence root must be absolute")
        data["operational_root"] = str(root.resolve())
        # These defaults affect display only. Every generation boundary uses load_config.
        data["runtime_observation_stale_seconds"] = 30
        data["generation_configuration_error"] = error
        return ConfigSnapshot(path, sha256(first).hexdigest(), utcnow(), freeze(data))
    except (OSError, yaml.YAMLError, TypeError) as exc:
        raise ConfigError(f"Cannot resolve control storage: {exc}") from exc
