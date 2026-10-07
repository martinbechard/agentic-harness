"""Mechanical cleanup eligibility, pagination, failure isolation, and scheduling."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from openai_codex.generated.v2_all import AbsolutePathBuf

from backlog_harness import codex


def thread(**changes):
    return {
        "id": "old",
        "cwd": "/project",
        "updatedAt": 10,
        "status": {"type": "notLoaded"},
        "turns": [{"status": "completed", "completedAt": 10}],
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"cwd": "/other"},
        {"section": {"id": codex.PINNED_SECTION_ID, "name": "Pinned"}},
        {"updatedAt": 100},
        {"turns": []},
        {"status": {"type": "active"}},
        {"status": {"type": "systemError"}},
        {"turns": [{"status": "inProgress", "completedAt": None}]},
        {"turns": [{"status": "completed", "completedAt": None}]},
        {"turns": [{"status": "completed", "completedAt": 100}]},
    ],
)
def test_ineligible_threads_are_preserved(changes):
    assert not codex.completed_before(thread(**changes), "/project", 100)


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_finished_thread_is_eligible(status):
    assert codex.completed_before(
        thread(turns=[{"status": status, "completedAt": 10}]), "/project", 100
    )


@pytest.mark.parametrize("ui_failure", [False, True])
def test_paginated_cleanup_rechecks_and_continues_after_failure(monkeypatch, ui_failure):
    calls, events = [], []
    reads = {}

    class Client:
        def __init__(self, config):
            assert config.cwd == "/project"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("closed")

        async def initialize(self):
            pass

        async def thread_list(self, params):
            calls.append("list")
            assert "cwd" not in params
            if params["archived"]:
                return SimpleNamespace(
                    data=[
                        SimpleNamespace(
                            id="previous" if params["cursor"] is None else "old",
                            cwd=AbsolutePathBuf(root="/project"),
                        ),
                        SimpleNamespace(id="foreign-archive", cwd=AbsolutePathBuf(root="/other")),
                    ],
                    next_cursor="archive-next" if params["cursor"] is None else None,
                )
            ids = (
                ["old", "resumed", "failed"]
                if params["cursor"] is None
                else ["empty", "recent", "failed-run", "worktree", "foreign", "pinned", "pin-race"]
            )
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id=i,
                        cwd=AbsolutePathBuf(
                            root=(
                                "/other"
                                if i == "foreign"
                                else "/project/.worktrees/fix"
                                if i == "worktree"
                                else "/project"
                            )
                        ),
                        updated_at=100 if i == "recent" else 10,
                    )
                    for i in ids
                ],
                next_cursor="next" if params["cursor"] is None else None,
            )

        async def thread_read(self, identity, include_turns):
            assert include_turns
            reads[identity] = reads.get(identity, 0) + 1
            data = thread(id=identity)
            if identity == "pinned" or (identity == "pin-race" and reads[identity] == 2):
                data["section"] = {"id": codex.PINNED_SECTION_ID, "name": "Pinned"}
            if identity == "failed-run":
                data["turns"][-1]["status"] = "failed"
            if identity == "worktree":
                data["cwd"] = "/project/.worktrees/fix"
            if identity == "empty":
                data["turns"] = []
            if identity == "resumed" and reads[identity] == 2:
                data["turns"].append({"status": "inProgress", "completedAt": None})
            return SimpleNamespace(thread=SimpleNamespace(model_dump=lambda **kw: deepcopy(data)))

        async def thread_archive(self, identity):
            calls.append(identity)
            if identity == "failed":
                raise RuntimeError("archive unavailable")

    async def notify(threads):
        assert threads == [{"id": "previous", "cwd": "/project"}, {"id": "old", "cwd": "/project"}]
        calls.append("notify")
        if ui_failure:
            raise OSError("app closed")

    monkeypatch.setattr(codex, "notify_archived", notify)
    monkeypatch.setattr(codex, "AsyncCodexClient", Client)
    monkeypatch.setattr(codex.time, "time", lambda: 200)
    result = asyncio.run(
        codex.archive_completed_threads(
            "/project", 100, lambda event, **fields: events.append((event, fields))
        )
    )
    assert not result
    assert calls == [
        "list",
        "list",
        "old",
        "failed",
        "failed-run",
        "worktree",
        "list",
        "list",
        "notify",
        "closed",
    ]
    assert "recent" not in reads
    assert "foreign" not in reads
    assert reads["pinned"] == 1
    assert reads["pin-race"] == 2
    assert events[-1][1] == {"project": "/project", "archived": 3, "failed": 1 + int(ui_failure)}
    assert any(e == "thread_archive_failed" for e, _ in events)


def test_cleanup_disabled_never_calls_sdk(monkeypatch):
    async def unexpected(*args):
        pytest.fail("disabled cleanup called SDK")

    monkeypatch.setattr(codex, "archive_completed_threads", unexpected)
    assert asyncio.run(
        codex.cleanup_threads(
            {"thread_cleanup": {"enabled": False}}, lambda *a, **kw: None, once=True
        )
    )


def test_periodic_defaults_retry_and_cancel(monkeypatch):
    calls, events = [], []

    async def archive(project, age, emit):
        calls.append((project, age))
        raise RuntimeError("offline")

    async def sleep(seconds):
        assert seconds == 60
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(codex, "archive_completed_threads", archive)
    monkeypatch.setattr(codex.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            codex.cleanup_threads({"project": "/project"}, lambda e, **kw: events.append(e))
        )
    assert calls == [("/project", 300)] * 2
    assert events == ["thread_cleanup_failed"] * 2


def test_one_shot_returns_failure(monkeypatch):
    async def archive(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(codex, "archive_completed_threads", archive)
    assert not asyncio.run(
        codex.cleanup_threads({"project": "/project"}, lambda *a, **kw: None, once=True)
    )


def test_successful_one_shot_uses_configured_age(monkeypatch):
    async def archive(project, age, emit):
        assert (project, age) == ("/project", 42)
        return True

    monkeypatch.setattr(codex, "archive_completed_threads", archive)
    assert asyncio.run(
        codex.cleanup_threads(
            {"project": "/project", "thread_cleanup": {"completed_age": 42}},
            lambda *a, **kw: None,
            once=True,
        )
    )


@pytest.mark.parametrize(
    "cwd, expected",
    [
        ("/project", True),
        ("/project/.worktrees/fix", True),
        ("/project-other/.worktrees/fix", False),
        ("/project/docs", False),
        ("/other", False),
    ],
)
def test_cleanup_project_and_managed_worktree_scope(cwd, expected):
    assert codex.completed_before(thread(cwd=cwd), "/project", 100) == expected


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
@pytest.mark.parametrize(
    "updated, ended, expected",
    [
        (799, 799, True),
        (800, 799, False),
        (799, 800, False),
        (801, 700, False),
        (700, None, False),
    ],
)
def test_five_minutes_requires_both_end_and_last_activity(status, updated, ended, expected):
    data = thread(updatedAt=updated, turns=[{"status": status, "completedAt": ended}])
    assert codex.completed_before(data, "/project", 1100 - 300) == expected


@pytest.mark.parametrize("section", [None, {"id": "custom", "name": "Pinned"}])
def test_unpinned_sections_remain_eligible(section):
    assert codex.completed_before(thread(section=section), "/project", 100)
