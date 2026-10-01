"""Installed observation caching separates interpretation from launch selection."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, install_wheel


@pytest.fixture(scope="session")
def observation_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def calls(harness):
    path = harness.repo.parent / "agent-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_model_effort_reuses_inventory_but_instruction_sources_refresh_once(
    observation_python, tmp_path
):
    harness = InstalledHarness.create(tmp_path, observation_python)

    first = harness.run("refresh-provider")
    assert first.returncode == 0, first.stdout + first.stderr
    assert len(calls(harness)) == 1

    config = yaml.safe_load(harness.config_path.read_text())
    config["profiles"]["control"].update(model="next-model", effort="high")
    harness.config_path.write_text(yaml.safe_dump(config))
    cached = harness.run("refresh-provider")
    assert cached.returncode == 0, cached.stdout + cached.stderr
    assert len(calls(harness)) == 1

    skill = Path(config["methodology_root"]) / "skills/manage-work-items/SKILL.md"
    skill.write_text(skill.read_text() + "Interpret the current inventory exactly.\n")
    refreshed = harness.run("refresh-provider")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    observed = calls(harness)
    assert len(observed) == 2
    assert "next-model" in observed[-1]["argv"]
    assert 'model_reasoning_effort="high"' in observed[-1]["argv"]

    (harness.repo / "AGENTS.md").write_text("Provider observations must classify every item.\n")
    refreshed = harness.run("refresh-provider")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert len(calls(harness)) == 3

    unchanged = harness.run("refresh-provider")
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert len(calls(harness)) == 3


def test_user_config_launch_selection_reuses_cache_but_tool_configuration_invalidates(
    observation_python, tmp_path
):
    harness = InstalledHarness.create(tmp_path, observation_python)
    config = yaml.safe_load(harness.config_path.read_text())
    config["agent_clis"]["primary"]["adapter_options"]["load_user_config"] = True
    harness.config_path.write_text(yaml.safe_dump(config))
    native_config = harness.native_home / "config.toml"
    native_config.write_text('model = "first"\nmodel_reasoning_effort = "low"\n')

    first = harness.run("refresh-provider")
    assert first.returncode == 0, first.stdout + first.stderr
    assert len(calls(harness)) == 1

    native_config.write_text('model = "second"\nmodel_reasoning_effort = "high"\n')
    cached = harness.run("refresh-provider")
    assert cached.returncode == 0, cached.stdout + cached.stderr
    assert len(calls(harness)) == 1

    native_config.write_text(
        'model = "second"\nmodel_reasoning_effort = "high"\n'
        '[mcp_servers.claim_helper]\ncommand = "helper"\n'
    )
    refreshed = harness.run("refresh-provider")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert len(calls(harness)) == 2

    unchanged = harness.run("refresh-provider")
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert len(calls(harness)) == 2


def question_revision(harness):
    item = harness.repo / "backlog/user-action-required/item-one.md"
    relative = str(item.relative_to(harness.repo))
    return sha256(relative.encode() + b"\0" + item.read_bytes()).hexdigest()


def test_wait_resume_uses_current_launch_and_refreshed_instruction_sources(
    observation_python, tmp_path
):
    harness = InstalledHarness.create(tmp_path, observation_python, scenario="legacy-question")
    initial = harness.run("run-item", "item-one")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    original = calls(harness)
    owner = next(row for row in original if "Running is now recorded" in row["prompt"])
    assert "resume" in owner["argv"]
    native_session = owner["argv"][owner["argv"].index("resume") + 1]

    config = yaml.safe_load(harness.config_path.read_text())
    config["profiles"]["control"].update(model="next-model", effort="high")
    config["profiles"]["worker"].update(model="next-model", effort="high")
    harness.config_path.write_text(yaml.safe_dump(config))
    cached = harness.run("refresh-provider")
    assert cached.returncode == 0, cached.stdout + cached.stderr
    assert len(calls(harness)) == len(original)

    skill = Path(config["methodology_root"]) / "skills/manage-work-items/SKILL.md"
    skill.write_text(skill.read_text() + "Retain the exact pending question intent.\n")
    refreshed = harness.run("refresh-provider")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert len(calls(harness)) == len(original) + 1

    answered = harness.run(
        "answer",
        "item-one",
        "--question-id",
        "language",
        "--revision",
        question_revision(harness),
        "--text",
        "English",
    )
    assert answered.returncode == 0, answered.stdout + answered.stderr
    classify = [
        row for row in calls(harness) if "Classify the exact operator answer" in row["prompt"]
    ]
    assert len(classify) == 1
    assert "next-model" in classify[0]["argv"]
    assert 'model_reasoning_effort="high"' in classify[0]["argv"]
    assert "Retain the exact pending question intent." in classify[0]["prompt"]
    assert "resume" in classify[0]["argv"]
    assert classify[0]["argv"][classify[0]["argv"].index("resume") + 1] == native_session


def test_incompatible_wait_resume_blocks_without_replacement_and_retains_answer_intent(
    observation_python, tmp_path
):
    harness = InstalledHarness.create(tmp_path, observation_python, scenario="legacy-question")
    initial = harness.run("run-item", "item-one")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    revision = question_revision(harness)
    before = calls(harness)

    config = yaml.safe_load(harness.config_path.read_text())
    config["profiles"]["worker"]["permissions"] = ["read"]
    harness.config_path.write_text(yaml.safe_dump(config))
    blocked = harness.run(
        "answer",
        "item-one",
        "--question-id",
        "language",
        "--revision",
        revision,
        "--text",
        "English",
    )
    assert blocked.returncode == 2
    assert "Resume permission changes require explicit authorization evidence" in blocked.stderr
    assert not [
        row
        for row in calls(harness)[len(before) :]
        if "Classify the exact operator answer" in row["prompt"]
    ]
    pending = list(
        (harness.repo / ".agent-ops/backlog-harness/workflow-evidence").rglob(
            "answer-operation-*.json"
        )
    )
    assert len(pending) == 1 and "result" not in json.loads(pending[0].read_text())

    config["profiles"]["worker"]["permissions"] = ["workspace-write"]
    harness.config_path.write_text(yaml.safe_dump(config))
    resumed = harness.run("resume-answer", "item-one")
    assert resumed.returncode == 2
    assert "Unsubmitted intent binding changed" in resumed.stderr
    assert not [
        row for row in calls(harness) if "Classify the exact operator answer" in row["prompt"]
    ]
    assert "result" not in json.loads(pending[0].read_text())
