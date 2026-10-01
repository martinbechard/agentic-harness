"""Semantic reuse never transfers native helper discovery across launch bindings."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from backlog_harness.contracts import digest, load_config
from backlog_harness.provider_observation import (
    MANAGEMENT_SKILLS,
    capability_fingerprint,
    effective_instruction_sources,
    migrate_legacy_fingerprint,
    semantic_observation_fingerprint,
    user_config_semantic_digest,
)


def fingerprint(snapshot, **changes):
    args = {
        "prompt": "Observe exact provider inventory without mutations",
        "schema": {"version": 1, "required": ["items", "policy"]},
        "instruction_sources": {"PROJECT.yaml": digest("project authority"), "AGENTS.md": None},
        "skill_sources": {},
    }
    args.update(changes)
    return semantic_observation_fingerprint(snapshot, **args)


def test_model_and_effort_changes_preserve_semantics_and_helper_capability(config_file):
    path, data = config_file
    first = load_config(path)
    semantic = fingerprint(first)
    original_binding = first.binding("coordinator")
    original_capability = capability_fingerprint(original_binding, semantic)
    data["profiles"]["control"].update(model="different", effort="high")
    path.write_text(yaml.safe_dump(data))
    changed = load_config(path)
    assert fingerprint(changed) == semantic
    assert capability_fingerprint(changed.binding("coordinator"), semantic) == original_capability
    # Executable/auth/tool-permission changes remain capability inputs.
    altered = replace(original_binding, executable_digest=digest("different binary"))
    assert capability_fingerprint(altered, semantic) != original_capability


@pytest.mark.parametrize("change", ["prompt", "schema", "instruction_sources"])
def test_interpretation_changes_invalidate_semantics(config_file, change):
    path, _ = config_file
    current = load_config(path)
    replacement = {
        "prompt": "Different authoritative classification instructions",
        "schema": {"version": 2, "required": ["items", "policy", "dependencies"]},
        "instruction_sources": {"PROJECT.yaml": digest("new policy"), "AGENTS.md": None},
    }
    assert fingerprint(current, **{change: replacement[change]}) != fingerprint(current)


def test_role_and_selected_skill_contents_invalidate(config_file):
    path, data = config_file
    original = load_config(path)
    data["profiles"]["control"]["role"] = "different coordinator role"
    path.write_text(yaml.safe_dump(data))
    assert fingerprint(load_config(path)) != fingerprint(original)
    # Pure helper accepts resolved bytes via digest, with no hidden filesystem reads.
    snapshot = replace(
        original,
        data={
            **original.data,
            "profiles": {
                **original.data["profiles"],
                "control": {**original.data["profiles"]["control"], "skills": ["selected"]},
            },
        },
    )
    first = fingerprint(snapshot, skill_sources={"selected": digest("skill v1")})
    assert fingerprint(snapshot, skill_sources={"selected": digest("skill v2")}) != first
    with pytest.raises(ValueError, match="exactly cover"):
        fingerprint(snapshot)


def test_referenced_management_sources_are_required_even_if_absent(config_file):
    path, _ = config_file
    original = load_config(path)
    agent = replace(original, data={**original.data, "provider_interaction": "agent"})
    with pytest.raises(ValueError, match="exactly cover"):
        fingerprint(agent)
    sources = {name: None for name in MANAGEMENT_SKILLS}
    before = fingerprint(agent, skill_sources=sources)
    sources["manage-work-items-file"] = digest("installed instructions")
    assert fingerprint(agent, skill_sources=sources) != before


def test_only_exact_current_old_fingerprint_migrates(config_file):
    path, _ = config_file
    current = load_config(path)
    binding = current.binding("coordinator")
    semantic = fingerprint(current)
    old = digest(
        [
            str(current.repository),
            current.data.get("methodology_root"),
            current.data.get("provider"),
            current.data.get("provider_interaction"),
            binding.relevant_digest,
        ]
    )
    older = digest([current.file_digest, binding.relevant_digest])
    for cached in (old, older):
        assert (
            migrate_legacy_fingerprint(
                cached, current, binding, semantic, policy_validated=True, source_validated=True
            )
            == semantic
        )
        assert (
            migrate_legacy_fingerprint(
                cached, current, binding, semantic, policy_validated=False, source_validated=True
            )
            is None
        )
        assert (
            migrate_legacy_fingerprint(
                cached, current, binding, semantic, policy_validated=True, source_validated=False
            )
            is None
        )
    stale = replace(binding, relevant_digest=digest("changed launch binding"))
    assert (
        migrate_legacy_fingerprint(
            old, current, stale, semantic, policy_validated=True, source_validated=True
        )
        is None
    )
    assert (
        migrate_legacy_fingerprint(
            "unrecognized", current, binding, semantic, policy_validated=True, source_validated=True
        )
        is None
    )


def test_semantic_reuse_never_reuses_capability_from_old_binding(config_file):
    path, _ = config_file
    current = load_config(path)
    binding = current.binding("coordinator")
    semantic = fingerprint(current)
    assert capability_fingerprint(binding, semantic) != semantic
    assert capability_fingerprint(binding, semantic) != capability_fingerprint(
        binding, digest("new instructions")
    )


def test_native_instruction_sources_include_workspace_home_and_semantic_user_config(
    config_file, tmp_path
):
    path, data = config_file
    home = tmp_path / "native-home"
    home.mkdir()
    (home / "AGENTS.md").write_text("Global provider rule\n")
    config = home / "config.toml"
    config.write_text('model = "first"\nmodel_reasoning_effort = "low"\ndeveloper_note = "one"\n')
    data["agent_clis"]["primary"]["adapter_options"] = {
        "codex_home": str(home),
        "load_user_config": True,
    }
    (Path(data["repository"]) / "AGENTS.md").write_text("Project provider rule\n")
    path.write_text(yaml.safe_dump(data))
    current = load_config(path)
    sources = effective_instruction_sources(current)
    assert sources["codex-home:AGENTS.md"]
    assert sources["workspace:" + str(Path(data["repository"]) / "AGENTS.md")]

    before = user_config_semantic_digest(config)
    config.write_text('model = "second"\nmodel_reasoning_effort = "high"\ndeveloper_note = "one"\n')
    assert user_config_semantic_digest(config) == before
    config.write_text('model = "second"\ndeveloper_note = "changed"\n')
    assert user_config_semantic_digest(config) != before
