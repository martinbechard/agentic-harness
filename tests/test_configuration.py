import pytest
import yaml

from backlog_harness.adapters.registry import AdapterRegistry
from backlog_harness.contracts import ConfigError, load_config, plain


def test_reload_is_deeply_immutable(config_file):
    path, data = config_file
    first = load_config(path)
    with pytest.raises(TypeError):
        first.data["profiles"]["worker"]["model"] = "changed"
    data["profiles"]["worker"]["model"] = "next"
    path.write_text(yaml.safe_dump(data))
    second = load_config(path)
    assert first.data["profiles"]["worker"]["model"] == "test"
    assert second.data["profiles"]["worker"]["model"] == "next"
    assert first.file_digest != second.file_digest
    assert first.binding("orchestrator").origin == second.binding("orchestrator").origin


@pytest.mark.parametrize(
    "key,value",
    [("poll_seconds", float("nan")), ("max_active_invocations", True), ("provider", "unknown")],
)
def test_invalid_current_configuration(config_file, key, value):
    path, data = config_file
    data[key] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_keys_and_unsupported_adapter(config_file):
    path, _data = config_file
    path.write_text(path.read_text() + "version: 1\n")
    with pytest.raises(ConfigError):
        load_config(path)
    with pytest.raises(ValueError):
        AdapterRegistry().resolve("arbitrary.module")


def test_skill_change_invalidates_binding(config_file):
    path, data = config_file
    root = __import__("pathlib").Path(data["methodology_root"])
    skill = root / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("first")
    data["profiles"]["worker"]["skills"] = ["example"]
    path.write_text(yaml.safe_dump(data))
    first = load_config(path).binding("orchestrator")
    skill.write_text("second")
    assert load_config(path).binding("orchestrator").relevant_digest != first.relevant_digest


@pytest.mark.parametrize("field", ["workspace", "workflow", "agents"])
def test_missing_runnable_route_rejected(config_file, field):
    path, data = config_file
    del data[field]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("argv", [[], [""], [4], "echo test"])
def test_bad_check_rejected_before_effect(config_file, argv):
    path, data = config_file
    data["workflow"]["checks"] = [argv]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "source",
    ["../escape", "/absolute", ".git/config", "backlog/item.md", "PROJECT.yaml", "./answer.py"],
)
def test_protected_source_path_rejected(config_file, source):
    path, data = config_file
    data["workflow"]["allowed_paths"] = [source]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)


def test_writable_parent_of_provider_rejected(config_file):
    path, data = config_file
    data["workspace"] = str(path.parent)
    (path.parent / ".git").mkdir()
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="disjoint"):
        load_config(path)


def test_unapplied_tool_control_rejected(config_file):
    path, data = config_file
    data["profiles"]["worker"]["tools"] = ["invented-tool"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="unsupported"):
        load_config(path)


def test_effective_authentication_context_is_bound_to_session(config_file, monkeypatch):
    path, data = config_file
    first_home = path.parent / "first-home"
    second_home = path.parent / "second-home"
    first_home.mkdir()
    second_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(first_home))
    first = load_config(path).binding("orchestrator")
    monkeypatch.setenv("CODEX_HOME", str(second_home))
    second = load_config(path).binding("orchestrator")
    assert first.auth_context == str(first_home)
    assert first.origin != second.origin
    assert first.relevant_digest != second.relevant_digest
    data["agent_clis"]["primary"]["adapter_options"] = {"codex_home": str(first_home)}
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).binding("orchestrator").origin == first.origin


def test_control_storage_remains_inspectable_when_generation_profile_is_missing(
    config_file, provider
):
    from backlog_harness.application import Application
    from backlog_harness.projections import snapshot

    path, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    del data["profiles"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)
    app = Application(path, control_only=True)
    value = snapshot(app)
    assert value["generation_configuration_error"]
    assert value["counts"] == {"Ready": 1}
    assert value["items"][0]["usage"]["may_generate"] is False
    assert not value["invocations"]


