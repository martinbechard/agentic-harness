"""Installed preparation normalization preserves evidence without repeating agents."""

import json
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, install_wheel


@pytest.fixture(scope="session")
def preparation_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def calls(harness):
    path = harness.repo.parent / "agent-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("shape", ["supported", "dashboard", "portrait"])
def test_installed_preparation_metadata_replay(preparation_python, tmp_path, shape):
    harness = InstalledHarness.create(
        tmp_path,
        preparation_python,
        dispatcher=Path(__file__).parent / "fixtures/preparation_agent.py",
    )
    data = yaml.safe_load(harness.config_path.read_text())
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.txt"],
        "check_commands": data["workflow"]["checks"],
    }
    harness.config_path.write_text(yaml.safe_dump(data))
    packet = {
        "allowed_paths": ["answer.txt"],
        "checks": data["workflow"]["checks"],
        "persistence": "file",
        "completion": "main-branch",
        "canonical_primary_branch": "main",
    }
    if shape == "supported":
        packet.update(
            checks_executed=False,
            verification_limit="Preparation supplies no completed verification",
        )
    else:
        actual = json.loads(
            (Path(__file__).parent / "fixtures/preparation" / (shape + ".json")).read_text()
        )
        packet.update({key: value for key, value in actual.items() if key not in packet})
    supplied = tmp_path / "preparation-response.json"
    supplied.write_text(json.dumps(packet))
    harness.env["PREPARATION_FIXTURE"] = str(supplied)
    result = harness.run("run-item", "item-one")
    before_calls = calls(harness)
    preparations = list(tmp_path.rglob("preparation.json"))
    assert preparations, result.stdout + result.stderr
    preparation = preparations[0]
    retained = preparation.read_bytes()
    if shape == "supported":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (harness.repo / "answer.txt").read_text() == "done\n"
        assert (
            next((harness.repo / "backlog").rglob("item-one.md"))
            .read_text()
            .find("Status: Completed")
            >= 0
        )
    else:
        assert result.returncode != 0
        assert "unsupported acceptance gates before dispatch" in result.stdout + result.stderr
        assert json.dumps(packet["required_gates"]) in json.loads(result.stderr)["error"]
        assert not (harness.repo / "answer.txt").exists()
        assert "Status: Ready" in (harness.repo / "backlog/feature-backlog/item-one.md").read_text()
        # The default producer returns a generic ACCEPT review, but it must never run.
        assert len(before_calls) == 2  # inventory and preparation only
    replay = harness.run("run-item", "item-one")
    assert replay.returncode == result.returncode, replay.stdout + replay.stderr
    assert calls(harness) == before_calls
    assert preparation.read_bytes() == retained


@pytest.mark.parametrize("additional_proof", [False, True])
def test_installed_preparation_contract_correction_replays_once(
    preparation_python, tmp_path, additional_proof
):
    harness = InstalledHarness.create(
        tmp_path,
        preparation_python,
        dispatcher=Path(__file__).parent / "fixtures/preparation_agent.py",
    )
    data = yaml.safe_load(harness.config_path.read_text())
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.txt"],
        "check_commands": data["workflow"]["checks"],
    }
    harness.config_path.write_text(yaml.safe_dump(data))
    gates = ["Reserve and accept the item before implementation."]
    classifications = [
        {
            "destination": "implementation_constraints",
            "runtime_obligation": "reservation_acceptance",
            "rationale": "The normal admission transitions already enforce this obligation.",
        }
    ]
    if additional_proof:
        gates.append("Capture fresh browser evidence before delivery.")
        classifications.append(
            {
                "destination": "required_gates",
                "rationale": "The configured workflow has no browser-proof selection.",
            }
        )
    packet = {
        "allowed_paths": ["answer.txt"],
        "checks": data["workflow"]["checks"],
        "persistence": "file",
        "completion": "main-branch",
        "canonical_primary_branch": "main",
        "gates": gates,
    }
    supplied = tmp_path / "preparation-response.json"
    supplied.write_text(json.dumps(packet))
    harness.env.update(
        PREPARATION_FIXTURE=str(supplied),
        PREPARATION_CLASSIFICATIONS=json.dumps(classifications),
    )
    result = harness.run("run-item", "item-one")
    before_calls = calls(harness)
    correction_calls = [
        call
        for call in before_calls
        if "Correct one retained preparation response representation" in call["prompt"]
    ]
    assert len(correction_calls) == 1, result.stdout + result.stderr
    preparation = next(tmp_path.rglob("preparation.json"))
    retained = preparation.read_bytes()
    resolution = next(tmp_path.rglob("preparation-contract-resolution.json"))
    correction = next(tmp_path.rglob("preparation-contract-correction.json"))
    linked = json.loads(resolution.read_text())
    assert linked["original_invocation_id"] != linked["corrected_invocation_id"]
    corrected = json.loads(correction.read_text())
    metadata = json.loads(corrected["text"])["workflow"]
    assert metadata["implementation_constraints"] == gates[:1]
    if additional_proof:
        assert result.returncode != 0
        assert "unsupported acceptance gates before dispatch" in result.stdout + result.stderr
        assert metadata["required_gates"] == gates[1:]
        assert not (harness.repo / "answer.txt").exists()
        assert "Status: Ready" in (harness.repo / "backlog/feature-backlog/item-one.md").read_text()
        assert len(before_calls) == 3  # inventory, preparation and its one correction
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (harness.repo / "answer.txt").read_text() == "done\n"
        assert (
            "Status: Completed" in next((harness.repo / "backlog").rglob("item-one.md")).read_text()
        )
    replay = harness.run("run-item", "item-one")
    assert replay.returncode == result.returncode, replay.stdout + replay.stderr
    assert calls(harness) == before_calls
    assert preparation.read_bytes() == retained
