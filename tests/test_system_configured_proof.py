"""Installed workflow requires candidate-bound native visual proof acceptance."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness


@pytest.fixture(scope="session")
def configured_proof_python(installed_package_python):
    return installed_package_python


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def calls(root):
    return (root / "agent-calls.jsonl").read_bytes()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "invalid",
        "generic",
        "verification-valid",
        "verification-missing",
        "verification-reject",
        "verification-tamper",
        "verification-overlap",
    ],
)
def test_installed_configured_proof_completion_and_replay(configured_proof_python, tmp_path, fault):
    harness = InstalledHarness.create(
        tmp_path,
        configured_proof_python,
        dispatcher=Path(__file__).parent / "fixtures/configured_proof_agent.py",
    )
    config = yaml.safe_load(harness.config_path.read_text())
    config["profiles"]["worker"]["artifact_output"] = True
    config["workflow"]["preparation"] = {
        "allowed_roots": ["answer.txt"],
        "check_commands": config["workflow"]["checks"],
    }
    harness.config_path.write_text(yaml.safe_dump(config))
    packet = {
        "allowed_paths": ["answer.txt"],
        "checks": config["workflow"]["checks"],
        "required_gates": ["Fresh print capture and independent review"],
    }
    supplied = tmp_path / "preparation-response.json"
    supplied.write_text(json.dumps(packet))
    harness.env["PREPARATION_FIXTURE"] = str(supplied)
    first = harness.run("run-item", "item-one")
    assert first.returncode != 0 and "unsupported acceptance gates" in first.stderr
    assert "Status: Ready" in (harness.repo / "backlog/feature-backlog/item-one.md").read_text()
    assert not (harness.candidate / "answer.txt").exists()
    assert not list(tmp_path.rglob("assignment.json"))
    preparation = next(tmp_path.rglob("preparation.json"))
    original = preparation.read_bytes()
    saved = json.loads(original)
    config["workflow"]["items"] = {
        "item-one": {
            "allowed_paths": ["answer.txt"],
            "proof_requirements": {
                "provider_revision": saved["item"]["revision"],
                "preparation_digest": digest(saved["decision"]),
                "requirements": [
                    {
                        "id": "letter",
                        "canonical_reference": "item-one#acceptance",
                        "evidence_kind": "print",
                        "acceptance_text": "Columns fit Letter",
                    }
                ],
            },
        }
    }
    if fault and fault.startswith("verification-"):
        config["workflow"]["items"]["item-one"]["proof_requirements"]["verification_required"] = (
            True
        )
    harness.config_path.write_text(yaml.safe_dump(config))
    if fault:
        harness.env["CONFIGURED_PROOF_FAULT"] = fault
    result = harness.run("run-item", "item-one")
    if fault in (None, "verification-valid"):
        assert result.returncode == 0, result.stdout + result.stderr
        assert (harness.repo / "answer.txt").read_text() == "done\n"
        assert (
            "Status: Completed" in next((harness.repo / "backlog").rglob("item-one.md")).read_text()
        )
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not (harness.repo / "answer.txt").exists()
        assert (
            "Status: Completed"
            not in next((harness.repo / "backlog").rglob("item-one.md")).read_text()
        )
        expected = {
            "missing": "requirement",
            "invalid": "hash differs",
            "generic": "Independent proof review binding differs",
            "verification-missing": "Independent acceptance verification",
            "verification-reject": "Independent acceptance verification",
            "verification-tamper": "receipts changed",
            "verification-overlap": "Independent proof review binding differs",
        }
        assert expected[fault] in result.stderr, result.stdout + result.stderr
    before_calls = calls(tmp_path)
    names = [
        "intent.json",
        "requested.json",
        "native-request.json",
        "configured-proof*.json",
        "design-attempt*.json",
        "design-acceptance.json",
    ]
    if fault and fault.startswith("verification-"):
        names += ["source-checks.json", "source-checks-execution.json", "review.json"]
    receipts = {p: p.read_bytes() for name in names for p in tmp_path.rglob(name)}
    assert receipts
    replay = harness.run("run-item", "item-one")
    assert replay.returncode == result.returncode, replay.stdout + replay.stderr
    assert calls(tmp_path) == before_calls
    assert all(p.read_bytes() == value for p, value in receipts.items())
    assert preparation.read_bytes() == original
