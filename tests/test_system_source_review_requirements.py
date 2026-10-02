"""Installed source-review selection reuses one native review with prior check evidence."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, install_wheel


@pytest.fixture(scope="session")
def source_review_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def calls(root):
    return (root / "agent-calls.jsonl").read_bytes()


def configure_selection(harness, fault):
    config = yaml.safe_load(harness.config_path.read_text())
    config["workflow"]["preparation"] = {
        "allowed_roots": ["answer.txt"],
        "check_commands": config["workflow"]["checks"],
    }
    harness.config_path.write_text(yaml.safe_dump(config))
    gate = "Verify role-suite agreement and generated outputs without weakening evaluation"
    packet = {
        "allowed_paths": ["answer.txt"],
        "checks": config["workflow"]["checks"],
        "gates": [gate],
    }
    supplied = harness.config_path.parent / "preparation-response.json"
    supplied.write_text(json.dumps(packet))
    harness.env["PREPARATION_FIXTURE"] = str(supplied)
    harness.env["PREPARATION_CLASSIFICATIONS"] = json.dumps(
        [
            {
                "destination": "required_gates",
                "rationale": "Passing commands alone does not establish preserved evaluation strength.",
            }
        ]
    )
    blocked = harness.run("run-item", "item-one")
    assert blocked.returncode != 0 and "unsupported acceptance gates" in blocked.stderr
    preparation = next(harness.repo.rglob("preparation.json"))
    correction = next(harness.repo.rglob("preparation-contract-correction.json"))
    resolution = next(harness.repo.rglob("preparation-contract-resolution.json"))
    original_bytes = {path: path.read_bytes() for path in (preparation, correction, resolution)}
    saved = json.loads(preparation.read_text())
    corrected = json.loads(correction.read_text())
    linked = json.loads(resolution.read_text())
    contract = {
        "provider_revision": saved["item"]["revision"],
        "preparation_digest": digest(corrected),
        "original_preparation_digest": digest(saved["decision"]),
        "correction_resolution_digest": digest(linked),
        "requirements": [
            {
                "id": "role-suite",
                "canonical_reference": "item-one#acceptance",
                "acceptance_text": "Role, suite, and generated output agree without weakening",
                "required_gate": gate,
            }
        ],
    }
    config["workflow"]["items"] = {
        "item-one": {
            "allowed_paths": ["answer.txt"],
            "review_requirements": contract,
        }
    }
    harness.config_path.write_text(yaml.safe_dump(config))
    if fault:
        harness.env["SOURCE_REVIEW_FAULT"] = fault
    return original_bytes


def test_installed_bound_source_review_accepts_or_blocks_and_replays(
    source_review_python, tmp_path
):
    for fault in (None, "weakening"):
        root = tmp_path / (fault or "accepted")
        harness = InstalledHarness.create(
            root,
            source_review_python,
            dispatcher=Path(__file__).parent / "fixtures" / "preparation_agent.py",
        )
        original_bytes = configure_selection(harness, fault)
        result = harness.run("run-item", "item-one")
        if fault is None:
            assert result.returncode == 0, result.stdout + result.stderr
            review = json.loads(next(harness.repo.rglob("review.json")).read_text())
            assert review["source_review_assessment"]["unresolved_findings"] == []
            assert review["pre_review_checks"][0]["returncode"] == 0
            assert (harness.repo / "answer.txt").read_text() == "done\n"
        else:
            assert result.returncode != 0
            assert "Source review assessment is missing, stale or unresolved" in result.stderr
            assert not (harness.repo / "answer.txt").exists()
        before = calls(root)
        replay = harness.run("run-item", "item-one")
        assert replay.returncode == result.returncode, replay.stdout + replay.stderr
        assert calls(root) == before
        assert all(path.read_bytes() == content for path, content in original_bytes.items())
        producer_prompts = [
            row["prompt"]
            for row in map(json.loads, (root / "agent-calls.jsonl").read_text().splitlines())
            if "Running is now recorded for your exact session" in row["prompt"]
        ]
        assert len(producer_prompts) == 1
        assert "SOURCE REVIEW PACKET" in producer_prompts[0]
