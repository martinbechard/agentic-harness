"""Public installed CLI composed legacy lifecycle scenarios."""

import json

import pytest
from system_support import InstalledHarness, install_wheel


@pytest.fixture(scope="session")
def installed_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


def test_s01_legacy_one_item(installed_python, tmp_path):
    harness = InstalledHarness.create(tmp_path, installed_python)
    result = harness.run("run-item", "item-one")
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["delivery"]["disposition"] == "READY"
    assert value["advancement_verified"] is True
    assert value["after"]["state"] == "Completed"
    assert value["after"]["path"] == "backlog/archive/item-one.md"
    assert value["delivery"]["main_commit"] != value["commit"]
    assert "Status: Completed" in (harness.repo / value["after"]["path"]).read_text()
    mutations = [
        json.loads(line)
        for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
        if "Perform only this authorized provider transition" in json.loads(line)["prompt"]
    ]
    assert len(mutations) == 3
    assert (harness.repo / "answer.txt").read_text() == "done\n"


def test_s01_dependency_queue(installed_python, tmp_path):
    import yaml
    from system_support import command

    harness = InstalledHarness.create(tmp_path, installed_python)
    second = harness.repo / "backlog/feature-backlog/item-two.md"
    second.write_text(
        "# Two\n\nWork Item ID: item-two\nProvider: file\nStatus: Ready\nOwner: Unowned\nOriginal High Generated Tokens: 1000\nDepends On: item-one\n\n## Objective\nWrite second.txt.\n"
    )
    assert command(["git", "-C", harness.repo, "add", "."]).returncode == 0
    assert command(["git", "-C", harness.repo, "commit", "-m", "Second item"]).returncode == 0
    data = yaml.safe_load(harness.config_path.read_text())
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    data["candidate_root"] = str(candidates)
    data["workflow"]["items"] = {
        "item-two": {
            "allowed_paths": ["second.txt"],
            "checks": [
                [
                    str(installed_python),
                    "-c",
                    "from pathlib import Path; assert Path('second.txt').read_text() == 'done\\n'",
                ]
            ],
        }
    }
    harness.config_path.write_text(yaml.safe_dump(data))
    result = harness.run("run", "--until-terminal", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout[result.stdout.rfind("\n{") + 1 :])["outcome"] == "successful"
    for name in ("item-one", "item-two"):
        assert "Status: Completed" in (harness.repo / f"backlog/archive/{name}.md").read_text()
    assert (harness.repo / "answer.txt").read_text() == "done\n"
    assert (harness.repo / "second.txt").read_text() == "done\n"
    prompts = [
        json.loads(line)["prompt"]
        for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    ]
    mutations = [
        prompt for prompt in prompts if "Perform only this authorized provider transition" in prompt
    ]
    assert len(mutations) == 6
    decisions = [json.loads(prompt[prompt.index("\n{") + 1 :]) for prompt in mutations]
    assert [(d["item"]["item_id"], d["target"]) for d in decisions] == [
        ("item-one", "Starting"),
        ("item-one", "Running"),
        ("item-one", "Completed"),
        ("item-two", "Starting"),
        ("item-two", "Running"),
        ("item-two", "Completed"),
    ]


