"""Pure fingerprints for provider meaning versus native discovery capabilities.

The caller resolves source bytes and supplies their hashes. These helpers neither
read files nor update caches. Backlog revision/manifest validation remains a
separate provider check; it must not be replaced by semantic fingerprint equality.
"""

import re
import tomllib
from hashlib import sha256
from pathlib import Path

from .contracts import digest

MANAGEMENT_SKILLS = (
    "manage-work-items",
    "manage-work-items-file",
    "resource-claim",
    "resource-claim-helper",
    "resource-claim-helper-mcp",
)


LEGACY_PROVIDER_OBSERVATION_PROMPT = (
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
    "infer expiry. Policy evidence must be [{path,sha256,excerpt,supports:[mode/admission/coordination]}], "
    "citing exact current source bytes and the authority for both mode and admission. "
    "When an explicit active workflow exempts claims, also return claims_required:false "
    "and claim_exemption naming that authority, with coordination evidence. SOLO alone "
    "does not exempt claims. Otherwise omit these fields or return claims_required:true. "
    "Do not copy document contents: the harness reads the referenced bytes and computes "
    "revision hashes. Classify every existing backlog Markdown file "
    "exactly once as an item or non_item with a reason. Omit Future Ideas entirely. "
    "If resource claims are selected, report helper:{discovery:native_tool_catalog, "
    "available:boolean,tools:[exact_exposed_tool_names]} based on tools actually exposed "
    "in this invocation. Do not infer availability from configuration text and do not "
    "invoke any claim operation as a probe. Report unavailable when unproven."
)

# This exact predecessor identifies the one compatible cache contract that policy
# reassessment may upgrade after it replaces invalid authority. Other observation
# contract changes still require a complete provider refresh.
LEGACY_POLICY_VALIDATOR_DIGEST = (
    "49aa3d1eded81bc201330027f2211e626a5b9b8fb267559e8eae9b5037593730"
)

PROVIDER_OBSERVATION_PROMPT = LEGACY_PROVIDER_OBSERVATION_PROMPT + (
    " Harness operational outputs and cached projections, including run.json, report "
    "runtime state but never establish mode, admission, or coordination authority. "
    "Preserve an explicit pause or stop from canonical authority; do not infer one from "
    "a prior harness run or its admission projection."
)


PROVIDER_OBSERVATION_SCHEMA = {
    "version": "provider-observation-v1",
    "required": ["items", "policy", "dependencies"],
    "classification": "all-backlog-markdown-except-future-ideas-exactly-once",
    "dependencies": "observed-item-identities-or-explicitly-unknown",
    "policy": "structured-current-source-evidence",
    "items": "authoritative-bytes-hydrated-and-revision-bound",
}


def source_digest(path):
    """Return a stable source hash while representing absence explicitly."""
    path = Path(path)
    return (
        sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
    )


