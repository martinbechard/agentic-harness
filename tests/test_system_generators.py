"""Installed integration regenerates declared projections within a confined clone."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, command, install_wheel


@pytest.fixture(scope="session")
def generator_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


SCRIPT = """import sys
from pathlib import Path
lines = Path('source.txt').read_text().splitlines()
expected = lines[0] + ':' + lines[-1] + '\\n'
if '--check' in sys.argv:
    assert lines[0] == 'candidate'
    assert Path('generated.txt').read_text() == expected
else:
    counter = Path('.git/generator-count')
    counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))
    if '--slow' in sys.argv:
        import subprocess,time
        child = subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
        Path('.git/generator-child.pid').write_text(str(child.pid))
        time.sleep(60)
    if '--external' in sys.argv:
        Path(sys.argv[sys.argv.index('--external')+1]).write_text('escaped')
    if '--undeclared' in sys.argv:
        Path('undeclared.txt').write_text('outside declared outputs')
    Path('generated.txt').write_text(expected)
"""


def git(repo, *args):
    run = command(["git", "-C", repo, *args])
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def prepare(generator_python, tmp_path, mode):
    harness = InstalledHarness.create(
        tmp_path, generator_python, dispatcher=Path(__file__).parent / "fixtures/generator_agent.py"
    )
    harness.env["HARNESS_GENERATOR_PYTHON"] = str(generator_python)
    (harness.repo / "source.txt").write_text("\n".join("base " + str(i) for i in range(30)) + "\n")
    (harness.repo / "generated.txt").write_text("base 0:base 29\n")
    (harness.repo / "generate.py").write_text(SCRIPT)
    git(harness.repo, "add", "source.txt", "generated.txt", "generate.py")
    git(harness.repo, "commit", "-m", "Tracked generator and authoritative inputs")
    git(harness.candidate, "pull", "--ff-only")
    config = yaml.safe_load(harness.config_path.read_text())
    config["workflow"]["allowed_paths"] = ["source.txt", "generated.txt"]
    config["workflow"]["checks"] = [[str(generator_python), "generate.py", "--check"]]
    harness.config_path.write_text(yaml.safe_dump(config))
    first = harness.run("run-item", "item-one")
    assert first.returncode == 2 and "Primary source paths advanced" in first.stderr, (
        first.stdout + first.stderr
    )
    sentinel = tmp_path / "external.txt"
    sentinel.write_text("preserved")
    argv = [str(generator_python), "generate.py"]
    if mode == "external":
        argv += ["--external", str(sentinel)]
    if mode == "undeclared":
        argv += ["--undeclared"]
    instruction = tmp_path / "integration.json"
    instruction.write_text(
        json.dumps(
            {
                "item_id": "item-one",
                "original_candidate": git(harness.candidate, "rev-parse", "HEAD"),
                "primary": git(harness.repo, "rev-parse", "HEAD"),
                "source_reference": "fixture authorized task",
                "authorization": "Complete item including generated integration",
                "prospective_estimate": {"remaining_high": 100},
                "generated_paths": ["generated.txt"],
                "generators": [argv],
                "generator_inputs": {"generate.py": sha256(SCRIPT.encode()).hexdigest()},
            }
        )
    )
    return harness, instruction, sentinel


def test_s06_generated_conflict_regenerates_and_delivers(generator_python, tmp_path):
    harness, instruction, _ = prepare(generator_python, tmp_path, "valid")
    calls = tmp_path / "agent-calls.jsonl"
    before = len(calls.read_text().splitlines())
    result = harness.run("reconcile-integration", "item-one", "--instruction", instruction)
    assert result.returncode == 0, result.stdout + result.stderr
    authority = json.loads(result.stdout)
    record = authority["candidate_record"]
    workspace = Path(record["workspace"])
    counter = workspace / ".git/generator-count"
    assert counter.read_text() == "1"
    requested_path = next(
        harness.repo.glob(
            ".agent-ops/backlog-harness/workflow-evidence/*/integration-candidate/generator-0-requested.json"
        )
    )
    requested = json.loads(requested_path.read_text())
    assert requested["argv"] == [str(generator_python), "generate.py"]
    before_source = git(workspace, "show", requested["input_tree"] + ":source.txt").splitlines()
    assert before_source[0] == "candidate" and before_source[-1] == "primary"
    assert git(workspace, "show", requested["input_tree"] + ":generated.txt") == "base 0:primary"
    assert (workspace / "generated.txt").read_text() == "candidate:primary\n"
    assert len(calls.read_text().splitlines()) == before + 1
    repeated = harness.run("reconcile-integration", "item-one", "--instruction", instruction)
    assert repeated.returncode == 0 and json.loads(repeated.stdout) == authority
    assert counter.read_text() == "1"
    assert len(calls.read_text().splitlines()) == before + 1
    finish = harness.run("run-item", "item-one")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    closed = json.loads(finish.stdout)
    assert closed["advancement_verified"] and closed["delivery"]["verified"]
    assert closed["delivery"]["candidate"] == record["candidate"]
    assert closed["delivery"]["main_commit"] != closed["commit"]
    assert (harness.repo / "generated.txt").read_text() == "candidate:primary\n"
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    assert counter.read_text() == "1"
    assert len(calls.read_text().splitlines()) == before + 2


@pytest.mark.parametrize("mode", ["external", "undeclared"])
def test_s06_generator_scope_rejects_escape(generator_python, tmp_path, mode):
    harness, instruction, sentinel = prepare(generator_python, tmp_path, mode)
    head = git(harness.repo, "rev-parse", "HEAD")
    calls = (tmp_path / "agent-calls.jsonl").read_bytes()
    result = harness.run("reconcile-integration", "item-one", "--instruction", instruction)
    assert result.returncode == 2, result.stdout + result.stderr
    generator_results = list(
        harness.repo.glob(
            ".agent-ops/backlog-harness/workflow-evidence/*/integration-candidate/generator-0-result.json"
        )
    )
    assert len(generator_results) == 1
    execution = json.loads(generator_results[0].read_text())
    if mode == "external":
        assert execution["returncode"] != 0
        assert "PermissionError" in execution["stderr"]
        assert "Integration generator failed" in result.stderr
    else:
        assert execution["returncode"] == 0
        assert "undeclared source paths" in result.stderr
    assert sentinel.read_text() == "preserved"
    assert git(harness.repo, "rev-parse", "HEAD") == head
    assert (tmp_path / "agent-calls.jsonl").read_bytes() == calls
    assert not list(
        harness.repo.glob(
            ".agent-ops/backlog-harness/workflow-evidence/*/superseding-delivery-authorization.json"
        )
    )
    assert "Status: Running" in (harness.repo / "backlog/feature-backlog/item-one.md").read_text()


def test_s06_generator_cli_sigint_cleans_group_and_retains_failure(generator_python, tmp_path):
    import signal
    import subprocess
    import time

    harness, instruction, _ = prepare(generator_python, tmp_path, "valid")
    packet = json.loads(instruction.read_text())
    packet["generators"][0].append("--slow")
    instruction.write_text(json.dumps(packet))
    log = tmp_path / "cancel.log"
    calls = (tmp_path / "agent-calls.jsonl").read_bytes()
    with log.open("w") as output:
        process = subprocess.Popen(
            [
                str(generator_python.parent / "agentic-harness"),
                "--config",
                str(harness.config_path),
                "reconcile-integration",
                "item-one",
                "--instruction",
                str(instruction),
            ],
            cwd=harness.repo,
            env=harness.env,
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 15
            markers = []
            while time.monotonic() < deadline:
                markers = list(tmp_path.glob(".integration-workspaces/*/.git/generator-child.pid"))
                if markers:
                    break
                assert process.poll() is None, log.read_text()
                time.sleep(0.05)
            assert len(markers) == 1, log.read_text()
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=15) != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    results = list(
        harness.repo.glob(
            ".agent-ops/backlog-harness/workflow-evidence/*/integration-candidate/generator-0-result.json"
        )
    )
    assert len(results) == 1, log.read_text()
    receipt = json.loads(results[0].read_text())
    assert receipt["failure_reason"] == "cancelled" and receipt["process_group_quiescent"]
    child = command(["ps", "-p", markers[0].read_text(), "-o", "stat="]).stdout.strip()
    assert not child or child.startswith("Z")
    counter = markers[0].with_name("generator-count")
    assert counter.read_text() == "1"
    retried = harness.run("reconcile-integration", "item-one", "--instruction", instruction)
    assert retried.returncode == 2
    assert counter.read_text() == "1"
    assert (tmp_path / "agent-calls.jsonl").read_bytes() == calls
    assert not list(
        harness.repo.glob(
            ".agent-ops/backlog-harness/workflow-evidence/*/superseding-delivery-authorization.json"
        )
    )