def test_s04_question_answer_restart(installed_python, tmp_path):
    from hashlib import sha256

    harness = InstalledHarness.create(tmp_path, installed_python, scenario="legacy-question")
    initial = harness.run("run-item", "item-one")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    item = harness.repo / "backlog/user-action-required/item-one.md"
    assert "Status: User Action Required" in item.read_text()
    calls = tmp_path / "agent-calls.jsonl"
    before = len(calls.read_text().splitlines())
    wrong = harness.run(
        "answer", "item-one", "--question-id", "wrong", "--revision", "stale", "--text", "English"
    )
    assert wrong.returncode == 2
    assert len(calls.read_text().splitlines()) == before
    relative = str(item.relative_to(harness.repo))
    revision = sha256(relative.encode() + b"\0" + item.read_bytes()).hexdigest()
    stale = harness.run(
        "answer",
        "item-one",
        "--question-id",
        "language",
        "--revision",
        "stale",
        "--text",
        "English",
    )
    assert stale.returncode == 2
    assert len(calls.read_text().splitlines()) == before
    answered = harness.run(
        "answer",
        "item-one",
        "--question-id",
        "language",
        "--revision",
        revision,
        "--text",
        "English",
    )
    assert answered.returncode == 0, answered.stdout + answered.stderr
    completed = harness.run("run-item", "item-one")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["delivery"]["verified"] is True
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    events = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum("Provider reservation is Starting" in event["prompt"] for event in events) == 1
    owner_calls = [
        event
        for event in events
        if "Classify the exact operator answer" in event["prompt"]
        or "Running is now recorded for your exact session" in event["prompt"]
    ]
    assert len(owner_calls) == 3
    assert len({event["argv"][event["argv"].index("resume") + 1] for event in owner_calls}) == 1
    before_duplicate = calls.read_bytes()
    duplicate = harness.run(
        "answer",
        "item-one",
        "--question-id",
        "language",
        "--revision",
        revision,
        "--text",
        "English",
    )
    assert duplicate.returncode == 0
    assert json.loads(duplicate.stdout) == json.loads(answered.stdout)
    assert calls.read_bytes() == before_duplicate


def test_s02_watch_idle_has_no_repeated_agent_calls(installed_python, tmp_path):
    import signal
    import subprocess
    import time

    import yaml
    from system_support import command

    harness = InstalledHarness.create(tmp_path, installed_python)
    data = yaml.safe_load(harness.config_path.read_text())
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    data["candidate_root"] = str(candidates)
    data["workflow"]["items"] = {
        "item-two": {
            "allowed_paths": ["second.txt"],
            "checks": [
                [
                    str(installed_python),
                    "-c",
                    "from pathlib import Path; assert Path('second.txt').read_text() == 'done\\n'",
                ]
            ],
        }
    }
    harness.config_path.write_text(yaml.safe_dump(data))
    done = harness.run("run-item", "item-one")
    assert done.returncode == 0, done.stdout + done.stderr
    calls = tmp_path / "agent-calls.jsonl"
    before = calls.read_bytes()
    output = tmp_path / "watch.log"
    with output.open("w") as stream:
        process = subprocess.Popen(
            [
                str(installed_python.parent / "agentic-harness"),
                "--config",
                str(harness.config_path),
                "run",
                "--watch",
            ],
            cwd=harness.repo,
            env=harness.env,
            stdout=stream,
            stderr=stream,
        )
        try:
            deadline = time.monotonic() + 15
            while "IdleWatch" not in output.read_text() and time.monotonic() < deadline:
                assert process.poll() is None, output.read_text()
                time.sleep(0.1)
            assert "IdleWatch" in output.read_text()
            time.sleep(2.2)
            assert calls.read_bytes() == before
            status = harness.run("status")
            assert status.returncode == 0
            assert calls.read_bytes() == before
            new = harness.repo / "backlog/feature-backlog/item-two.md"
            new.write_text(
                "# Two\n\nWork Item ID: item-two\nProvider: file\nStatus: Ready\nOwner: Unowned\nOriginal High Generated Tokens: 1000\n\n## Objective\nWrite second.txt.\n"
            )
            assert command(["git", "-C", harness.repo, "add", str(new)]).returncode == 0
            assert (
                command(["git", "-C", harness.repo, "commit", "-m", "New watch item"]).returncode
                == 0
            )
            deadline = time.monotonic() + 60
            while (
                not (harness.repo / "backlog/archive/item-two.md").exists()
                and time.monotonic() < deadline
            ):
                assert process.poll() is None, output.read_text()
                time.sleep(0.2)
            assert (harness.repo / "backlog/archive/item-two.md").exists(), output.read_text()
            assert (harness.repo / "second.txt").read_text() == "done\n"
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    "scenario,success",
    [("legacy-correct", True), ("legacy-reject", False), ("legacy-stale-review", False)],
)
def test_s03_independent_review_gate(installed_python, tmp_path, scenario, success):
    harness = InstalledHarness.create(tmp_path, installed_python, scenario=scenario)
    result = harness.run("run-item", "item-one")
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    if success:
        assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    else:
        assert not (harness.repo / "answer.txt").exists()
        assert (
            "Status: Running" in (harness.repo / "backlog/feature-backlog/item-one.md").read_text()
        )
        assert "review" in result.stderr.lower()


