"""Real subprocesses and Git worktrees in a separate, disposable test project."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backlog_harness.cli import Events
from backlog_harness.engine import Harness
from backlog_harness.process import Agents

FIXTURE = Path(__file__).parent / "fixtures" / "project_agent.py"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    control = tmp_path / "control"
    control.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Harness test project")
    (root / "fixture.json").write_text(json.dumps({"main": str(root), "control": str(control)}))
    (root / "check.py").write_text(
        "from pathlib import Path\n"
        "assert all(p.read_text() == 'verified' for p in Path('products').glob('*.txt'))\n"
    )
    (root / "README.md").write_text("Separate disposable harness test project.\n")
    (root / "backlog" / "ready").mkdir(parents=True)
    git(root, "add", ".")
    git(root, "commit", "-m", "Create separate test project")
    config = {
        "project": str(root),
        "state": str(tmp_path / "state"),
        **{
            role: [sys.executable, str(FIXTURE.resolve())]
            for role in ("access", "development", "merge")
        },
        "access_timeout": 3,
    }
    return root, control, config


def add(root, identity, **fields):
    path = root / "backlog" / "ready" / f"{identity}.json"
    path.write_text(json.dumps({"id": identity, "status": "ready", **fields}))
    git(root, "add", ".")
    git(root, "commit", "-m", f"Create {identity}")


async def until(predicate, timeout=10):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def execute(config, predicate, **options):
    events = Events(config["state"])
    agents = Agents(config, events)
    options.setdefault("timeout", 3)
    harness = Harness(
        agents,
        agents.deliver,
        agents.integrate,
        events,
        poll_interval=0.04,
        merge_interval=0.1,
        **options,
    )
    task = asyncio.create_task(harness.run())
    try:
        await until(lambda: predicate(harness) or task.done())
        if task.done() or (harness.epic and harness.complete and not harness.active):
            await asyncio.wait_for(task, 10)
        assert predicate(harness)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return harness


def test_parallel_delivery_dependencies_defects_and_merge(project):
    root, control, config = project
    add(root, "a", epic="release", mode="blocking_defect", commit_delay=0.25)
    add(root, "b", epic="release", dependencies=["a"])
    add(root, "outside", mode="unrelated_defect")

    async def scenario():
        h = await execute(config, lambda h: h.complete and not h.active, epic="release", capacity=2)
        assert h.complete

    asyncio.run(scenario())
    assert (root / "backlog/completed/a.json").exists()
    assert (root / "backlog/completed/b.json").exists()
    assert (root / "backlog/completed/a-defect.json").exists()
    # The unrelated item is published by its delivery merge; the harness can then select it.
    assert list((root / "backlog").glob("*/outside-defect.json"))
    entries = [
        json.loads(line)
        for line in (Path(config["state"]) / "activities.jsonl").read_text().splitlines()
    ]
    dispatched = [e["item"] for e in entries if e["event"] == "dispatched"]
    assert dispatched[:2] == ["a", "outside"]
    assert dispatched.count("a") == 1
    assert dispatched.count("b") == 1
    assert int((control / "a.attempts").read_text()) == 1
    subprocess.run([sys.executable, str(root / "check.py")], cwd=root, check=True)


@pytest.mark.parametrize("mode", ["interrupted", "restart", "transient"])
def test_real_process_recovery_and_retry(project, mode):
    root, control, config = project
    add(root, "one", mode=mode)
    asyncio.run(execute(config, lambda h: (root / "backlog/completed/one.json").exists()))
    workspace = root.parent / "worktrees/one"
    assert int((control / "one.attempts").read_text()) == 2
    if mode != "transient":
        assert (workspace / "unrelated.txt").read_text() == "preserve me"
        assert git(workspace, "branch", "--show-current") == "item/one"
    if mode == "interrupted":
        assert (workspace / "partial.txt").read_text() == "resumed"
    elif mode == "restart":
        assert not (workspace / "partial.txt").exists()


@pytest.mark.parametrize(
    "mode,folder", [("failure", "holding"), ("user_action", "user-action-required")]
)
def test_nondelivery_checkpoint_is_published_without_product_delivery(project, mode, folder):
    root, control, config = project
    add(root, "one", mode=mode)
    workspace = root.parent / "worktrees/one"
    target = workspace / "backlog" / folder / "one.json"
    asyncio.run(
        execute(
            config, lambda h: (root / "backlog" / folder / "one.json").exists() and not h.active
        )
    )
    assert not (root / "backlog/ready/one.json").exists()
    assert not (root / "products/one.txt").exists()
    assert int((control / "one.attempts").read_text()) == 1
    if mode == "user_action":
        assert "Human user:" in json.loads(target.read_text())["question"]


def test_failed_combined_tests_prevent_merge(project):
    root, control, config = project
    add(root, "one", mode="bad_merge")
    before = git(root, "rev-parse", "HEAD")
    asyncio.run(execute(config, lambda h: (control / "merge-rejected").exists()))
    assert git(root, "rev-parse", "HEAD") == before
    assert not (root / "products/one.txt").exists()
    assert (root / "backlog/ready/one.json").exists()


def test_actual_timeout_processes_are_reaped_before_hold(project):
    root, _, config = project
    add(root, "one", mode="timeout")
    target = root.parent / "worktrees/one/backlog/holding/one.json"
    asyncio.run(execute(config, lambda h: target.exists() and not h.active, timeout=0.12))
    events = [
        json.loads(line)
        for line in (Path(config["state"]) / "activities.jsonl").read_text().splitlines()
    ]
    started = [e for e in events if e["event"] == "agent_started" and e["role"] == "development"]
    stopped = {e["invocation"] for e in events if e["event"] == "agent_group_stopped"}
    assert len(started) == 3
    timeouts = [e for e in events if e["event"] == "development_timeout"]
    assert {e["invocation"] for e in timeouts} == {e["invocation"] for e in started}
    assert all(e["role"] == "development" and e["item"] == "one" for e in timeouts)
    assert all(e["invocation"] in stopped for e in started)
    assert len(json.loads(target.read_text())["failures"]) == 3


def test_foreground_cli_pause_resume_and_epic_exit(project):
    import yaml

    root, _, config = project
    add(root, "one", epic="release")
    config["scheduling"] = {"poll_interval": 0.03, "merge_interval": 0.05}
    path = root.parent / "config.yaml"
    path.write_text(yaml.safe_dump(config))

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backlog_harness",
            "--config",
            str(path),
            "--epic",
            "release",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        events = []

        async def collect():
            while line := await process.stdout.readline():
                events.append(json.loads(line))

        reader = asyncio.create_task(collect())
        try:
            process.stdin.write(b"pause\n")
            await process.stdin.drain()
            await until(lambda: any(e["event"] == "paused" for e in events))
            await asyncio.sleep(0.1)
            assert not any(e["event"] == "dispatched" for e in events)
            process.stdin.write(b"unknown\nresume\n")
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 10)
            await reader
            assert process.returncode == 0, (await process.stderr.read()).decode()
            assert any(e["event"] == "unknown_control" for e in events)
            assert any(e["event"] == "resumed" for e in events)
            assert (root / "backlog/completed/one.json").exists()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await reader

    asyncio.run(scenario())


def test_cli_interrupt_reaps_agent_group_and_preserves_worktree(project):
    import os
    import signal

    import yaml

    root, _, config = project
    add(root, "one", mode="timeout")
    path = root.parent / "config.yaml"
    path.write_text(yaml.safe_dump(config))

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backlog_harness",
            "--config",
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        events = []

        async def collect():
            while line := await process.stdout.readline():
                events.append(json.loads(line))

        reader = asyncio.create_task(collect())
        try:
            await until(
                lambda: any(
                    e["event"] == "agent_output" and "Delivering one" in e["text"] for e in events
                )
            )
            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), 10)
            await reader
            assert process.returncode == 0, (await process.stderr.read()).decode()
            assert any(e["event"] == "exiting" for e in events)
            pid = next(
                e["pid"]
                for e in events
                if e["event"] == "agent_started" and e["role"] == "development"
            )
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert (root.parent / "worktrees/one/backlog/running/one.json").exists()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await reader

    asyncio.run(scenario())


def test_provider_reconciles_legacy_candidate_and_excludes_unpublished_work(project):
    """Provider double owns reconciliation; harness only dispatches its returned item."""
    root, control, config = project
    add(root, "legacy", interrupted_candidate=True)
    add(root, "live")
    add(root, "unreviewed", approved=False)
    legacy_record = root / "backlog/ready/legacy.json"
    item = json.loads(legacy_record.read_text())
    item["status"] = "running"
    (root / "backlog/running").mkdir()
    legacy_record.unlink()
    (root / "backlog/running/legacy.json").write_text(json.dumps(item))
    git(root, "add", "backlog")
    git(root, "commit", "-m", "Record legacy assignment")
    worktrees = root.parent / "worktrees"
    worktrees.mkdir()
    for identity, status in [
        ("legacy", "running"),
        ("live", "running"),
        ("unreviewed", "awaiting-merge"),
    ]:
        workspace = worktrees / identity
        git(root, "worktree", "add", "-b", "item/" + identity, str(workspace), "main")
        if identity != "legacy":
            old = workspace / f"backlog/ready/{identity}.json"
            record = json.loads(old.read_text())
            record["status"] = status
            old.unlink()
            target = workspace / f"backlog/{status}/{identity}.json"
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(record))
        (workspace / f"{identity}-candidate.txt").write_text("preserve candidate")
        git(workspace, "add", ".")
        git(workspace, "commit", "-m", f"Preserve {identity} candidate")
    legacy = worktrees / "legacy"
    candidate = git(legacy, "rev-parse", "HEAD")
    (legacy / "unrelated.txt").write_text("keep local")
    other_heads = {i: git(worktrees / i, "rev-parse", "HEAD") for i in ("live", "unreviewed")}
    asyncio.run(
        execute(
            config,
            lambda h: (
                "backlog/completed/legacy.json"
                in git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
            ),
        )
    )
    git(root, "merge-base", "--is-ancestor", candidate, "HEAD")
    assert (root / "legacy-candidate.txt").read_text() == "preserve candidate"
    assert (legacy / "unrelated.txt").read_text() == "keep local"
    assert not (root / "unrelated.txt").exists()
    assert (control / "legacy.attempts").read_text() == "1"
    for identity, revision in other_heads.items():
        assert not (control / f"{identity}.attempts").exists()
        assert git(worktrees / identity, "rev-parse", "HEAD") == revision
        assert not (root / f"{identity}-candidate.txt").exists()


@pytest.mark.parametrize("main_change", ["unchanged", "completed", "answer", "conflict"])
def test_waiting_reconciliation_preserves_authority_and_is_idempotent(project, main_change):
    root, _control, config = project
    add(root, "one")
    workspace = root.parent / "worktrees" / "one"
    workspace.parent.mkdir()
    git(root, "worktree", "add", "-b", "item/one", str(workspace), "main")
    waiting = {
        "id": "one",
        "status": "user-action-required",
        "question": "Enable preview?",
        "candidate": "preserved-candidate",
        "decision_id": "pending-1",
    }
    (workspace / "backlog/ready/one.json").unlink()
    destination = workspace / "backlog/user-action-required/one.json"
    destination.parent.mkdir()
    destination.write_text(json.dumps(waiting))
    (workspace / "unfinished.txt").write_text("unfinished product change")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "Record waiting checkpoint with unfinished product")
    candidate = git(workspace, "rev-parse", "HEAD")
    if main_change != "unchanged":
        p = root / "backlog/ready/one.json"
        data = json.loads(p.read_text())
        data.update(
            {
                "completed": {"status": "completed"},
                "answer": {"answer": "Approved", "decision_id": "new-answer"},
                "conflict": {"question": "Different question", "revision": "competing"},
            }[main_change]
        )
        if main_change == "completed":
            p.unlink()
            p = root / "backlog/completed/one.json"
            p.parent.mkdir()
        p.write_text(json.dumps(data))
        git(root, "add", "backlog")
        git(root, "commit", "-m", "Record newer authoritative main change")
    before = git(root, "rev-parse", "HEAD")
    unrelated = root / "unrelated.txt"
    unrelated.write_text("preserve me")
    git(root, "add", "unrelated.txt")
    staged_before = git(root, "diff", "--cached", "--", "unrelated.txt")

    async def scan():
        # Fresh provider instance has no remembered workspace mapping.
        agents = Agents(config, lambda *_args, **_fields: None)
        assert await agents.ready(1, None, False, []) == []

    asyncio.run(scan())
    after = git(root, "rev-parse", "HEAD")
    if main_change == "unchanged":
        assert after != before
        assert json.loads((root / "backlog/user-action-required/one.json").read_text()) == waiting
        assert not (root / "backlog/ready/one.json").exists()
    else:
        assert after == before
    assert not (root / "unfinished.txt").exists()
    assert git(workspace, "rev-parse", "HEAD") == candidate
    assert git(root, "diff", "--cached", "--", "unrelated.txt") == staged_before
    asyncio.run(scan())
    assert git(root, "rev-parse", "HEAD") == after


@pytest.mark.skipif(os.environ.get("RUN_CODEX_LIVE") != "1", reason="Authenticated live model test")
def test_live_access_reconciles_waiting_without_product_merge(project):
    root, _control, config = project
    (root / "AGENTS.md").write_text("""# Isolated lifecycle publication test project
