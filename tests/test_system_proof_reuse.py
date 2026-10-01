"""Installed public CLI preserves accepted scoped proof across unrelated integration."""

import json
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from system_support import InstalledHarness, command, install_wheel


@pytest.fixture(scope="session")
def proof_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def git(repo, *args):
    result = command(["git", "-C", repo, *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write_json(path, value):
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def preserved_case(tmp_path, python):
    harness = InstalledHarness.create(
        tmp_path, python, dispatcher=Path(__file__).parent / "fixtures/proof_reuse_agent.py"
    )
    (harness.repo / "answer.txt").write_text("before\n")
    (harness.repo / "guidance.txt").write_text("unrelated guidance\n")
    (harness.repo / "expectations.txt").write_text("Expected answer: done\n")
    git(harness.repo, "add", "answer.txt", "guidance.txt", "expectations.txt")
    git(harness.repo, "commit", "-m", "Initial source")
    git(harness.candidate, "pull", "--ff-only")
    base = git(harness.candidate, "rev-parse", "HEAD")
    (harness.candidate / "answer.txt").write_text("done\n")
    git(harness.candidate, "add", "answer.txt")
    git(harness.candidate, "commit", "-m", "Historical external candidate")
    candidate = git(harness.candidate, "rev-parse", "HEAD")
    native, turn = str(uuid4()), str(uuid4())
    runtime = tmp_path / ("rollout-" + native + ".jsonl")
    runtime.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                {"type": "session_meta", "payload": {"id": native}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
            ]
        )
    )
    execution = "external-fixture/item-one"
    snapshot, operation = execution + "/1/snapshot", execution + "/1/code"
    receipt = write_json(
        tmp_path / "historical-receipt.json",
        {
            snapshot: {"receipt": {"candidate_sha": candidate}},
            operation: {
                "receipt": {
                    "op_id": operation,
                    "session_id": native,
                    "started_unix": 1,
                    "finished_unix": 2,
                }
            },
        },
    )
    remaining = "Finish semantic proof, independent review and exact-candidate approval."
    item = harness.repo / "backlog/feature-backlog/item-one.md"
    item.write_text(
        item.read_text()
        + "\nExecution: "
        + execution
        + "\nCandidate: "
        + candidate
        + "\n"
        + remaining
        + "\n"
    )
    git(harness.repo, "add", str(item.relative_to(harness.repo)))
    git(harness.repo, "commit", "-m", "Record stopped external execution")
    evidence = harness.candidate / ".agent-ops/preserved.json"
    evidence.parent.mkdir(exist_ok=True)
    supporting = write_json(evidence, {"candidate": candidate})
    config = yaml.safe_load(harness.config_path.read_text())
    (tmp_path / "imports").mkdir()
    config["candidate_root"] = str(tmp_path / "imports")
    config["profiles"]["worker"]["artifact_output"] = True
    harness.config_path.write_text(yaml.safe_dump(config))
    write_json(
        tmp_path / "scope.json", {k: config["workflow"][k] for k in ("allowed_paths", "checks")}
    )
    supplied = tmp_path / "recovery.json"
    write_json(
        supplied,
        {
            "candidate": {
                "checkout": str(harness.candidate),
                "head": candidate,
                "base": base,
                "allowed_paths": ["answer.txt"],
                "evidence": [{"path": ".agent-ops/preserved.json", "sha256": supporting["sha256"]}],
            },
            "runtime_records": [
                {
                    "path": str(runtime),
                    "sha256": sha256(runtime.read_bytes()).hexdigest(),
                    "native_session_id": native,
                    "final_turn_id": turn,
                }
            ],
            "preserved_execution": {
                "execution_id": execution,
                "snapshot_operation": snapshot,
                "runtime_operations": {native: operation},
                "candidate_approval_required": True,
                "canonical_sha256": sha256(item.read_bytes()).hexdigest(),
                "remaining_work": remaining,
                "receipt": receipt,
            },
        },
    )
    result = harness.run("recover-item", "item-one", "--evidence", supplied)
    assert result.returncode == 0, result.stdout + result.stderr
    return harness, candidate


