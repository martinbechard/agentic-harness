"""Installed CLI: durable failed checks permit exactly one scoped correction."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, command


@pytest.fixture(scope="session")
def correction_python(installed_package_python):
    return installed_package_python


def git(repo, *args):
    result = command(["git", "-C", repo, *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("exhausted", [False, True])
def test_installed_failed_checks_amendment_and_bounded_correction(
    correction_python, tmp_path, exhausted
):
    harness = InstalledHarness.create(
        tmp_path,
        correction_python,
        scenario="check-correction-exhausted" if exhausted else "check-correction",
        dispatcher=Path(__file__).parent / "fixtures/check_correction_agent.py",
    )
    source = "\n".join(["base", "base 29", *[f"base {i}" for i in range(2, 30)]]) + "\n"
    (harness.repo / "answer.txt").write_text(source)
    (harness.repo / "generated.txt").write_text(json.dumps({"source": source, "runs": 0}))
    generator = harness.repo / "generate.py"
    generator.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "source = Path('answer.txt').read_text()\n"
        "if '--check' in sys.argv:\n"
        "    assert json.loads(Path('generated.txt').read_text())['source'] == source, 'stale generation'\n"
        "else:\n"
        "    output = Path('generated.txt')\n"
        "    runs = json.loads(output.read_text())['runs'] + 1\n"
        "    output.write_text(json.dumps({'source': source, 'runs': runs}))\n"
    )
    git(harness.repo, "add", "answer.txt", "generated.txt", "generate.py")
    git(harness.repo, "commit", "-m", "Existing generator and check premise")
    git(harness.candidate, "pull", "--ff-only")
    config = yaml.safe_load(harness.config_path.read_text())
    checks = [
        [str(correction_python), "generate.py", "--check"],
        [
            str(correction_python),
            "-c",
            "from pathlib import Path; lines = Path('answer.txt').read_text().splitlines(); assert lines[0] == 'done' and lines[1] == lines[-1], 'obsolete premise'",
        ],
    ]
    config["workflow"].update(allowed_paths=["answer.txt", "generated.txt"], checks=checks)
    harness.config_path.write_text(yaml.safe_dump(config))
    first = harness.run("run-item", "item-one")
    assert first.returncode == 2, first.stdout + first.stderr
    assert "Primary source paths advanced" in first.stderr
    evidence = next(
        harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/source-checks.json")
    ).parent
    originals = {path: path.read_bytes() for path in evidence.glob("*.json")}
    instruction = {
        "item_id": "item-one",
        "original_candidate": git(harness.candidate, "rev-parse", "HEAD"),
        "primary": git(harness.repo, "rev-parse", "HEAD"),
        "source_reference": "fixture-task-authority",
        "authorization": "Complete item including bounded failed-check integration repair",
        "prospective_estimate": {"remaining_high": 100},
    }
    instruction_path = tmp_path / "integration.json"
    instruction_path.write_text(json.dumps(instruction))
    # Fault injection stops only after the actual configured commands and durable receipts.
    bootstrap = (
        "import os, json\nfrom backlog_harness.application import Application\n"
        "from backlog_harness.cli import main\n"
        "from backlog_harness.workflow import TransitionBlocked\n"
        "original = Application.checks\n"
        "def interrupted(self, repository, item_id, candidate, stage):\n"
        "    try:\n"
        "        result = original(self, repository, item_id, candidate, stage)\n"
        "    except TransitionBlocked as error:\n"
        "        if stage != 'integration-checks' or str(error) != 'Required checks failed':\n"
        "            raise\n"
        "        result = json.loads(self._stage_path(item_id, stage).read_text())\n"
        "    if stage == 'integration-checks' and any(row['returncode'] for row in result):\n"
        "        os._exit(73)\n"
        "    return result\n"
        "Application.checks = interrupted\nraise SystemExit(main())\n"
    )
    calls = tmp_path / "agent-calls.jsonl"
    before_failure = calls.read_bytes()
    args = ["reconcile-integration", "item-one", "--instruction", str(instruction_path)]
    interrupted = command(
        [correction_python, "-c", bootstrap, "--config", harness.config_path, *args],
        cwd=harness.repo,
        env=harness.env,
    )
    assert interrupted.returncode == 73, interrupted.stdout + interrupted.stderr
    assert calls.read_bytes() == before_failure
    failed = json.loads((evidence / "integration-checks.json").read_text())
    assert [row["argv"] for row in failed] == checks
    assert all(row["returncode"] != 0 for row in failed)
    assert json.loads((harness.candidate / "generated.txt").read_text())["runs"] == 0
    retained = {path: path.read_bytes() for path in evidence.glob("integration-*.json")}
    candidate = json.loads((evidence / "integration-candidate.json").read_text())["candidate"]
    amendment = {
        "item_id": "item-one",
        "instruction_digest": sha256(
            json.dumps(instruction, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "failed_attempt": 1,
        "failed_candidate": candidate,
        "primary": instruction["primary"],
        "next_attempt": 2,
        "purpose": "reproduce-failed-required-check",
        "required_check": checks[0],
        "generators": [[str(correction_python), "generate.py"]],
        "generated_paths": ["generated.txt"],
        "generator_inputs": {"generate.py": sha256(generator.read_bytes()).hexdigest()},
    }
    amendment_path = tmp_path / "amendment.json"
    amendment_path.write_text(json.dumps(amendment))
    before = calls.read_text().splitlines()
    result = harness.run(*args, "--amendment", str(amendment_path))
    assert result.returncode == (2 if exhausted else 0), result.stdout + result.stderr
    generated = next((tmp_path / ".integration-workspaces").glob("*-2/generated.txt"))
    assert json.loads(generated.read_text())["runs"] == 1
    assert len(calls.read_text().splitlines()) == len(before) + (1 if exhausted else 2)
    assert all(path.read_bytes() == content for path, content in {**originals, **retained}.items())
    second_checks = json.loads((evidence / "integration-checks-2.json").read_text())
    assert [row["argv"] for row in second_checks] == checks
    assert second_checks[0]["returncode"] == 0
    assert bool(second_checks[1]["returncode"]) == exhausted
    replay = harness.run(*args, "--amendment", str(amendment_path))
    assert replay.returncode == result.returncode, replay.stdout + replay.stderr
    assert json.loads((evidence / "integration-checks-2.json").read_text()) == second_checks
    assert len(calls.read_text().splitlines()) == len(before) + (1 if exhausted else 2)
    generated = next((tmp_path / ".integration-workspaces").glob("*-2/generated.txt"))
    assert json.loads(generated.read_text())["runs"] == 1
    assert not list(evidence.glob("integration-*-3*"))
    if exhausted:
        assert "exhausted two attempts" in result.stderr
        assert not (evidence / "integration-review-2.json").exists()
        assert not (harness.repo / "backlog/archive/item-one.md").exists()
        return
    assert json.loads(replay.stdout) == json.loads(result.stdout)
    finish = harness.run("run-item", "item-one")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    assert (harness.repo / "answer.txt").read_text() == json.loads(
        (harness.repo / "generated.txt").read_text()
    )["source"]
    assert (harness.repo / "answer.txt").read_text().splitlines()[1] == "primary advance"
    assert (evidence / "integration-review-2.json").is_file()
    assert all(path.read_bytes() == content for path, content in {**originals, **retained}.items())