The file work-item provider is JSON files under backlog/<status>/<id>.json.
The configured primary branch is main and this is its primary checkout.
Use Git history and registered worktrees to reconcile item records. Assigned branch
item/<id> and sibling worktrees/<id> are the canonical assignments. Do not implement
product work, run project_agent.py, or mutate other projects. You own main-side
lifecycle publication for this request. Commit only owned record paths and preserve
unrelated staged data. Status and folder must agree. There is no claim service.
Before each write re-read the primary item and confirm it has not changed.
Questions, decision IDs, candidates and history are opaque evidence to preserve.
A newer main answer with unresolved competing question evidence must not be
replaced or dispatched: report the conflict. Completed main items stay completed.
Verify committed readback. Do not append unchanged scan notes. Product acceptance
is independent of lifecycle publication; never merge unfinished product files.
""")
    git(root, "add", "AGENTS.md")
    git(root, "commit", "-m", "Define isolated provider conventions")
    for name in ["waiting", "completed", "conflict", "answered"]:
        add(root, name)
        workspace = root.parent / "worktrees" / name
        workspace.parent.mkdir(exist_ok=True)
        git(root, "worktree", "add", "-b", "item/" + name, str(workspace), "main")
        p = workspace / "backlog/ready" / (name + ".json")
        record = json.loads(p.read_text())
        record.update(
            status="user-action-required",
            question="Enable preview?",
            candidate="candidate-" + name,
            decision_id="pending-" + name,
            owner="Human user",
            next_action="Answer the preview question",
            waiting_since="2026-10-04T10:00:00Z",
        )
        p.unlink()
        p = workspace / "backlog/user-action-required" / (name + ".json")
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(record))
        (workspace / "unfinished.txt").write_text("Never publish this product file")
        git(workspace, "add", ".")
        git(workspace, "commit", "-m", "Waiting checkpoint")
        if name != "waiting":
            p = root / "backlog/ready" / (name + ".json")
            current = json.loads(p.read_text())
            if name == "completed":
                current["status"] = "completed"
                p.unlink()
                p = root / "backlog/completed" / (name + ".json")
                p.parent.mkdir(exist_ok=True)
            elif name == "answered":
                current.update(
                    answer="Use another endpoint",
                    decision_id="newer-answer",
                    question="Which endpoint?",
                )
            else:
                current["question"] = "A different pending question"
            p.write_text(json.dumps(current))
            git(root, "add", "backlog")
            git(root, "commit", "-m", "Newer authoritative main record")
    before = {p.relative_to(root): p.read_bytes() for p in (root / "backlog").glob("*/*.json")}
    heads = {p.name: git(p, "rev-parse", "HEAD") for p in (root.parent / "worktrees").iterdir()}
    (root / "unrelated.txt").write_text("Preserve staged data")
    git(root, "add", "unrelated.txt")
    index = git(root, "diff", "--cached")
    config["access"] = [
        sys.executable,
        "-m",
        "backlog_harness.codex",
        "--model",
        "gpt-6-luna",
        "--effort",
        "high",
    ]
    config["access_timeout"] = 600

    async def scan():
        agents = Agents(config, lambda *_args, **_fields: None)
        assert await agents.ready(1, None, False, []) == []

    asyncio.run(scan())
    waiting = root / "backlog/user-action-required/waiting.json"
    expected = root.parent / "worktrees/waiting/backlog/user-action-required/waiting.json"
    assert json.loads(waiting.read_text()) == json.loads(expected.read_text())
    assert not (root / "backlog/ready/waiting.json").exists()
    for path, data in before.items():
        if path.name != "waiting.json":
            assert (root / path).read_bytes() == data
    assert not (root / "unfinished.txt").exists()
    assert git(root, "diff", "--cached") == index
    for name, head in heads.items():
        assert git(root.parent / "worktrees" / name, "rev-parse", "HEAD") == head
    published = git(root, "rev-parse", "HEAD")
    asyncio.run(scan())
    assert git(root, "rev-parse", "HEAD") == published
    assert git(root, "diff", "--cached") == index