def test_accepted_workflow_remains_frozen_and_changed_scope_blocks_invocation(config_file):
    import asyncio

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import TransitionBlocked

    path, data = config_file
    app = Application(path)
    workflow = app.item_workflow("one")
    atomic_json(app._stage_path("one", "assignment"), {"workflow": workflow})
    data["workflow"]["allowed_paths"] = ["another.py"]
    path.write_text(yaml.safe_dump(data))
    app.config = load_config(path)
    assert app.item_workflow("one") == workflow
    with pytest.raises(TransitionBlocked, match="Accepted workflow changed"):
        asyncio.run(app.invoke("one", "accept", "orchestrator", "prompt"))
    assert not list((app.root / "runs").glob("*/operations/*/invocations/*/intent.json"))


def test_agent_management_permissions_and_user_config_reload(config_file, tmp_path):
    path, data = config_file
    home = tmp_path / "native-home"
    home.mkdir()
    native_config = home / "config.toml"
    native_config.write_text('[mcp_servers.claims]\ncommand="claim-helper"\n')
    data["provider_interaction"] = "agent"
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    data["agent_clis"]["primary"]["adapter_options"] = {
        "codex_home": str(home),
        "load_user_config": True,
    }
    path.write_text(yaml.safe_dump(data))
    first = load_config(path).binding("coordinator")
    native_config.write_text('[mcp_servers.claims]\ncommand="updated-helper"\n')
    second = load_config(path).binding("coordinator")
    assert first.relevant_digest != second.relevant_digest
    assert first.auth_context == str(home.resolve())
    data["provider_interaction"] = "direct"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="control roles"):
        load_config(path)


def test_provider_defaults_to_agent_without_silent_fixture_fallback(config_file):
    from backlog_harness.application import Application
    from backlog_harness.provider import AgentProvider

    path, data = config_file
    data.pop("provider_interaction")
    path.write_text(yaml.safe_dump(data))
    assert isinstance(Application(path).provider, AgentProvider)


@pytest.mark.parametrize("value", [0, -1, True, "900", float("inf"), float("nan")])
def test_invalid_invocation_timeout(config_file, value):
    path, data = config_file
    data["invocation_timeout_seconds"] = value
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="invocation_timeout_seconds"):
        load_config(path)


@pytest.mark.parametrize("override", ["preparation", "mode", "completion"])
def test_item_workflow_cannot_override_preparation_or_gates(config_file, override):
    path, data = config_file
    data["workflow"]["items"] = {
        "one": {"allowed_paths": ["answer.py"], override: {"allowed_roots": ["escape"]}}
    }
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="only override"):
        load_config(path)


@pytest.mark.parametrize(
    "fault",
    [None, "partial-lineage", "duplicate-id", "duplicate-gate", "missing-gate", "extra-field"],
)
def test_explicit_source_review_requirement_contract(config_file, fault):
    path, data = config_file
    requirement = {
        "id": "role-suite",
        "canonical_reference": "item-one#acceptance",
        "acceptance_text": "Role and suite agree without weakening evaluation",
        "required_gate": "Verify role-suite agreement",
    }
    contract = {
        "provider_revision": "revision",
        "preparation_digest": "1" * 64,
        "original_preparation_digest": "2" * 64,
        "correction_resolution_digest": "3" * 64,
        "requirements": [requirement],
    }
    if fault == "partial-lineage":
        contract.pop("correction_resolution_digest")
    elif fault == "duplicate-id":
        contract["requirements"].append(
            {**requirement, "required_gate": "Verify generated outputs"}
        )
    elif fault == "duplicate-gate":
        contract["requirements"].append({**requirement, "id": "generated"})
    elif fault == "missing-gate":
        contract["requirements"][0].pop("required_gate")
    elif fault == "extra-field":
        contract["route"] = "invented"
    data["workflow"]["items"] = {
        "one": {"allowed_paths": ["answer.py"], "review_requirements": contract}
    }
    path.write_text(yaml.safe_dump(data))
    if fault:
        with pytest.raises(ConfigError):
            load_config(path)
    else:
        assert load_config(path).data["workflow"]["items"]["one"]["review_requirements"]


