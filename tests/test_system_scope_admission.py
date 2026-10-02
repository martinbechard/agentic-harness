"""Installed CLI preserves one stopped execution across an exact scope amendment."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, command, install_wheel

from backlog_harness.contracts import digest
from backlog_harness.evidence import component


@pytest.fixture(scope="session")
def scope_admission_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def git(repository, *args):
    result = command(["git", "-C", repository, *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_public_scope_amendment_reuses_provider_effect_and_native_execution(
    scope_admission_python, tmp_path
):
    harness = InstalledHarness.create(
        tmp_path,
        scope_admission_python,
        dispatcher=Path(__file__).parent / "fixtures/scope_amendment_agent.py",
    )
    (harness.repo / "answer.txt").write_text("base\n")
    git(harness.repo, "add", "--", "answer.txt")
    git(harness.repo, "commit", "-m", "Scope amendment fixture base")
    assert command(["git", "-C", harness.candidate, "pull", "--ff-only"]).returncode == 0

    candidates = tmp_path / "candidates"
    candidates.mkdir()
    candidate = candidates / component("item-one")
    harness.candidate.rename(candidate)
    harness.candidate = candidate
    config = yaml.safe_load(harness.config_path.read_text())
    old_check = [
        str(scope_admission_python),
        "-c",
        "from pathlib import Path; assert Path('answer.txt').read_text() == 'candidate\\n'",
    ]
    new_check = [str(scope_admission_python), "-c", "assert True"]
    config["candidate_root"] = str(candidates)
    config["workspace"] = str(candidate)
    config["workflow"].update(
        allowed_paths=["answer.txt"],
        checks=[old_check],
        preparation={
            "allowed_roots": ["answer.txt", "expanded.txt"],
            "check_commands": [old_check, new_check],
        },
    )
    harness.config_path.write_text(yaml.safe_dump(config))
    harness.env["HARNESS_SCOPE_OLD_CHECK"] = json.dumps(old_check)

    stopped = harness.run("run-item", "item-one")
    assert stopped.returncode == 2
    assert "Question boundary contains unapproved source work" in stopped.stderr
    evidence = next(
        harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/assignment.json")
    ).parent
    originals = {
        name: (evidence / f"{name}.json").read_bytes()
        for name in ("assignment", "accept", "produce-review", "preparation")
        if (evidence / f"{name}.json").exists()
    }
    acceptance = json.loads((evidence / "accept.json").read_text())
    previous = json.loads((evidence / "produce-review.json").read_text())
    previous_value = json.loads(previous["text"])
    assert previous_value["question"]["question_id"] == "reconcile-verifier-validation-blocker"

    config["workflow"].update(
        allowed_paths=["answer.txt", "expanded.txt"], checks=[old_check, new_check]
    )
    harness.config_path.write_text(yaml.safe_dump(config))
    item_path = harness.repo / "backlog/feature-backlog/item-one.md"
    content = item_path.read_text()
    amended = content + "\n## Expanded requirement\n\nRun the expanded check.\n"
    authority = tmp_path / "authority.txt"
    answer = "Consolidate the common cause into the original item."
    question = previous_value["question"]
    authority.write_text(question["question_id"] + "\n" + question["text"] + "\n" + answer)
    request = {
        "version": 1,
        "item_id": "item-one",
        "expected_revision": sha256(
            b"backlog/feature-backlog/item-one.md\0" + content.encode()
        ).hexdigest(),
        "previous_admission_digest": None,
        "authority_sources": [
            {
                "path": str(authority),
                "sha256": sha256(authority.read_bytes()).hexdigest(),
                "reason": "exact design authority response",
            }
        ],
        "original_preparation_digest": digest(
            json.loads((evidence / "preparation.json").read_text())
        ),
        "original_assignment_digest": digest(json.loads((evidence / "assignment.json").read_text())),
        "acceptance_digest": digest(acceptance),
        "previous_result_digest": digest(previous),
        "scope_answer": {
            "question_id": question["question_id"],
            "question_digest": digest(question),
            "answer_text": answer,
            "authority_reference": str(authority),
            "authority_digest": sha256(authority.read_bytes()).hexdigest(),
        },
        "candidate": {
            "repository": str(candidate),
            "base": json.loads((evidence / "base.json").read_text())["commit"],
            "head": git(candidate, "rev-parse", "HEAD"),
            "tree": git(candidate, "rev-parse", "HEAD^{tree}"),
        },
        "scope": {
            "allowed_paths": ["answer.txt", "expanded.txt"],
            "checks": [old_check, new_check],
        },
        "amended_content": amended,
    }
    request_path = tmp_path / "scope-amendment.json"
    request_path.write_text(json.dumps(request))
    calls_path = tmp_path / "agent-calls.jsonl"
    before = len(calls_path.read_text().splitlines())

    amended_result = harness.run("amend-scope", "item-one", "--input", request_path)
    assert amended_result.returncode == 0, amended_result.stdout + amended_result.stderr
    assert item_path.read_text() == amended
    assert len(calls_path.read_text().splitlines()) == before + 2
    assert all((evidence / f"{name}.json").read_bytes() == value for name, value in originals.items())
    retained = {
        path: path.read_bytes()
        for path in evidence.glob("scope-*.json")
    }

    replay = harness.run("amend-scope", "item-one", "--input", request_path)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert len(calls_path.read_text().splitlines()) == before + 2
    assert all(path.read_bytes() == value for path, value in retained.items())

    continued = harness.run("run-item", "item-one")
    assert continued.returncode == 0, continued.stdout + continued.stderr
    marker = json.loads((tmp_path / "scope-continuation.json").read_text())
    assert marker == {
        "cwd": str(candidate.resolve()),
        "resumed": True,
        "session_id": acceptance["session"]["native_session_id"],
    }
    final_candidate = git(candidate, "rev-parse", "HEAD")
    assert git(candidate, "merge-base", "--is-ancestor", request["candidate"]["head"], final_candidate) == ""
    assert (harness.repo / "answer.txt").read_text() == "candidate\n"
    assert (harness.repo / "expanded.txt").read_text() == "expanded\n"
    archived = harness.repo / "backlog/archive/item-one.md"
    assert "Status: Completed" in archived.read_text()
    checks = json.loads((evidence / "source-checks.json").read_text())
    assert [row["argv"] for row in checks] == [old_check, new_check]
    final_calls = len(calls_path.read_text().splitlines())
    terminal_replay = harness.run("run-item", "item-one")
    assert terminal_replay.returncode == 0, terminal_replay.stdout + terminal_replay.stderr
    assert len(calls_path.read_text().splitlines()) == final_calls
