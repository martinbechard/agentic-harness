"""Mechanical cleanup eligibility, pagination, failure isolation, and scheduling."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

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
        {"updatedAt": 100},
        {"turns": []},
        {"status": {"type": "active"}},
        {"status": {"type": "systemError"}},
        {"turns": [{"status": "inProgress", "completedAt": None}]},
        {"turns": [{"status": "failed", "completedAt": 10}]},
        {"turns": [{"status": "interrupted", "completedAt": 10}]},
        {"turns": [{"status": "completed", "completedAt": None}]},
        {"turns": [{"status": "completed", "completedAt": 100}]},
    ],
)
def test_ineligible_threads_are_preserved(changes):
    assert not codex.completed_before(thread(**changes), "/project", 100)


def test_completed_thread_is_eligible():
    assert codex.completed_before(thread(), "/project", 100)


def test_paginated_cleanup_rechecks_and_continues_after_failure(monkeypatch):
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
            assert params["cwd"] == "/project" and params["archived"] is False
            ids = ["old", "resumed", "failed"] if params["cursor"] is None else ["empty", "recent"]
            return SimpleNamespace(
                data=[SimpleNamespace(id=i, updated_at=100 if i == "recent" else 10) for i in ids],
                next_cursor="next" if params["cursor"] is None else None,
            )

        async def thread_read(self, identity, include_turns):
            assert include_turns
            reads[identity] = reads.get(identity, 0) + 1
            data = thread(id=identity)
            if identity == "empty":
                data["turns"] = []
            if identity == "resumed" and reads[identity] == 2:
                data["turns"].append({"status": "inProgress", "completedAt": None})
            return SimpleNamespace(thread=SimpleNamespace(model_dump=lambda **kw: deepcopy(data)))

        async def thread_archive(self, identity):
            calls.append(identity)
            if identity == "failed":
                raise RuntimeError("archive unavailable")

    monkeypatch.setattr(codex, "AsyncCodexClient", Client)
    monkeypatch.setattr(codex.time, "time", lambda: 200)
    result = asyncio.run(
        codex.archive_completed_threads(
            "/project", 100, lambda event, **fields: events.append((event, fields))
        )
    )
    assert not result
    assert calls == ["list", "list", "old", "failed", "closed"]
    assert "recent" not in reads
    assert events[-1][1] == {"project": "/project", "archived": 1, "failed": 1}
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
        assert seconds == 900
        if len(calls) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(codex, "archive_completed_threads", archive)
    monkeypatch.setattr(codex.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            codex.cleanup_threads({"project": "/project"}, lambda e, **kw: events.append(e))
        )
    assert calls == [("/project", 3600)] * 2
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