def test_source_review_contract_preserves_mixed_structured_gate_values(config_file):
    path, data = config_file
    structured = {
        "gate": "Deterministic generated-report boundary",
        "requirement": "Prove the boundary with reports present and absent.",
    }
    requirements = [
        {
            "id": "generated-report",
            "canonical_reference": "item-one#generated-report",
            "acceptance_text": "Generated reports do not change the maintained-source result",
            "required_gate": structured,
        },
        {
            "id": "source-rejection",
            "canonical_reference": "item-one#source-rejection",
            "acceptance_text": "Unexpected retired names remain rejected",
            "required_gate": "Maintained-source rejection",
        },
    ]
    contract = {
        "provider_revision": "revision",
        "preparation_digest": "1" * 64,
        "requirements": requirements,
    }
    data["workflow"]["items"] = {
        "one": {"allowed_paths": ["answer.py"], "review_requirements": contract}
    }
    path.write_text(yaml.safe_dump(data))
    loaded = load_config(path)
    assert plain(loaded.data["workflow"]["items"]["one"]["review_requirements"]) == contract


def test_source_review_contract_rejects_duplicate_structured_gate_regardless_of_key_order(
    config_file,
):
    path, data = config_file
    first = {
        "gate": "Deterministic generated-report boundary",
        "requirement": "Prove reports present and absent.",
    }
    reversed_order = {
        "requirement": "Prove reports present and absent.",
        "gate": "Deterministic generated-report boundary",
    }
    requirements = [
        {
            "id": "first",
            "canonical_reference": "item-one#first",
            "acceptance_text": "First requirement",
            "required_gate": first,
        },
        {
            "id": "second",
            "canonical_reference": "item-one#second",
            "acceptance_text": "Second requirement",
            "required_gate": reversed_order,
        },
    ]
    data["workflow"]["items"] = {
        "one": {
            "allowed_paths": ["answer.py"],
            "review_requirements": {
                "provider_revision": "revision",
                "preparation_digest": "1" * 64,
                "requirements": requirements,
            },
        }
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(ConfigError, match="required gates must be unique"):
        load_config(path)


@pytest.mark.parametrize(
    "gate",
    [
        {},
        {"gate": "Named gate"},
        {"requirement": "Named requirement"},
        {"gate": "", "requirement": "Named requirement"},
        {"gate": "Named gate", "requirement": 1},
        {"gate": "Named gate", "requirement": "Named requirement", "unknown": "value"},
        ["Named gate", "Named requirement"],
    ],
)
def test_source_review_contract_rejects_malformed_structured_gate(config_file, gate):
    path, data = config_file
    data["workflow"]["items"] = {
        "one": {
            "allowed_paths": ["answer.py"],
            "review_requirements": {
                "provider_revision": "revision",
                "preparation_digest": "1" * 64,
                "requirements": [
                    {
                        "id": "gate",
                        "canonical_reference": "item-one#gate",
                        "acceptance_text": "Review the exact gate",
                        "required_gate": gate,
                    }
                ],
            },
        }
    }
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="required_gate"):
        load_config(path)


@pytest.mark.parametrize("setting", ["true", 1, None])
def test_artifact_output_requires_explicit_boolean(config_file, setting):
    path, data = config_file
    data["profiles"]["worker"]["artifact_output"] = setting
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="artifact_output"):
        load_config(path)


def test_disabled_artifact_permission_preserves_existing_digest(config_file):
    path, data = config_file
    before = load_config(path).binding("orchestrator")
    data["profiles"]["worker"]["artifact_output"] = False
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).binding("orchestrator").permission_digest == before.permission_digest
    data["profiles"]["worker"]["artifact_output"] = True
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).binding("orchestrator").permission_digest != before.permission_digest


@pytest.mark.parametrize("target", ["project-one", "project-two"])
def test_default_evidence_belongs_to_target_not_launch_directory(
    config_file, tmp_path, monkeypatch, target
):
    from backlog_harness.contracts import load_control_config

    config, data = config_file
    repository = tmp_path / target
    repository.mkdir()
    launch = tmp_path / "unrelated-launch"
    launch.mkdir()
    data["repository"] = str(repository)
    config.write_text(yaml.safe_dump(data))
    monkeypatch.chdir(launch)
    expected = repository / ".agent-ops/backlog-harness"
    assert load_config(config).operational_root == expected
    del data["profiles"]
    config.write_text(yaml.safe_dump(data))
    assert load_control_config(config).operational_root == expected


def test_explicit_evidence_override_is_preserved(config_file, tmp_path):
    config, data = config_file
    override = tmp_path / "explicit-evidence"
    data["operational_root"] = str(override)
    config.write_text(yaml.safe_dump(data))
    assert load_config(config).operational_root == override