def user_config_semantic_digest(path):
    """Exclude known launch selection while conservatively retaining every other key."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return None
    raw = path.read_bytes()
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return digest({"unparsed_sha256": sha256(raw).hexdigest()})
    # These values are supplied explicitly for every harness launch and do not alter the
    # provider interpretation contract. Unknown keys remain in the digest by design.
    for key in ("model", "model_reasoning_effort"):
        value.pop(key, None)
    return digest(value)


def effective_instruction_sources(current):
    """Hash native instruction inputs visible from the provider invocation workspace."""
    binding = current.binding("coordinator")
    cli = current.data["agent_clis"][binding.cli_name]
    options = cli.get("adapter_options", {})
    repository = current.repository.resolve()
    sources = {
        "PROJECT.yaml": source_digest(repository / "PROJECT.yaml"),
        "adapter:disable_memories": digest(bool(options.get("disable_memories", True))),
        "adapter:load_user_config": digest(bool(options.get("load_user_config", False))),
    }
    for directory in reversed((repository, *repository.parents)):
        for name in ("AGENTS.md", "AGENTS.override.md"):
            path = directory / name
            sources["workspace:" + str(path)] = source_digest(path)
    codex_home = Path(binding.auth_context)
    for name in ("AGENTS.md", "AGENTS.override.md"):
        path = codex_home / name
        sources["codex-home:" + name] = source_digest(path)
    sources["codex-home:config-semantic"] = (
        user_config_semantic_digest(codex_home / "config.toml")
        if options.get("load_user_config", False)
        else None
    )
    return sources


def effective_skill_sources(current):
    """Hash selected prompt-injected skills and provider skills available by reference."""
    profile = current.data["profiles"][current.data["agents"]["coordinator"]["profile"]]
    names = set(profile.get("skills", ()))
    if current.data.get("provider_interaction", "agent") == "agent":
        names.update(MANAGEMENT_SKILLS)
    root = Path(current.data["methodology_root"]) / "skills"
    return {name: source_digest(root / name / "SKILL.md") for name in sorted(names)}


def helper_capability_sources(current):
    """Return helper-relevant CLI/auth/tool inputs without model or effort selection."""
    binding = current.binding("coordinator")
    cli = current.data["agent_clis"][binding.cli_name]
    options = cli.get("adapter_options", {})
    config_path = Path(binding.auth_context) / "config.toml"
    return {
        "origin": list(binding.origin),
        "permission_digest": binding.permission_digest,
        "load_user_config": bool(options.get("load_user_config", False)),
        "user_config_capability_digest": (
            user_config_semantic_digest(config_path)
            if options.get("load_user_config", False)
            else None
        ),
    }


def _sources(values, label):
    if not isinstance(values, dict) or any(
        not isinstance(name, str)
        or not name
        or (
            value is not None
            and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value))
        )
        for name, value in values.items()
    ):
        raise ValueError(label + " must map source names to SHA256 values or explicit absence")
    return dict(values)


def semantic_observation_fingerprint(
    current,
    *,
    prompt,
    schema,
    instruction_sources,
    skill_sources,
):
    """Hash exactly the interpretation contract, excluding next-launch settings.

    prompt/schema are the actual observation prompt and parser/schema contract.
    instruction_sources includes applicable PROJECT/AGENTS/role instructions and
    any other source interpretation dependencies resolved by the caller. Explicit
    None represents an absent optional source, so later appearance invalidates it.
    skill_sources must include selected skills and referenced management skills.
    Models, effort, executable and account/global config are intentionally absent.
    """
    if not isinstance(prompt, str) or not prompt.strip() or not schema:
        raise ValueError("Actual observation prompt and schema contract are required")
    agent = current.data["agents"]["coordinator"]
    profile = current.data["profiles"][agent["profile"]]
    selected = list(profile.get("skills", ()))
    referenced = (
        list(MANAGEMENT_SKILLS)
        if current.data.get("provider_interaction", "agent") == "agent"
        else []
    )
    skills = _sources(skill_sources, "skill_sources")
    required = set(selected) | set(referenced)
    if set(skills) != required or any(skills[name] is None for name in selected):
        raise ValueError("Skill evidence must exactly cover selected and referenced skills")
    return digest(
        {
            "version": "provider-observation-semantic-v1",
            "repository": str(current.repository),
            "methodology_root": current.data.get("methodology_root"),
            "provider": current.data.get("provider"),
            "provider_interaction": current.data.get("provider_interaction", "agent"),
            "role": profile["role"],
            "selected_skills": selected,
            "referenced_skills": referenced,
            "skill_sources": skills,
            "instruction_sources": _sources(instruction_sources, "instruction_sources"),
            "prompt": prompt,
            "schema": schema,
        }
    )


def capability_fingerprint(current_binding, semantic_fingerprint, capability_sources=None):
    """Discovery stays bound to the exact full binding, never just semantic reuse."""
    if not current_binding.relevant_digest or not semantic_fingerprint:
        raise ValueError("Current full binding and semantic fingerprint are required")
    sources = capability_sources or {
        "origin": list(current_binding.origin),
        "permission_digest": current_binding.permission_digest,
    }
    return digest(
        {
            "version": "provider-capability-v1",
            "binding": sources,
            "observation": semantic_fingerprint,
        }
    )


def migrate_legacy_fingerprint(
    cached_fingerprint,
    current,
    current_binding,
    semantic_fingerprint,
    *,
    policy_validated,
    source_validated,
):
    """Return a migration value only for an exactly current historical formula.

    Validation flags are assertions from the owning provider validator, not a
    substitute for validation. This cannot migrate a cache produced under an old
    binding after that binding changes. No capability receipt is migrated here.
    """
    if policy_validated is not True or source_validated is not True:
        return None
    old_full = digest(
        [
            str(current.repository),
            current.data.get("methodology_root"),
            current.data.get("provider"),
            current.data.get("provider_interaction"),
            current_binding.relevant_digest,
        ]
    )
    older_config = digest([current.file_digest, current_binding.relevant_digest])
    if cached_fingerprint not in {old_full, older_config}:
        return None
    if not semantic_fingerprint:
        raise ValueError("Semantic fingerprint is required")
    return semantic_fingerprint