@pytest.mark.parametrize("fault", [None, "guidance", "dependency", "tamper", "missing"])
def test_installed_scoped_proof_reuse(proof_python, tmp_path, fault):
    harness, original = preserved_case(tmp_path, proof_python)
    result = harness.run("run-item", "item-one")
    assert result.returncode == 2 and "Need scoped semantic proof" in result.stderr, (
        result.stdout + result.stderr
    )
    instruction = tmp_path / "continue.txt"
    instruction.write_text("Complete only scoped proof and retained independent review.")
    for verb in ["continue-work", "run-item", "run-proof", "continue-proof", "run-item"]:
        args = [verb, "item-one"] + (["--instruction", instruction] if verb != "run-item" else [])
        result = harness.run(*args)
        assert result.returncode == (
            2 if verb == "run-item" and "Need scoped semantic proof" in result.stderr else 0
        ), result.stdout + result.stderr
    stages = next(
        harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/proof-review.json")
    ).parent
    item = next(harness.repo.glob("backlog/**/item-one.md"))
    revision = sha256(
        str(item.relative_to(harness.repo)).encode() + b"\0" + item.read_bytes()
    ).hexdigest()
    authorization = tmp_path / "approve.json"
    write_json(
        authorization,
        {
            "item_id": "item-one",
            "question_id": "approve-proof",
            "revision": revision,
            "candidate": original,
            "disposition": "approve",
            "answer": "Approve this exact candidate and complete authorized integration.",
            "source_reference": "fixture explicit operator answer",
        },
    )
    approved = harness.run("authorize-delivery", "item-one", "--authorization", authorization)
    assert approved.returncode == 0, approved.stdout + approved.stderr
    original_bytes = {p: p.read_bytes() for p in stages.glob("*.json")}
    calls = tmp_path / "agent-calls.jsonl"
    proof_calls = lambda: sum(
        "Perform only the missing proof" in json.loads(line)["prompt"]
        for line in calls.read_text().splitlines()
    )
    assert proof_calls() == 1
    original_artifacts = {
        p: p.read_bytes()
        for p in harness.repo.glob(".agent-ops/backlog-harness/runs/**/artifacts/*.json")
    }
    if fault in ("tamper", "missing"):
        artifact = next(p for p in original_artifacts if p.name == "candidate-binding.json")
        artifact.unlink() if fault == "missing" else artifact.write_text("{}")
    path = (
        "expectations.txt"
        if fault == "dependency"
        else "guidance.txt"
        if fault == "guidance"
        else "unrelated.txt"
    )
    (harness.repo / path).write_text(
        "changed relevant input\n" if fault == "dependency" else "new unrelated guidance\n"
    )
    git(harness.repo, "add", path)
    git(harness.repo, "commit", "-m", "Concurrent primary advance")
    integration = tmp_path / "integration.json"
    write_json(
        integration,
        {
            "item_id": "item-one",
            "original_candidate": original,
            "primary": git(harness.repo, "rev-parse", "HEAD"),
            "source_reference": "fixture explicit operator authority",
            "authorization": "Integrate reviewed candidate under original approval",
            "prospective_estimate": {"remaining_high": 1000},
        },
    )
    result = harness.run("reconcile-integration", "item-one", "--instruction", integration)
    if fault in ("dependency", "tamper", "missing"):
        assert result.returncode == 2, result.stdout + result.stderr
        if fault == "dependency":
            assert "fresh verified proof required" in result.stderr, result.stderr
        else:
            assert "proof" in result.stderr.lower(), result.stderr
        assert not (harness.repo / "backlog/archive/item-one.md").exists()
        assert proof_calls() == 1
        return
    assert result.returncode == 0, result.stdout + result.stderr
    merged = json.loads(result.stdout)["candidate"]
    assert merged != original
    records = [json.loads(p.read_text()) for p in stages.glob("*.json")]
    reuse_records = [
        record for record in records if isinstance(record, dict) and "proof_reuse" in record
    ]
    assert reuse_records
    reuse = next(
        record["proof_reuse"]
        for record in reuse_records
        if record["proof_reuse"].get("disposition") == "reviewed-applicability"
    )
    assert reuse["context"]["original_candidate"] == original
    assert reuse["context"]["candidate"] == merged
    assert reuse["decision"]["candidate"] == merged
    assert reuse["decision"]["verdict"] == "ACCEPT"
    assert reuse["context"]["changed_dependencies"] == (
        ["guidance.txt"] if fault == "guidance" else []
    )
    assert {row["path"] for row in reuse["context"]["dependencies"]} == {
        "answer.txt",
        "expectations.txt",
        "guidance.txt",
    }
    finish = harness.run("run-item", "item-one")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    assert proof_calls() == 1
    assert all(p.read_bytes() == value for p, value in original_bytes.items())
    assert all(p.read_bytes() == value for p, value in original_artifacts.items())
    count = len(calls.read_text().splitlines())
    repeated = harness.run("run-item", "item-one")
    assert repeated.returncode == 0, repeated.stderr
    assert len(calls.read_text().splitlines()) == count
