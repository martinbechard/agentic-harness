"""Installed CLI scenarios: actual validators, provider commits and durable graph state."""

import json
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, install_wheel


@pytest.fixture(scope="session")
def graph_wheel(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def graph_harness(tmp_path, graph_wheel, scenario):
    harness = InstalledHarness.create(
        tmp_path,
        graph_wheel,
        scenario=scenario,
        dispatcher=Path(__file__).parent / "fixtures" / "graph_agent.py",
    )
    data = yaml.safe_load(harness.config_path.read_text())
    data["workflow"]["items"] = {
        "item-one": {
            "engine": "langgraph",
            "allowed_paths": data["workflow"]["allowed_paths"],
            "checks": data["workflow"]["checks"],
        }
    }
    harness.config_path.write_text(yaml.safe_dump(data))
    return harness


def successful_json(process):
    assert process.returncode == 0, process.stderr + process.stdout
    return json.loads(process.stdout)


def native_invocations(harness):
    root = harness.repo / ".agent-ops" / "backlog-harness"
    return sorted(root.glob("runs/*/operations/*/invocations/*/intent.json"))


def assert_complete(harness, result):
    assert result["result"]["state"] == "Completed"
    assert result["delivery"]["verified"] is True
    assert result["delivery"]["candidate"] == result["candidate"]
    assert (harness.repo / "answer.txt").read_text() == "done\n"
    root = harness.repo / ".agent-ops" / "backlog-harness"
    assert list((root / "item-graphs").glob("*/checkpoints.sqlite"))
    assert not list(root.rglob("continuation.json"))
    assert not list(root.rglob("answer-operation-*.json"))
    assert result["review"]["native_verified"] is True
    assert result["review"]["fresh_context"] is True
    assert result["review"]["reviewer_session"] != result["review"]["producer_session"]


def test_installed_graph_completes_real_provider_workflow(tmp_path, graph_wheel):
    harness = graph_harness(tmp_path, graph_wheel, "graph")
    result = successful_json(harness.run("run-item", "item-one"))
    assert_complete(harness, result)
    before = native_invocations(harness)
    repeated = successful_json(harness.run("run-item", "item-one"))
    assert_complete(harness, repeated)
    assert native_invocations(harness) == before


def test_installed_graph_question_restart_answer(tmp_path, graph_wheel):
    harness = graph_harness(tmp_path, graph_wheel, "graph-question")
    waiting = successful_json(harness.run("run-item", "item-one"))
    question = waiting["__interrupt__"][0]
    before = native_invocations(harness)
    evidence_root = harness.repo / ".agent-ops" / "backlog-harness"
    acceptance_paths = list(evidence_root.rglob("accept.json"))
    assert len(acceptance_paths) == 1
    original_session = json.loads(acceptance_paths[0].read_text())["session"]
    assert not (harness.candidate / "answer.txt").exists()
    assert successful_json(harness.run("run-item", "item-one"))["__interrupt__"]
    assert native_invocations(harness) == before
    bad = harness.run(
        "answer",
        "item-one",
        "--question-id",
        "wrong",
        "--revision",
        question["revision"],
        "--text",
        "English",
    )
    assert bad.returncode != 0
    assert native_invocations(harness) == before
    configuration = yaml.safe_load(harness.config_path.read_text())
    configuration["workflow"]["items"]["item-one"]["engine"] = "legacy"
    harness.config_path.write_text(yaml.safe_dump(configuration))
    switched = harness.run("run-item", "item-one")
    assert switched.returncode != 0
    assert native_invocations(harness) == before
    configuration["workflow"]["items"]["item-one"]["engine"] = "langgraph"
    harness.config_path.write_text(yaml.safe_dump(configuration))
    complete = successful_json(
        harness.run(
            "answer",
            "item-one",
            "--question-id",
            question["question"]["question_id"],
            "--revision",
            question["revision"],
            "--text",
            "English",
        )
    )
    assert_complete(harness, complete)
    assert complete["acceptance"]["session"] == original_session
    assert (
        complete["produced"]["session"]["native_session_id"]
        == original_session["native_session_id"]
    )


def agent_calls(harness):
    return [
        json.loads(line)
        for line in (harness.repo.parent / "agent-calls.jsonl").read_text().splitlines()
    ]


def assessment_calls(harness):
    return [
        call
        for call in agent_calls(harness)
        if "Assess this generation guard incident" in call["prompt"]
    ]


def set_original_estimate(harness, estimate):
    from system_support import command

    item = harness.repo / "backlog/feature-backlog/item-one.md"
    item.write_text(
        item.read_text().replace(
            "Original High Generated Tokens: 1000", f"Original High Generated Tokens: {estimate}"
        )
    )
    for arguments in (("add", "--", str(item)), ("commit", "-m", "Set pre-execution estimate")):
        result = command(["git", "-C", harness.repo, *arguments])
        assert result.returncode == 0, result.stderr


def test_installed_graph_overrun_reestimates_without_reset(tmp_path, graph_wheel):
    harness = graph_harness(tmp_path, graph_wheel, "graph-hold-resume")
    set_original_estimate(harness, 1)
    complete = successful_json(harness.run("run-item", "item-one"))
    assert_complete(harness, complete)
    assert complete["assignment"]["original_high"] == 1
    assert complete["hold_decision"]["assessment"]["remaining_high"] == 20
    assert complete["hold_decision"]["assessment"]["cause"]
    assert complete["hold_decision"]["assessment"]["revised_approach"]
    assert complete["hold_decision"]["allowance"]["original_high"] == 1
    assert len(assessment_calls(harness)) == 1
    assert (
        len(
            [
                call
                for call in agent_calls(harness)
                if "Implement only the assigned candidate paths" in call["prompt"]
            ]
        )
        == 1
    )
    prior = native_invocations(harness)
    successful_json(harness.run("run-item", "item-one"))
    assert native_invocations(harness) == prior


@pytest.mark.parametrize(
    "scenario,estimate,disposition",
    [
        ("graph-hold-rethink", 1, "rethink"),
        ("graph-unknown", 1000, "escalate"),
        ("graph-unknown-resume", 1000, "resume"),
    ],
)
def test_installed_public_hold_review_preserves_wait(
    tmp_path, graph_wheel, scenario, estimate, disposition
):
    harness = graph_harness(tmp_path, graph_wheel, scenario)
    if estimate != 1000:
        set_original_estimate(harness, estimate)
    waiting = successful_json(harness.run("run-item", "item-one"))
    held = waiting["__interrupt__"][0]
    assert held["kind"] == "hold"
    assert held["decision"]["assessment"]["operation"] == disposition
    assert held["decision"]["original_high"] == estimate
    assert held["decision"]["release_permitted"] is False
    documents = list((harness.repo / "backlog").rglob("item-one.md"))
    assert len(documents) == 1
    assert "Status: Holding" in documents[0].read_text().splitlines()
    assert f"Original High Generated Tokens: {estimate}" in documents[0].read_text().splitlines()
    root = harness.repo / ".agent-ops" / "backlog-harness"
    assert not list(root.rglob("allowance.json"))
    before = native_invocations(harness)
    repeated = successful_json(harness.run("review-hold", "item-one"))
    assert repeated["__interrupt__"][0] == held
    assert native_invocations(harness) == before
    assert len(assessment_calls(harness)) == 1
    assert not (harness.repo / "answer.txt").exists()
    if scenario.startswith("graph-unknown"):
        assert not (harness.candidate / "answer.txt").exists()
        assert not any(
            "Implement only the assigned candidate paths" in call["prompt"]
            for call in agent_calls(harness)
        )
        assert held["decision"]["usage"]["generated_tokens"] is None
