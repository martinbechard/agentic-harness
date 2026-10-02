"""Installed CLI preserves one stopped execution across an exact scope amendment."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from system_support import InstalledHarness, command

from backlog_harness.contracts import digest
from backlog_harness.evidence import component


@pytest.fixture(scope="session")
def scope_admission_python(installed_package_python):
    return installed_package_python


def git(repository, *args):
    result = command(["git", "-C", repository, *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def calls(harness):
    path = harness.repo.parent / "agent-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def question_revision(harness):
    item = harness.repo / "backlog/user-action-required/item-one.md"
    relative = str(item.relative_to(harness.repo))
    return sha256(relative.encode() + b"\0" + item.read_bytes()).hexdigest()


def amended_harness(scope_admission_python, tmp_path, *, prior_answer=False):
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

    if prior_answer:
        harness.env["HARNESS_SCOPE_PRIOR_QUESTION"] = "1"
        prior_question = harness.run("run-item", "item-one")
        assert prior_question.returncode == 0, prior_question.stdout + prior_question.stderr
        prior_revision = question_revision(harness)
        prior_answered = harness.run(
            "answer",
            "item-one",
            "--question-id",
            "confirm-retained-execution",
            "--revision",
            prior_revision,
            "--text",
            "Continue this exact native execution.",
        )
        assert prior_answered.returncode == 0, prior_answered.stdout + prior_answered.stderr

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
    previous_stage = (
        json.loads((evidence / "continuation.json").read_text())["stage"]
        if prior_answer
        else "produce-review"
    )
    previous = json.loads((evidence / f"{previous_stage}.json").read_text())
    previous_value = json.loads(previous["text"])
    assert previous_value["question"]["question_id"] == "reconcile-verifier-validation-blocker"

    config["workflow"].update(
        allowed_paths=["answer.txt", "expanded.txt"], checks=[old_check, new_check]
    )
    harness.config_path.write_text(yaml.safe_dump(config))
    item_paths = list(harness.repo.glob("backlog/*/item-one.md"))
    assert len(item_paths) == 1
    item_path = item_paths[0]
    item_relative = str(item_path.relative_to(harness.repo))
    content = item_path.read_text()
    amended = content + "\n## Expanded requirement\n\nRun the expanded check.\n"
    authority = tmp_path / "authority.txt"
    answer = "Consolidate the common cause into the original item."
    question = previous_value["question"]
    authority.write_text(question["question_id"] + "\n" + question["text"] + "\n" + answer)
    request = {
        "version": 1,
        "item_id": "item-one",
        "expected_revision": sha256(item_relative.encode() + b"\0" + content.encode()).hexdigest(),
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
        "original_assignment_digest": digest(
            json.loads((evidence / "assignment.json").read_text())
        ),
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
        "review_requirements": None,
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
    assert all(
        (evidence / f"{name}.json").read_bytes() == value for name, value in originals.items()
    )
    retained = {path: path.read_bytes() for path in evidence.glob("scope-*.json")}

    replay = harness.run("amend-scope", "item-one", "--input", request_path)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert len(calls_path.read_text().splitlines()) == before + 2
    assert all(path.read_bytes() == value for path, value in retained.items())
    return harness, candidate, evidence, acceptance, request, calls_path


def test_public_scope_amendment_reuses_provider_effect_and_native_execution(
    scope_admission_python, tmp_path
):
    harness, candidate, evidence, acceptance, request, calls_path = amended_harness(
        scope_admission_python, tmp_path
    )

    continued = harness.run("run-item", "item-one")
    assert continued.returncode == 0, continued.stdout + continued.stderr
    marker = json.loads((tmp_path / "scope-continuation.json").read_text())
    assert marker == {
        "cwd": str(candidate.resolve()),
        "resumed": True,
        "session_id": acceptance["session"]["native_session_id"],
    }
    final_candidate = git(candidate, "rev-parse", "HEAD")
    assert (
        git(candidate, "merge-base", "--is-ancestor", request["candidate"]["head"], final_candidate)
        == ""
    )
    assert (harness.repo / "answer.txt").read_text() == "candidate\n"
    assert (harness.repo / "expanded.txt").read_text() == "expanded\n"
    archived = harness.repo / "backlog/archive/item-one.md"
    assert "Status: Completed" in archived.read_text()
    checks = json.loads((evidence / "source-checks.json").read_text())
    assert [row["argv"] for row in checks] == request["scope"]["checks"]
    final_calls = len(calls_path.read_text().splitlines())
    terminal_replay = harness.run("run-item", "item-one")
    assert terminal_replay.returncode == 0, terminal_replay.stdout + terminal_replay.stderr
    assert len(calls_path.read_text().splitlines()) == final_calls


def test_public_scope_amendment_answers_new_question_before_same_native_completion(
    scope_admission_python, tmp_path
):
    harness, candidate, _evidence, acceptance, request, calls_path = amended_harness(
        scope_admission_python, tmp_path
    )
    harness.env["HARNESS_SCOPE_POST_AMENDMENT_QUESTION"] = "1"

    questioned = harness.run("run-item", "item-one")
    assert questioned.returncode == 0, questioned.stdout + questioned.stderr
    waiting = harness.repo / "backlog/user-action-required/item-one.md"
    assert "Status: User Action Required" in waiting.read_text()
    assert "restore-required-verification-environment" in waiting.read_text()
    assert git(candidate, "rev-parse", "HEAD") == request["candidate"]["head"]
    unanswered_calls = calls(harness)

    unanswered = harness.run("run-item", "item-one")
    assert unanswered.returncode == 2
    assert calls(harness) == unanswered_calls

    revision = question_revision(harness)
    answer_args = (
        "answer",
        "item-one",
        "--question-id",
        "restore-required-verification-environment",
        "--revision",
        revision,
        "--text",
        "Restore the admitted verification environment and continue.",
    )
    answered = harness.run(*answer_args)
    assert answered.returncode == 0, answered.stdout + answered.stderr
    answered_calls = calls(harness)
    answer_replay = harness.run(*answer_args)
    assert answer_replay.returncode == 0, answer_replay.stdout + answer_replay.stderr
    assert calls(harness) == answered_calls

    continued = harness.run("run-item", "item-one")
    assert continued.returncode == 0, continued.stdout + continued.stderr
    marker = json.loads((tmp_path / "scope-continuation.json").read_text())
    assert marker == {
        "cwd": str(candidate.resolve()),
        "resumed": True,
        "session_id": acceptance["session"]["native_session_id"],
    }
    continuation_calls = [
        row for row in calls(harness) if "Scoped question continuation:" in row["prompt"]
    ]
    assert len(continuation_calls) == 1
    continuation = continuation_calls[0]
    assert "resume" in continuation["argv"]
    assert (
        continuation["argv"][continuation["argv"].index("resume") + 1]
        == acceptance["session"]["native_session_id"]
    )
    assert (
        git(
            candidate,
            "merge-base",
            "--is-ancestor",
            request["candidate"]["head"],
            git(candidate, "rev-parse", "HEAD"),
        )
        == ""
    )
    assert (harness.repo / "answer.txt").read_text() == "candidate\n"
    assert (harness.repo / "expanded.txt").read_text() == "expanded\n"
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()

    final_calls = len(calls_path.read_text().splitlines())
    terminal_replay = harness.run("run-item", "item-one")
    assert terminal_replay.returncode == 0, terminal_replay.stdout + terminal_replay.stderr
    assert len(calls_path.read_text().splitlines()) == final_calls


def test_public_scope_amendment_replays_historical_question_before_current_answer(
    scope_admission_python, tmp_path
):
    harness, candidate, evidence, acceptance, request, _calls_path = amended_harness(
        scope_admission_python, tmp_path, prior_answer=True
    )
    harness.env["HARNESS_SCOPE_POST_AMENDMENT_QUESTION"] = "1"
    admission_path = next(evidence.glob("scope-admission-receipt-*.json"))
    admission = json.loads(admission_path.read_text())
    stage = "scope-continuation-" + digest(admission)
    stage_path = evidence / f"{stage}.json"
    producer_before = [
        row
        for row in calls(harness)
        if "Running is now recorded for your exact session" in row["prompt"]
        and "Scope admission:" in row["prompt"]
    ]

    bootstrap = command(
        [
            scope_admission_python,
            Path(__file__).parent / "fixtures/scope_replay_bootstrap.py",
            "--config",
            harness.config_path,
            "run-item",
            "item-one",
        ],
        cwd=harness.repo,
        env=harness.env,
        timeout=60,
    )
    assert bootstrap.returncode == 2
    assert "Historical fixture stopped before provider effect" in bootstrap.stderr
    retained = json.loads(stage_path.read_text())
    assert json.loads(retained["text"])["question"] == {
        "question_id": "restore-required-verification-environment",
        "text": "Can the required verification environment be restored?",
    }
    historical_calls = [
        row
        for row in calls(harness)
        if "Running is now recorded for your exact session" in row["prompt"]
        and "Scope admission:" in row["prompt"]
    ]
    assert len(historical_calls) == len(producer_before) + 1
    assert "Persisted canonical approval:" in historical_calls[-1]["prompt"]
    assert "Scoped question continuation:" not in historical_calls[-1]["prompt"]
    retained_item = harness.repo / admission["provider_receipt"]["after"]["path"]
    assert "Status: Running" in retained_item.read_text()
    assert "restore-required-verification-environment" not in retained_item.read_text()

    # Altering the recorded result still blocks reuse even though prompt wording may change.
    retained_bytes = stage_path.read_bytes()
    changed = json.loads(retained_bytes)
    changed["text"] = json.dumps(
        {"item_id": "item-one", "question": {"question_id": "forged", "text": "Forged"}}
    )
    stage_path.write_text(json.dumps(changed))
    before_tamper = calls(harness)
    tampered = harness.run("run-item", "item-one")
    assert tampered.returncode == 2
    assert "Completed stage result differs" in tampered.stderr
    assert calls(harness) == before_tamper
    stage_path.write_bytes(retained_bytes)

    replayed = harness.run("run-item", "item-one")
    assert replayed.returncode == 0, replayed.stdout + replayed.stderr
    assert len(
        [
            row
            for row in calls(harness)
            if "Running is now recorded for your exact session" in row["prompt"]
            and "Scope admission:" in row["prompt"]
        ]
    ) == len(historical_calls)
    waiting = harness.repo / "backlog/user-action-required/item-one.md"
    assert "Status: User Action Required" in waiting.read_text()
    assert "restore-required-verification-environment" in waiting.read_text()

    answer_args = (
        "answer",
        "item-one",
        "--question-id",
        "restore-required-verification-environment",
        "--revision",
        question_revision(harness),
        "--text",
        "Restore the admitted verification environment and continue.",
    )
    answered = harness.run(*answer_args)
    assert answered.returncode == 0, answered.stdout + answered.stderr
    answered_calls = calls(harness)
    answer_replay = harness.run(*answer_args)
    assert answer_replay.returncode == 0, answer_replay.stdout + answer_replay.stderr
    assert calls(harness) == answered_calls

    completed = harness.run("run-item", "item-one")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    modern = [row for row in calls(harness) if "Scoped question continuation:" in row["prompt"]]
    assert len(modern) == 1
    assert "resume" in modern[0]["argv"]
    assert (
        modern[0]["argv"][modern[0]["argv"].index("resume") + 1]
        == acceptance["session"]["native_session_id"]
    )
    assert (
        git(
            candidate,
            "merge-base",
            "--is-ancestor",
            request["candidate"]["head"],
            git(candidate, "rev-parse", "HEAD"),
        )
        == ""
    )
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    final_calls = calls(harness)
    terminal_replay = harness.run("run-item", "item-one")
    assert terminal_replay.returncode == 0, terminal_replay.stdout + terminal_replay.stderr
    assert calls(harness) == final_calls
