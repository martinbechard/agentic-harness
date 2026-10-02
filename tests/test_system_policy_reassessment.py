"""Installed CLI repairs retained inventory policy without repeating observation."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from system_support import InstalledHarness


@pytest.fixture(scope="session")
def policy_reassessment_python(installed_package_python):
    return installed_package_python


def files(path):
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def calls(harness):
    path = harness.repo.parent / "agent-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def invocation_evidence(envelope):
    return Path(json.loads(envelope.read_text())["evidence_path"])


def test_public_reassessment_repairs_retained_inventory_once(
    policy_reassessment_python, tmp_path
):
    harness = InstalledHarness.create(
        tmp_path,
        policy_reassessment_python,
        dispatcher=Path(__file__).parent / "fixtures/policy_agent.py",
    )
    cache = harness.repo / ".agent-ops/backlog-harness/provider-observation.json"

    observed = harness.run("refresh-provider")
    assert observed.returncode == 2
    assert "Policy evidence excerpt is absent" in observed.stderr
    assert not cache.exists()
    envelopes = list(
        (harness.repo / ".agent-ops/backlog-harness/workflow-evidence").rglob(
            "observe-*.json"
        )
    )
    assert len(envelopes) == 1
    envelope = envelopes[0]
    observation_evidence = invocation_evidence(envelope)
    retained_evidence = files(observation_evidence)
    assert len(calls(harness)) == 1

    repaired = harness.run("reassess-policy", "--observation", envelope)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    accepted = json.loads(cache.read_text())
    item = accepted["items"][0]
    content = (harness.repo / item["path"]).read_bytes()
    assert item["content"].encode() == content
    assert item["revision"] == sha256(item["path"].encode() + b"\0" + content).hexdigest()
    assert item["state"] == "READY"
    assert accepted["policy"]["eligible"] is True
    assert accepted["invocation_id"] == json.loads(envelope.read_text())["invocation_id"]
    assert files(observation_evidence) == retained_evidence
    assert len(calls(harness)) == 2

    accepted_bytes = cache.read_bytes()
    status = harness.run("status")
    assert status.returncode == 0, status.stdout + status.stderr
    visible = json.loads(status.stdout)
    assert visible["items"][0]["state"] == "Ready"
    assert len(calls(harness)) == 2
    assert cache.read_bytes() == accepted_bytes

    policy_envelopes = list(
        (harness.repo / ".agent-ops/backlog-harness/workflow-evidence").rglob(
            "reassess-*.json"
        )
    )
    assert len(policy_envelopes) == 1
    policy_evidence = invocation_evidence(policy_envelopes[0])
    retained_policy_evidence = files(policy_evidence)

    replayed = harness.run("reassess-policy", "--observation", envelope)
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    assert len(calls(harness)) == 2
    assert cache.read_bytes() == accepted_bytes
    assert files(observation_evidence) == retained_evidence
    assert files(policy_evidence) == retained_policy_evidence
