"""Installed preimplementation design gate enforces native independent acceptance."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness
from test_system_configured_proof import configured_proof_python  # noqa: F401


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("fault", [None, "reject-first", "missing", "self", "wrong-digest"])
def test_installed_design_gate_before_producer(configured_proof_python, tmp_path, fault):  # noqa: F811
    harness = InstalledHarness.create(
        tmp_path,
        configured_proof_python,
        dispatcher=Path(__file__).parent / "fixtures/design_review_agent.py",
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
        "required_gates": ["Independent design acceptance before implementation"],
    }
    supplied = tmp_path / "preparation-response.json"
    supplied.write_text(json.dumps(packet))
    harness.env["PREPARATION_FIXTURE"] = str(supplied)
    first = harness.run("run-item", "item-one")
    assert first.returncode != 0 and "unsupported acceptance gates" in first.stderr
    preparation = next(tmp_path.rglob("preparation.json"))
    original = preparation.read_bytes()
    saved = json.loads(original)
    config["workflow"]["items"] = {
        "item-one": {
            "allowed_paths": ["answer.txt"],
            "design_review": {
                "provider_revision": saved["item"]["revision"],
                "preparation_digest": digest(saved["decision"]),
                "canonical_reference": "item-one#design",
                "acceptance_text": "Review content design before changing source",
            },
        }
    }
    harness.config_path.write_text(yaml.safe_dump(config))
    if fault:
        harness.env["DESIGN_REVIEW_FAULT"] = fault
    result = harness.run("run-item", "item-one")
    calls_path = tmp_path / "agent-calls.jsonl"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    producers = [
        i
        for i, call in enumerate(calls)
        if "Running is now recorded for your exact session" in call["prompt"]
    ]
    attempts = [
        json.loads(line) for line in (tmp_path / "design-attempts.jsonl").read_text().splitlines()
    ]
    assert len(attempts) == (2 if fault == "reject-first" else 1)
    for attempt in attempts:
        permission = next(
            arg for arg in attempt["argv"] if arg.startswith("permissions.harness.filesystem=")
        )
        artifact_dir = str(Path(attempt["artifact"]["path"]).parent)
        assert (
            permission
            == 'permissions.harness.filesystem={":root"="read",'
            + json.dumps(artifact_dir)
            + '="write"}'
        )
    if fault in (None, "reject-first"):
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(producers) == 1
        accepted = json.loads(next(tmp_path.rglob("design-acceptance.json")).read_text())
        assert (
            json.JSONDecoder().raw_decode(
                calls[producers[0]]["prompt"].split("Accepted design: ", 1)[1]
            )[0]
            == accepted
        )
        assert accepted["design"] == attempts[-1]["artifact"]
        if fault == "reject-first":
            assert attempts[0]["artifact"]["sha256"] != attempts[1]["artifact"]["sha256"]
        assert all(
            i < producers[0]
            for i, call in enumerate(calls)
            if "Prepare only a preimplementation design artifact" in call["prompt"]
        )
        assert (harness.repo / "answer.txt").read_text() == "done\n"
        assert (
            "Status: Completed" in next((harness.repo / "backlog").rglob("item-one.md")).read_text()
        )
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not producers
        assert not (harness.candidate / "answer.txt").exists()
        assert not (harness.repo / "answer.txt").exists()
    before_calls = calls_path.read_bytes()
    receipts = {
        p: p.read_bytes()
        for pattern in (
            "intent.json",
            "requested.json",
            "native-request.json",
            "design-attempt*.json",
            "design-acceptance.json",
        )
        for p in tmp_path.rglob(pattern)
    }
    assert receipts
    replay = harness.run("run-item", "item-one")
    assert replay.returncode == result.returncode, replay.stdout + replay.stderr
    assert calls_path.read_bytes() == before_calls
    assert all(p.read_bytes() == content for p, content in receipts.items())
    assert preparation.read_bytes() == original
