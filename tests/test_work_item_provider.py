"""Count-only admission checks never select, mutate, or start an agent themselves."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest

from backlog_harness.work_item_provider import (
    FileWorkItemProvider,
    GitHubWorkItemProvider,
    configured_provider,
)


def count(provider, epic=None, outside=False, excluded=None):
    return asyncio.run(provider.ready_count(epic=epic, outside=outside, excluded=excluded or []))


def file_provider(tmp_path, source):
    script = tmp_path / "provider.py"
    script.write_text(source)
    return FileWorkItemProvider(
        str(tmp_path), ["backlog/features"], "status-field", [sys.executable, str(script)]
    )


def test_file_provider_calls_shared_eligibility_observer_with_exact_scope(tmp_path):
    provider = file_provider(
        tmp_path,
        "import json,sys\n"
        "from pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        "assert request == {'epic':'release','outside':True,'excluded':['one'],"
        "'paths':['backlog/features'],'layout':'status-field'}\n"
        "assert Path.cwd().name == " + repr(tmp_path.name) + "\n"
        "print(json.dumps({'ready_count':2}))\n",
    )
    assert count(provider, "release", True, ["one"]) == 2


@pytest.mark.parametrize(
    "output",
    [
        "invalid JSON",
        "[]",
        "{}",
        '{"ready_count":true}',
        '{"ready_count":-1}',
        '{"ready_count":1.5}',
        '{"ready_count":"1"}',
        '{"ready_count":1,"items":[]}',
    ],
)
def test_invalid_file_provider_response_is_not_a_count(tmp_path, output):
    provider = file_provider(tmp_path, "print(" + repr(output) + ")\n")
    with pytest.raises(ValueError):
        count(provider)


def test_file_provider_failure_is_not_empty_or_agent_fallback(tmp_path):
    provider = file_provider(
        tmp_path, "import sys\nprint('queue unavailable',file=sys.stderr)\nsys.exit(2)\n"
    )
    with pytest.raises(RuntimeError, match="queue unavailable"):
        count(provider)


def test_file_provider_timeout_reaps_process(tmp_path, monkeypatch):
    import os
    import signal

    provider = file_provider(tmp_path, "import time; time.sleep(60)\n")
    original = os.killpg
    stopped = []

    def stop(pid, sig):
        stopped.append((pid, sig))
        original(pid, sig)

    async def run():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider.ready_count(epic=None, outside=False, excluded=[]), 0.1)
        assert stopped and stopped[0][1] == signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(stopped[0][0], 0)

    monkeypatch.setattr(os, "killpg", stop)
    asyncio.run(run())


@pytest.mark.parametrize(
    "settings",
    [
        [],
        {},
        {"type": []},
        {"type": "unknown"},
        {"type": "agent", "paths": []},
        {"type": "file"},
        {"type": "file", "command": []},
        {"type": "file", "command": "shell command"},
        {"type": "file", "command": [None]},
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
    file = configured_provider(
        {**config, "work_item_provider": {"type": "file", "command": ["provider"]}}
    )
    assert file.paths == ["backlog/ready"]
    assert file.command == ["provider"]
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


def test_configured_cli_skips_six_ineligible_records_then_observes_eligible_work(tmp_path):
    import signal

    import yaml

    from backlog_harness.cli import load_config

    # The real provider's eligibility resolver has its own tests. This fixture exposes
    # its observation, including the regression: six stored Ready records, none eligible.
    inventory = tmp_path / "inventory.json"
    records = [{"stored_state": "Ready", "eligible": False} for _ in range(6)]
    inventory.write_text(json.dumps(records))
    provider_script = tmp_path / "count.py"
    provider_script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        "records=json.loads(Path('inventory.json').read_text())\n"
        "print(json.dumps({'ready_count':sum(r['eligible'] for r in records)}))\n"
    )
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "r=json.loads(Path(os.environ['HARNESS_REQUEST']).read_text())\n"
        "assert r['work_item_provider']['type'] == 'file'\n"
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
                "work_item_provider": {
                    "type": "file",
                    "command": [sys.executable, str(provider_script)],
                },
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
            records[0]["eligible"] = True
            inventory.write_text(json.dumps(records))
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


def test_file_provider_reaps_group_when_parent_exits_with_child_holding_output(tmp_path):
    import subprocess

    provider = file_provider(
        tmp_path,
        "import subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "Path('child.pid').write_text(str(child.pid))\n"
        "print('{\"ready_count\":0}')\n",
    )

    async def run():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider.ready_count(epic=None, outside=False, excluded=[]), 0.5)

    asyncio.run(run())
    child = (tmp_path / "child.pid").read_text()
    # A dead orphan can briefly remain as a zombie until the OS reaps it.
    status = subprocess.run(
        ["ps", "-p", child, "-o", "stat="], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert not status or status.startswith("Z")


def test_file_blocked_count_uses_explicit_operation_and_blocked_scope(tmp_path):
    provider = file_provider(
        tmp_path,
        "import json,sys\n"
        "r=json.load(sys.stdin)\n"
        "assert r == {'action':'blocked_count','epic':'release','outside':False,"
        "'excluded':['one'],'paths':['backlog/features'],'layout':'status-field'}\n"
        "print(json.dumps({'blocked_count':3}))\n",
    )
    assert asyncio.run(provider.blocked_count(epic="release", excluded=["one"])) == 3
    folders = FileWorkItemProvider(str(tmp_path), ["backlog/ready"], "status-folders", ["x"])
    assert folders.blocked_paths == ["backlog/blocked"]


def test_old_ready_only_observer_cannot_masquerade_as_blocked_count(tmp_path):
    provider = file_provider(tmp_path, "print('{\"ready_count\":2}')")
    with pytest.raises(ValueError, match="blocked_count"):
        asyncio.run(provider.blocked_count(epic=None, excluded=[]))


def test_github_blocked_count_uses_explicit_label_and_existing_filters(tmp_path, monkeypatch):
    process = GitHubProcess(
        [
            [
                {"number": 1, "milestone": {"title": "release"}},
                {"number": 2, "pull_request": {}},
                {"number": 3, "milestone": {"title": "other"}},
            ]
        ]
    )
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    provider = GitHubWorkItemProvider(str(tmp_path), "owner/repo", "Ready", "gh", "Blocked")
    assert asyncio.run(provider.blocked_count(epic="release", excluded=[])) == 1
    assert "labels=Blocked" in launch.call_args.args[2]
    provider.blocked_label = None
    with pytest.raises(ValueError, match="blocked_label"):
        asyncio.run(provider.blocked_count(epic=None, excluded=[]))


@pytest.mark.parametrize(
    "kind,key,value",
    [
        ("file", "blocked_paths", []),
        ("file", "blocked_paths", ""),
        ("file", "blocked_paths", [1]),
        ("github", "blocked_label", ""),
        ("github", "blocked_label", "a,b"),
        ("github", "blocked_label", False),
    ],
)
def test_invalid_blocked_provider_settings(kind, key, value, tmp_path):
    settings = {"type": kind, key: value}
    if kind == "file":
        settings["command"] = ["provider"]
    else:
        settings.update(repository="owner/repo", ready_label="Ready")
    with pytest.raises(ValueError, match=key):
        configured_provider({"project": str(tmp_path), "work_item_provider": settings})
