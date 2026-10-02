"""Generator supervision records uncertainty and interruption without inventing success."""

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backlog_harness import integration_commands as commands


@pytest.mark.parametrize(
    "observed",
    [
        OSError("unavailable"),
        subprocess.TimeoutExpired("ps", 1),
        SimpleNamespace(returncode=1, stdout=""),
        SimpleNamespace(returncode=0, stdout="123 S\n"),
    ],
)
def test_failed_inspection_cannot_claim_quiescence(monkeypatch, observed):
    def inspect(*args, **kwargs):
        if isinstance(observed, BaseException):
            raise observed
        return observed

    monkeypatch.setattr(commands.subprocess, "run", inspect)
    assert commands._group_quiescent(123) is False


@pytest.mark.parametrize("failure", ["launch", "runtime", "interrupt", "cancel", "output"])
def test_generator_failures_keep_cleanup_evidence(tmp_path, monkeypatch, failure):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(commands.shutil, "which", lambda name: "/sandbox")
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    monkeypatch.setattr(commands, "_group_quiescent", lambda pid: True)
    monkeypatch.setattr(commands, "_cleanup", lambda process: True)
    stream = Mock()
    process = SimpleNamespace(pid=123, stdout=stream, stderr=stream, poll=lambda: 1)
    calls = 0

    def communicate(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "runtime":
                raise OSError("read failed")
            if failure == "interrupt":
                raise KeyboardInterrupt()
            if failure == "cancel":
                raise asyncio.CancelledError()
        if failure == "output":
            raise OSError("output unavailable")
        return "output", "error"

    process.communicate = communicate

    def launch(*args, **kwargs):
        if failure == "launch":
            raise OSError("missing executable")
        return process

    monkeypatch.setattr(commands.subprocess, "Popen", launch)
    records = []
    if failure in ("interrupt", "cancel"):
        with pytest.raises((KeyboardInterrupt, asyncio.CancelledError)):
            commands.run_generator(["generator"], tmp_path, record_result=records.append)
    else:
        result = commands.run_generator(["generator"], tmp_path, record_result=records.append)
        assert result.failure_reason in ("launch_failed", "runtime_failed")
    assert len(records) == 1
    assert records[0]["failure_reason"] is not None
    assert records[0]["process_group_quiescent"] is (failure != "output")
    if failure == "output":
        assert stream.close.call_count == 2


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError()])
def test_cleanup_handles_exited_or_uncontrollable_group(monkeypatch, error):
    probes = iter([False, True])
    monkeypatch.setattr(commands, "_group_quiescent", lambda pid: next(probes))

    def signal(*args):
        raise error

    monkeypatch.setattr(commands.os, "killpg", signal)
    process = SimpleNamespace(pid=123, poll=Mock())
    assert commands._cleanup(process) is isinstance(error, ProcessLookupError)


def test_cleanup_exhausts_both_signals_without_claiming_quiescence(monkeypatch):
    import signal

    monkeypatch.setattr(commands, "_group_quiescent", lambda pid: False)
    signals = []
    monkeypatch.setattr(commands.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    clock = iter([0, 1, 2, 3])
    monkeypatch.setattr(commands.time, "monotonic", lambda: next(clock))
    process = SimpleNamespace(pid=123, poll=Mock())
    assert commands._cleanup(process) is False
    assert signals == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_successful_generator_parent_does_not_hide_live_descendants(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(commands.shutil, "which", lambda name: "/sandbox")
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    monkeypatch.setattr(commands, "_group_quiescent", lambda pid: False)
    monkeypatch.setattr(commands, "_cleanup", lambda process: False)
    process = SimpleNamespace(pid=123, poll=lambda: 0, communicate=lambda **kwargs: ("done", ""))
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *args, **kwargs: process)
    result = commands.run_generator(["generator"], tmp_path)
    assert result.returncode == 0
    assert result.failure_reason == "descendants_remaining"
    assert result.process_group_quiescent is False


def test_unavailable_output_with_no_streams_retains_uncertainty(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(commands.shutil, "which", lambda name: "/sandbox")
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    monkeypatch.setattr(commands, "_group_quiescent", lambda pid: True)
    monkeypatch.setattr(commands, "_cleanup", lambda process: True)
    process = SimpleNamespace(pid=123, poll=lambda: 0, stdout=None, stderr=None)
    responses = iter([("done", ""), OSError("lost output")])

    def communicate(**kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    process.communicate = communicate
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *args, **kwargs: process)
    result = commands.run_generator(["generator"], tmp_path)
    assert result.failure_reason == "output_unresolved"
    assert result.process_group_quiescent is False