def test_s05_uncertain_provider_effect_is_not_repeated(installed_python, tmp_path):
    from system_support import command

    harness = InstalledHarness.create(
        tmp_path, installed_python, scenario="legacy-crash-after-commit"
    )
    first = harness.run("run-item", "item-one")
    assert first.returncode == 2
    item = harness.repo / "backlog/feature-backlog/item-one.md"
    assert "Status: Starting" in item.read_text()
    head = command(["git", "-C", harness.repo, "rev-parse", "HEAD"]).stdout.strip()
    calls = (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    retry = harness.run("run-item", "item-one")
    assert retry.returncode == 2
    assert command(["git", "-C", harness.repo, "rev-parse", "HEAD"]).stdout.strip() == head
    after = (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    added = [json.loads(line)["prompt"] for line in after[len(calls) :]]
    assert all("Observe the authoritative file provider" in prompt for prompt in added)
    assert (
        sum(
            "Perform only this authorized provider transition" in json.loads(line)["prompt"]
            for line in after
        )
        == 1
    )
    assert not (harness.repo / "answer.txt").exists()


def _commit_fixture(harness):
    from system_support import command

    assert command(["git", "-C", harness.repo, "add", "."]).returncode == 0
    assert command(["git", "-C", harness.repo, "commit", "-m", "Queue conditions"]).returncode == 0


def _item(harness, identity, state="Ready", dependency=None):
    path = harness.repo / f"backlog/feature-backlog/{identity}.md"
    path.write_text(
        f"# {identity}\n\nWork Item ID: {identity}\nProvider: file\nStatus: {state}\nOwner: Unowned\nOriginal High Generated Tokens: 1000\n"
        + (f"Depends On: {dependency}\n" if dependency else "")
        + "\n## Objective\nBounded fixture.\n"
    )


def _final_output(result):
    return json.loads(result.stdout[result.stdout.rfind("\n{") + 1 :])


@pytest.mark.parametrize("condition", ["missing", "cycle", "failed", "holding", "excluded"])
def test_s01_blocked_dependencies_do_not_starve_independent(installed_python, tmp_path, condition):
    import yaml

    harness = InstalledHarness.create(tmp_path, installed_python)
    if condition == "missing":
        _item(harness, "dependent", dependency="absent")
    elif condition == "cycle":
        _item(harness, "dependent", dependency="cycle-peer")
        _item(harness, "cycle-peer", dependency="dependent")
    elif condition in ("failed", "holding"):
        _item(harness, "dependent", dependency="prerequisite")
        _item(harness, "prerequisite", state=condition.title())
    else:
        _item(harness, "excluded")
        data = yaml.safe_load(harness.config_path.read_text())
        data["excluded_items"] = {"excluded": "Explicitly outside this run"}
        harness.config_path.write_text(yaml.safe_dump(data))
    _commit_fixture(harness)
    # Candidate must include the current primary baseline, as it would when managed by the harness.
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    data = yaml.safe_load(harness.config_path.read_text())
    data["candidate_root"] = str(candidates)
    harness.config_path.write_text(yaml.safe_dump(data))
    result = harness.run("run", "--until-terminal")
    assert result.returncode == 2, result.stdout + result.stderr
    final = _final_output(result)
    assert final["outcome"] == "blocked"
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    prompts = [
        json.loads(line)["prompt"]
        for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    ]
    mutations = [
        json.loads(p[p.index("\n{") + 1 :])
        for p in prompts
        if "Perform only this authorized provider transition" in p
    ]
    assert [m["item"]["item_id"] for m in mutations] == ["item-one"] * 3
    if condition == "excluded":
        assert (
            final["items"]["excluded"]["reason"]
            == "Excluded from this run: Explicitly outside this run"
        )
    else:
        assert "Unmet dependencies:" in final["items"]["dependent"]["reason"]


def test_s01_terminal_nondelivery_is_not_success(installed_python, tmp_path):
    harness = InstalledHarness.create(tmp_path, installed_python)
    _item(harness, "item-one", state="Failed")
    _commit_fixture(harness)
    result = harness.run("run", "--until-terminal")
    assert result.returncode == 2
    assert _final_output(result)["outcome"] == "settled_with_nondelivery"
    prompts = [
        json.loads(line)["prompt"]
        for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    ]
    assert len(prompts) == 1 and "Observe the authoritative file provider" in prompts[0]


@pytest.mark.parametrize("excluded", [[], {"item-one": ""}, {"item-one": False}])
def test_s01_invalid_exclusion_configuration_blocks_before_agent(
    installed_python, tmp_path, excluded
):
    import yaml

    harness = InstalledHarness.create(tmp_path, installed_python)
    data = yaml.safe_load(harness.config_path.read_text())
    data["excluded_items"] = excluded
    harness.config_path.write_text(yaml.safe_dump(data))
    result = harness.run("validate")
    assert result.returncode == 2
    assert not (tmp_path / "agent-calls.jsonl").exists()


def test_s01_watch_blocked_has_no_repeated_agent_calls(installed_python, tmp_path):
    import signal
    import subprocess
    import time

    import yaml

    harness = InstalledHarness.create(tmp_path, installed_python)
    data = yaml.safe_load(harness.config_path.read_text())
    data["excluded_items"] = {"item-one": "Wait for authorized scope"}
    harness.config_path.write_text(yaml.safe_dump(data))
    observed = harness.run("refresh-provider")
    assert observed.returncode == 0, observed.stdout + observed.stderr
    calls = tmp_path / "agent-calls.jsonl"
    before = calls.read_bytes()
    output = tmp_path / "blocked-watch.log"
    with output.open("w") as stream:
        process = subprocess.Popen(
            [
                str(installed_python.parent / "agentic-harness"),
                "--config",
                str(harness.config_path),
                "run",
                "--watch",
            ],
            cwd=harness.repo,
            env=harness.env,
            stdout=stream,
            stderr=stream,
        )
        try:
            time.sleep(3)
            assert process.poll() is None, output.read_text()
            assert calls.read_bytes() == before
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_s05_terminal_commit_crash_recovers_without_repeat(installed_python, tmp_path):
    import os
    import signal
    import subprocess
    import time

    harness = InstalledHarness.create(
        tmp_path, installed_python, scenario="legacy-crash-after-terminal"
    )
    log = tmp_path / "crash.log"
    marker = tmp_path / "terminal-pause.json"
    native_stopped = False
    with log.open("w") as stream:
        process = subprocess.Popen(
            [
                str(installed_python.parent / "agentic-harness"),
                "--config",
                str(harness.config_path),
                "run-item",
                "item-one",
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
            # Wait for actual JSONL transport records to reach durable supervisor storage.
            event_paths = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                starts = [
                    path
                    for path in evidence.glob("provider-agent-operations/*/requested.json")
                    if json.loads(path.read_text())["target"] == "Starting"
                ]
                if starts:
                    request = json.loads(starts[0].read_text())
                    event_paths = [
                        path
                        for path in evidence.glob("runs/*/operations/*/invocations/*/events.jsonl")
                        if request["stage_operation"] in path.read_text()
                        and "turn.completed" in path.read_text()
                    ]
                    if event_paths:
                        break
                time.sleep(0.05)
            assert event_paths, "Terminal transport was not persisted before crash"
            process.kill()
            process.wait(timeout=5)
            os.killpg(json.loads(marker.read_text())["pid"], signal.SIGKILL)
            native_stopped = True
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if marker.exists() and not native_stopped:
                try:
                    os.killpg(json.loads(marker.read_text())["pid"], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    recovered = harness.run("reconcile")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    effects = [row for row in json.loads(recovered.stdout) if row.get("effect_verified")]
    assert len(effects) == 1 and effects[0]["receipt"]["after"]["state"] == "Starting"
    resumed = harness.run("run-item", "item-one")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)["delivery"]["verified"] is True
    calls = [
        json.loads(line)["prompt"]
        for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    ]
    transitions = [
        json.loads(p[p.index("\n{") + 1 :])
        for p in calls
        if "Perform only this authorized provider transition" in p
    ]
    assert [r["target"] for r in transitions] == ["Starting", "Running", "Completed"]
