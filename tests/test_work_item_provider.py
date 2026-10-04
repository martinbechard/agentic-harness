"""Count-only admission checks never select, mutate, or start an agent themselves."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backlog_harness.work_item_provider import (
    FileWorkItemProvider,
    GitHubWorkItemProvider,
    configured_provider,
)


def record(root, name, status=None, body=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Work\n\n{('Status: ' + status) if status else ''}\n\n## Summary\n{body}")
    return path


def count(provider, epic=None, outside=False, excluded=None):
    return asyncio.run(provider.ready_count(epic=epic, outside=outside, excluded=excluded or []))


def test_file_status_folders_scope_exclusions_and_read_only(tmp_path):
    root = tmp_path / "backlog/ready"
    record(root, "one.md")
    record(root, "epic/two.md", "Ready")
    record(root, "epic/index.md")
    record(root, "README.md")
    before = {p: p.read_bytes() for p in root.rglob("*.md")}
    provider = FileWorkItemProvider(tmp_path, ["backlog/ready"], "status-folders")
    assert count(provider) == 2
    assert count(provider, "backlog/ready/epic") == 1
    assert count(provider, "backlog/ready/epic", outside=True) == 1
    assert count(provider, excluded=["one", "two"]) == 0
    assert before == {p: p.read_bytes() for p in root.rglob("*.md")}
    record(root, "contradiction.md", "Running")
    with pytest.raises(ValueError, match="disagree"):
        count(provider)


def test_file_legacy_status_and_overlapping_paths(tmp_path):
    root = tmp_path / "backlog/features"
    record(root, "one.md", "Ready")
    record(root, "epic/two.md", "Ready")
    for status in (
        "Running",
        "Starting",
        "Holding",
        "Blocked",
        "User Action Required",
        "Completed",
    ):
        record(root, f"{status}.md", status, "Status: Ready\n")
    record(root, "unmarked.md", body="Status: Ready\n")
    record(tmp_path / "backlog/future-ideas", "future.md", "Ready")
    provider = FileWorkItemProvider(
        tmp_path, ["backlog/features", "backlog/features/epic"], "status-field"
    )
    assert count(provider) == 2
    assert count(provider, excluded=["one"]) == 1


def test_missing_directory_is_not_empty(tmp_path):
    provider = FileWorkItemProvider(tmp_path, ["missing"], "status-folders")
    with pytest.raises(ValueError, match="does not exist"):
        count(provider)


def test_empty_directory_and_unreadable_record(tmp_path, monkeypatch):
    provider = FileWorkItemProvider(tmp_path, ["."], "status-folders")
    assert count(provider) == 0
    record(tmp_path, "one.md")
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("read")))
    with pytest.raises(OSError, match="read"):
        count(provider)


@pytest.mark.parametrize(
    "settings",
    [
        [],
        {},
        {"type": []},
        {"type": "unknown"},
        {"type": "agent", "paths": []},
        {"type": "file", "timeout": 0},
        {"type": "file", "timeout": True},
        {"type": "file", "layout": "unknown"},
        {"type": "file", "paths": []},
        {"type": "file", "paths": "backlog"},
        {"type": "file", "paths": [""]},
        {"type": "github"},
        {"type": "github", "repository": "owner/repo;command"},
        {"type": "github", "repository": "owner/repo", "ready_label": ""},
        {"type": "github", "repository": "owner/repo", "ready_label": "a,b"},
        {"type": "github", "repository": "owner/repo", "ready_label": "Ready", "executable": ""},
    ],
)
def test_invalid_provider_configuration(tmp_path, settings):
    with pytest.raises(ValueError):
        configured_provider({"project": str(tmp_path), "work_item_provider": settings})


def test_explicit_optional_provider_configuration(tmp_path):
    config = {"project": str(tmp_path)}
    assert configured_provider(config) is None
    assert configured_provider({**config, "work_item_provider": {"type": "agent"}}) is None
    file = configured_provider({**config, "work_item_provider": {"type": "file"}})
    assert file.paths == [tmp_path / "backlog/ready"]
    github = configured_provider(
        {
            **config,
            "work_item_provider": {
                "type": "github",
                "repository": "owner/repo",
                "ready_label": "Status: Ready",
            },
        }
    )
    assert github.repository == "owner/repo"
    assert github.executable == "gh"


class GitHubProcess:
    def __init__(self, output, code=0):
        self.returncode = code
        self.communicate = AsyncMock(return_value=(json.dumps(output).encode(), b"unavailable"))
        self.wait = AsyncMock()


@pytest.mark.parametrize(
    "epic,outside,excluded,expected",
    [
        (None, False, [], 3),
        ("Release", False, [], 1),
        ("Release", True, [], 2),
        (None, False, ["1", "#2", "owner/repo#3"], 0),
        (None, False, ["https://github.com/owner/repo/issues/1"], 2),
    ],
)
def test_github_all_pages_exclude_prs_and_scope(
    tmp_path, monkeypatch, epic, outside, excluded, expected
):
    process = GitHubProcess(
        [
            [
                {
                    "number": 1,
                    "milestone": {"title": "Release"},
                    "html_url": "https://github.com/owner/repo/issues/1",
                },
                {"number": 9, "pull_request": {}},
            ],
            [
                {"number": 2, "milestone": None},
                {"number": 3},
                {
                    "number": 1,
                    "milestone": {"title": "Release"},
                    "html_url": "https://github.com/owner/repo/issues/1",
                },
            ],
        ]
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    provider = GitHubWorkItemProvider(str(tmp_path), "owner/repo", "Status: Ready", "gh")
    assert count(provider, epic, outside, excluded) == expected
    args = spawn.call_args.args
    assert args == (
        "gh",
        "api",
        "repos/owner/repo/issues?state=open&labels=Status%3A+Ready&per_page=100",
        "--paginate",
        "--slurp",
    )
    process.wait.assert_awaited_once()


@pytest.mark.parametrize(
    "output,code,error",
    [
        ([], 1, RuntimeError),
        ({}, 0, ValueError),
        ([{}], 0, ValueError),
        ([[{}]], 0, ValueError),
        ([[{"number": True}]], 0, ValueError),
        ([[{"number": 1, "html_url": []}]], 0, ValueError),
        ([[{"number": 1, "milestone": "wrong"}]], 0, ValueError),
    ],
)
def test_github_errors_are_not_zero(tmp_path, monkeypatch, output, code, error):
    process = GitHubProcess(output, code)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(error):
        count(GitHubWorkItemProvider(str(tmp_path), "o/r", "ready", "gh"))
    process.wait.assert_awaited_once()


def test_github_timeout_reaps_real_process(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        child = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(child)
        return child

    async def run():
        provider = GitHubWorkItemProvider(str(tmp_path), "o/r", "ready", "gh")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider.ready_count(epic=None, outside=False, excluded=[]), 0.1)
        assert children[0].returncode is not None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    asyncio.run(run())


def test_configured_cli_skips_empty_polls_and_observes_new_ready_file(tmp_path):
    import signal

    import yaml

    from backlog_harness.cli import load_config

    ready = tmp_path / "backlog/ready"
    ready.mkdir(parents=True)
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "r=json.loads(Path(os.environ['HARNESS_REQUEST']).read_text())\n"
        "assert r['work_item_provider'] == {'type': 'file'}\n"
        "out={'items': []} if r['role']=='access' else {'status': 'success'}\n"
        "Path(os.environ['HARNESS_RESULT']).write_text(json.dumps(out))\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "project": str(tmp_path),
                "state": str(tmp_path / "state"),
                **{
                    role: [sys.executable, str(script)]
                    for role in ("access", "development", "merge")
                },
                "thread_cleanup": {"enabled": False},
                "scheduling": {"poll_interval": 0.03, "merge_interval": 60},
                "work_item_provider": {"type": "file"},
            }
        )
    )
    assert load_config(path)["work_item_provider"]["type"] == "file"

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

        async def until(predicate):
            async with asyncio.timeout(10):
                while not predicate():
                    await asyncio.sleep(0.01)

        reader = asyncio.create_task(collect())
        try:
            await until(lambda: sum(e["event"] == "ready_count" for e in events) >= 2)
            assert not any(e.get("action") == "ready" for e in events)
            record(ready, "new.md", "Ready")
            await until(lambda: any(e["event"] == "provider_response" for e in events))
            assert any(e["event"] == "ready_count" and e["count"] == 1 for e in events)
            assert any(e["event"] == "agent_started" and e["role"] == "access" for e in events)
            assert not any(e["event"] == "dispatched" for e in events)
            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), 10)
            assert process.returncode == 0, (await process.stderr.read()).decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await reader

    asyncio.run(scenario())


def test_file_directory_scan_error_is_not_zero(tmp_path, monkeypatch):
    import os

    def denied(*args):
        raise PermissionError("cannot scan queue")

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(PermissionError, match="cannot scan queue"):
        count(FileWorkItemProvider(tmp_path, ["."], "status-folders"))


def test_file_ignores_non_markdown_and_trims_status_whitespace(tmp_path):
    (tmp_path / "note.txt").write_text("Status: Ready")
    record(tmp_path, "ready.md", "Ready  ")
    assert count(FileWorkItemProvider(tmp_path, ["."], "status-field")) == 1
