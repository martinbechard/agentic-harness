"""Installed public reconciliation after a compatible primary advance."""

import json
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, command


@pytest.fixture(scope="session")
def integration_python(installed_package_python):
    return installed_package_python


@pytest.mark.parametrize(
    "conflict",
    [False, True, "resolver-correction", "review-correction", "scope-violation", "resolver-crash"],
)
def test_s06_compatible_primary_advance_reconciles_to_closure(
    integration_python, tmp_path, conflict
):
    harness = InstalledHarness.create(
        tmp_path,
        integration_python,
        scenario=(
            "integration-conflict-" + conflict
            if isinstance(conflict, str)
            else "integration-conflict"
            if conflict
            else "legacy"
        ),
        dispatcher=Path(__file__).parent / "fixtures/integration_agent.py",
    )

    def git(repo, *args):
        run = command(["git", "-C", repo, *args])
        assert run.returncode == 0, run.stderr
        return run.stdout.strip()

    (harness.repo / "answer.txt").write_text("\n".join("base " + str(i) for i in range(30)) + "\n")
    if conflict:
        (harness.repo / "answer.txt").write_text(json.dumps({"enabled": False, "flags": []}) + "\n")
    git(harness.repo, "add", "answer.txt")
    git(harness.repo, "commit", "-m", "Shared source base")
    git(harness.candidate, "pull", "--ff-only")
    config = yaml.safe_load(harness.config_path.read_text())
    config["workflow"]["checks"] = [
        [
            str(integration_python),
            "-c",
            "from pathlib import Path; assert Path('answer.txt').read_text().splitlines()[0] == 'done'",
        ]
    ]
    if conflict:
        config["workflow"]["checks"][0][-1] = (
            "import json; from pathlib import Path; assert json.loads(Path('answer.txt').read_text())['enabled']"
        )
    harness.config_path.write_text(yaml.safe_dump(config))
    first = harness.run("run-item", "item-one")
    assert first.returncode == 2, first.stdout + first.stderr
    assert "Primary source paths advanced" in first.stderr
    candidate = git(harness.candidate, "rev-parse", "HEAD")
    instruction = tmp_path / "integration.json"
    instruction.write_text(
        json.dumps(
            {
                "item_id": "item-one",
                "original_candidate": candidate,
                "primary": git(harness.repo, "rev-parse", "HEAD"),
                "source_reference": "fixture-task-authority",
                "authorization": "Complete this item including integration repair",
                "prospective_estimate": {"remaining_high": 100},
            }
        )
    )
    stage_calls = {
        False: 1,
        True: 2,
        "resolver-correction": 3,
        "review-correction": 4,
        "scope-violation": 1,
        "resolver-crash": 2,
    }[conflict]
    calls_path = tmp_path / "agent-calls.jsonl"
    original_calls = calls_path.read_text().splitlines()
    original_checks = next(
        harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/source-checks.json")
    )
    checks_bytes = original_checks.read_bytes()
    original_evidence = {path: path.read_bytes() for path in original_checks.parent.glob("*.json")}
    if conflict == "resolver-crash":
        crash_after_resolver_completion(harness, integration_python, instruction, tmp_path)
    result = harness.run("reconcile-integration", "item-one", "--instruction", str(instruction))
    if conflict == "scope-violation":
        assert result.returncode == 2, result.stdout + result.stderr
        assert "Resolver changed undeclared paths" in result.stderr
        assert len(calls_path.read_text().splitlines()) == len(original_calls) + 1
        repeated = harness.run(
            "reconcile-integration", "item-one", "--instruction", str(instruction)
        )
        assert repeated.returncode == 2
        assert len(calls_path.read_text().splitlines()) == len(original_calls) + 1
        assert not (harness.repo / "unrelated.txt").exists()
        assert not (harness.repo / "backlog/archive/item-one.md").exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["candidate"] != candidate
    assert len(calls_path.read_text().splitlines()) == len(original_calls) + stage_calls
    repeated = harness.run("reconcile-integration", "item-one", "--instruction", str(instruction))
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout) == json.loads(result.stdout)
    assert len(calls_path.read_text().splitlines()) == len(original_calls) + stage_calls
    finish = harness.run("run-item", "item-one")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    lines = (harness.repo / "answer.txt").read_text().splitlines()
    if conflict:
        assert json.loads("\n".join(lines)) == {"enabled": True, "flags": ["keep"]}
    else:
        assert lines[0] == "done" and lines[-1] == "primary advance"
    assert original_checks.read_bytes() == checks_bytes
    assert all(path.read_bytes() == value for path, value in original_evidence.items())
    assert len(calls_path.read_text().splitlines()) == len(original_calls) + stage_calls + 1
    completed = harness.run("run-item", "item-one")
    assert completed.returncode == 0, completed.stderr
    assert len(calls_path.read_text().splitlines()) == len(original_calls) + stage_calls + 1


def crash_after_resolver_completion(harness, python, instruction, tmp_path):
    """Kill after durable terminal output, before the supervisor records its result."""
    import os
    import signal
    import subprocess
    import time

    marker = tmp_path / "resolver-terminal-pause.json"
    log = tmp_path / "resolver-crash.log"
    stopped = False
    with log.open("w") as stream:
        process = subprocess.Popen(
            [
                str(python.parent / "agentic-harness"),
                "--config",
                str(harness.config_path),
                "reconcile-integration",
                "item-one",
                "--instruction",
                str(instruction),
            ],
            cwd=harness.repo,
            env=harness.env,
            stdout=stream,
            stderr=stream,
        )
        try:
            deadline = time.monotonic() + 30
            while not marker.exists() and time.monotonic() < deadline:
                assert process.poll() is None, log.read_text()
                time.sleep(0.05)
            assert marker.exists(), log.read_text()
            evidence = harness.repo / ".agent-ops/backlog-harness"
            deadline = time.monotonic() + 5
            completed = []
            while time.monotonic() < deadline:
                completed = [
                    path
                    for path in evidence.glob("runs/*/operations/*/invocations/*/events.jsonl")
                    if "resolution_digest" in path.read_text()
                    and "turn.completed" in path.read_text()
                ]
                if completed:
                    break
                time.sleep(0.05)
            assert len(completed) == 1, "Resolver terminal transport was not persisted"
            assert not list(evidence.glob("workflow-evidence/*/integration-resolve.json"))
            process.kill()
            process.wait(timeout=5)
            os.killpg(json.loads(marker.read_text())["pid"], signal.SIGKILL)
            stopped = True
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if marker.exists() and not stopped:
                try:
                    os.killpg(json.loads(marker.read_text())["pid"], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
