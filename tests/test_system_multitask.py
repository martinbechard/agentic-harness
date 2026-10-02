"""Installed MULTITASK queue: two real overlapping native subprocesses, disjoint scopes."""

import json
from pathlib import Path

import yaml
from system_support import InstalledHarness, command


def test_installed_multitask_two_independent_items(installed_package_python, tmp_path):
    python = installed_package_python
    harness = InstalledHarness.create(
        tmp_path, python, dispatcher=Path(__file__).parent / "fixtures/multitask_agent.py"
    )
    project = harness.repo / "PROJECT.yaml"
    project.write_text(
        project.read_text().replace("execution_mode: SOLO", "execution_mode: MULTITASK")
        + "project_setup: {concurrent_tasking: true}\n"
    )
    second = harness.repo / "backlog/feature-backlog/item-two.md"
    second.write_text(
        "# Two\n\nWork Item ID: item-two\nProvider: file\nStatus: Ready\nOwner: Unowned\nOriginal High Generated Tokens: 1000\n\n## Objective\nWrite independent second.txt.\n"
    )
    for arguments in [("add", "."), ("commit", "-m", "Authorize independent concurrent work")]:
        result = command(["git", "-C", harness.repo, *arguments])
        assert result.returncode == 0, result.stderr
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    config = yaml.safe_load(harness.config_path.read_text())
    config.update(
        candidate_root=str(candidates), max_active_invocations=2, invocation_timeout_seconds=30
    )
    config["workflow"]["mode"] = "MULTITASK"
    config["workflow"]["items"] = {
        item: {
            "allowed_paths": [name],
            "checks": [
                [
                    str(python),
                    "-c",
                    f"from pathlib import Path; assert Path({name!r}).read_text() == 'done\\n'",
                ]
            ],
        }
        for item, name in [("item-one", "answer.txt"), ("item-two", "second.txt")]
    }
    harness.config_path.write_text(yaml.safe_dump(config))
    result = harness.run("run", "--until-terminal", timeout=90)
    (tmp_path / "run.stdout").write_text(result.stdout)
    (tmp_path / "run.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    final = json.loads(result.stdout[result.stdout.rfind("\n{") + 1 :])
    assert final == {"outcome": "successful", "counts": {"Completed": 2}}
    marks = [
        json.loads(path.read_text()) for path in sorted((tmp_path / "overlap").glob("item-*.json"))
    ]
    assert len(marks) == 2
    assert (
        len({m["cwd"] for m in marks})
        == len({m["session"] for m in marks})
        == len({m["pid"] for m in marks})
        == 2
    )
    assert max(m["started"] for m in marks) < min(m["finished"] for m in marks)
    assert all(Path(m["cwd"]).parent == candidates for m in marks)
    calls = [
        json.loads(line)["prompt"]
        for line in (tmp_path / "agent-calls.jsonl").read_text().splitlines()
    ]
    assert sum("Running is now recorded for your exact session" in p for p in calls) == 2
    mutations = [
        json.loads(p[p.index("\n{") + 1 :])
        for p in calls
        if "Perform only this authorized provider transition" in p
    ]
    for item, name in [("item-one", "answer.txt"), ("item-two", "second.txt")]:
        assert [m["target"] for m in mutations if m["item"]["item_id"] == item] == [
            "Starting",
            "Running",
            "Completed",
        ]
        assert (harness.repo / name).read_text() == "done\n"
        assert "Status: Completed" in (harness.repo / f"backlog/archive/{item}.md").read_text()
    deliveries = list(
        harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/delivery.json")
    )
    assert len(deliveries) == 2
    for path in deliveries:
        receipt = json.loads(path.read_text())
        assert (
            receipt["verified"]
            and receipt["review"]["native_verified"]
            and receipt["review"]["fresh_context"]
        )
        assert receipt["candidate"] == receipt["review"]["candidate"]
        assert receipt["review"]["producer_session"] != receipt["review"]["reviewer_session"]
        assert receipt["checks"] and receipt["integrated_checks"]
        assert (
            command(
                [
                    "git",
                    "-C",
                    harness.repo,
                    "merge-base",
                    "--is-ancestor",
                    receipt["main_commit"],
                    "HEAD",
                ]
            ).returncode
            == 0
        )
    closures = [
        json.loads(p.read_text())
        for p in harness.repo.glob(
            ".agent-ops/backlog-harness/provider-agent-operations/*/receipt.json"
        )
    ]
    completed = [r for r in closures if r["after"]["state"] == "Completed"]
    assert len(completed) == 2 and all(r["advancement_verified"] for r in completed)
