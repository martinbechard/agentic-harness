"""Actual foreground locking and prompt_toolkit terminal over a PTY."""

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
from urllib.request import urlopen

import pytest
from system_support import InstalledHarness, install_wheel


@pytest.fixture(scope="session")
def controls_python(tmp_path_factory):
    return install_wheel(tmp_path_factory)


@pytest.fixture(scope="module")
def completed_harness(tmp_path_factory, controls_python):
    harness = InstalledHarness.create(
        tmp_path_factory.mktemp("completed-controls"), controls_python
    )
    result = harness.run("run-item", "item-one")
    assert result.returncode == 0, result.stdout + result.stderr
    return harness


class Terminal:
    def __init__(self, harness):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 400, 0, 0))
        self.process = subprocess.Popen(
            [
                str(harness.python.parent / "agentic-harness"),
                "--config",
                str(harness.config_path),
                "app",
            ],
            cwd=harness.repo,
            env={**harness.env, "TERM": "xterm-256color"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        self.buffer = ""
        self.wait_text("harness>")

    def read(self):
        if select.select([self.master], [], [], 0.1)[0]:
            try:
                chunk = os.read(self.master, 262144).decode(errors="replace")
            except OSError:
                return
            if "\x1b[6n" in chunk:
                os.write(self.master, b"\x1b[1;1R")
            self.buffer += chunk

    def wait_text(self, text, timeout=15):
        deadline = time.monotonic() + timeout
        while text not in self.buffer and time.monotonic() < deadline:
            self.read()
        assert text in self.buffer, self.buffer[-4000:]

    def send(self, text):
        self.buffer = ""
        os.write(self.master, (text + "\n").encode())

    def value(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        decoder = json.JSONDecoder()
        while time.monotonic() < deadline:
            self.read()
            clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", self.buffer).replace("\r", "")
            for match in re.finditer(r"\{", clean):
                try:
                    value, _ = decoder.raw_decode(clean[match.start() :])
                except ValueError:
                    continue
                if isinstance(value, dict) and predicate(value):
                    return value
        raise AssertionError(self.buffer[-5000:])

    def close(self):
        try:
            self.send("quit")
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            os.close(self.master)


def test_s09_competing_foreground_runner_rejected(completed_harness, tmp_path):
    harness = completed_harness
    calls = harness.repo.parent / "agent-calls.jsonl"
    before = calls.read_bytes()
    output = tmp_path / "watch.log"
    with output.open("w") as stream:
        first = subprocess.Popen(
            [
                str(harness.python.parent / "agentic-harness"),
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
                assert first.poll() is None, output.read_text()
                time.sleep(0.1)
            assert "IdleWatch" in output.read_text()
            second = harness.run("run", "--watch", timeout=10)
            assert second.returncode == 2, second.stdout + second.stderr
            assert "already active" in second.stderr or "owns this operation" in second.stderr
            assert first.poll() is None
            assert calls.read_bytes() == before
        finally:
            first.send_signal(signal.SIGINT)
            try:
                first.wait(timeout=10)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(timeout=5)


def test_s11_populated_terminal_matches_status_and_dashboard(completed_harness):
    harness = completed_harness
    before = (harness.repo.parent / "agent-calls.jsonl").read_bytes()
    expected = json.loads(harness.run("status").stdout)
    terminal = Terminal(harness)
    try:
        terminal.send("status")
        status = terminal.value(lambda value: "trace_span_count" in value)
        assert status["items"] == expected["items"]
        assert status["counts"] == expected["counts"]
        assert status["trace_span_count"] == expected["trace_span_count"]
        terminal.send("item show item-one")
        item = terminal.value(
            lambda value: value.get("item_id") == "item-one" and "content" in value
        )
        assert item["state"] == "Completed"
        assert item["revision"] == expected["items"][0]["revision"]
        acceptance = next(
            harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/accept.json")
        )
        session = json.loads(acceptance.read_text())["session"]["session_id"]
        terminal.send("session show " + session)
        found = terminal.value(lambda value: "session_evidence" in value)["session_evidence"]
        assert found and all(
            row["session"]["session_id"] == session and not row["partial"] for row in found
        )
        terminal.send("review-hold item-one")
        response = terminal.value(lambda value: "error" in value)
        assert "Unknown command" not in response["error"]
    finally:
        terminal.close()
    dashboard = subprocess.Popen(
        [
            str(harness.python.parent / "agentic-harness"),
            "--config",
            str(harness.config_path),
            "dashboard",
            "--port",
            "0",
        ],
        env=harness.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([dashboard.stdout], [], [], 10)[0]
        base = dashboard.stdout.readline().strip().split("Read-only dashboard: ", 1)[1]
        with urlopen(base + "/api/snapshot", timeout=5) as response:
            actual = json.load(response)
        assert actual["items"] == status["items"]
        assert actual["trace_span_count"] == status["trace_span_count"]
    finally:
        dashboard.terminate()
        dashboard.wait(timeout=5)
    assert (harness.repo.parent / "agent-calls.jsonl").read_bytes() == before


def test_s04_terminal_generic_answer_resumes_graph(controls_python, tmp_path):
    from test_system_graph_cli import graph_harness

    harness = graph_harness(tmp_path, controls_python, "graph-question")
    initial = harness.run("run-item", "item-one")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    terminal = Terminal(harness)
    try:
        terminal.send("item answer item-one language")
        terminal.wait_text("answer>")
        terminal.send("English")
        result = terminal.value(
            lambda value: value.get("result", {}).get("state") == "Completed", timeout=45
        )
        assert result["delivery"]["verified"] is True
    finally:
        terminal.close()
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()


def test_s09_same_item_competitors_do_not_duplicate_execution(controls_python, tmp_path):
    dispatcher = tmp_path / "paused_agent.py"
    dispatcher.write_text("""import importlib.util,os,time
from pathlib import Path
def dispatch(prompt,cwd,native_home,argv,session_id):
    root=Path(os.environ['HARNESS_SYSTEM_REPO']).parent
    if 'Running is now recorded for your exact session' in prompt:
        (root/'work-started').write_text(session_id)
        deadline=time.monotonic()+10
        while not (root/'release-work').exists() and time.monotonic()<deadline:
            time.sleep(.02)
        assert (root/'release-work').exists(), 'Test did not release worker'
    spec=importlib.util.spec_from_file_location('legacy',Path(os.environ['HARNESS_SYSTEM_HELPERS'])/'legacy_agent.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.dispatch(prompt,cwd,native_home,argv,session_id)
""")
    harness = InstalledHarness.create(tmp_path / "case", controls_python, dispatcher=dispatcher)
    root = harness.repo.parent
    argv = [
        str(harness.python.parent / "agentic-harness"),
        "--config",
        str(harness.config_path),
        "run-item",
        "item-one",
    ]
    processes = []
    with (root / "first.log").open("w") as first_log, (root / "second.log").open("w") as second_log:
        try:
            first = subprocess.Popen(
                argv, cwd=harness.repo, env=harness.env, stdout=first_log, stderr=first_log
            )
            processes.append(first)
            deadline = time.monotonic() + 30
            while not (root / "work-started").exists() and time.monotonic() < deadline:
                assert first.poll() is None, (root / "first.log").read_text()
                time.sleep(0.05)
            assert (root / "work-started").exists()
            calls = (root / "agent-calls.jsonl").read_bytes()
            second = subprocess.Popen(
                argv, cwd=harness.repo, env=harness.env, stdout=second_log, stderr=second_log
            )
            processes.append(second)
            time.sleep(0.5)
            assert (root / "agent-calls.jsonl").read_bytes() == calls
            (root / "release-work").touch()
            assert first.wait(timeout=30) == 0, (root / "first.log").read_text()
            assert second.wait(timeout=30) == 0, (root / "second.log").read_text()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    rows = [json.loads(line) for line in (root / "agent-calls.jsonl").read_text().splitlines()]
    assert (
        sum("Running is now recorded for your exact session" in row["prompt"] for row in rows) == 1
    )
    assert (
        sum("Perform only this authorized provider transition" in row["prompt"] for row in rows)
        == 3
    )
    assert "Status: Completed" in (harness.repo / "backlog/archive/item-one.md").read_text()
    first_result = json.loads((root / "first.log").read_text())
    second_result = json.loads((root / "second.log").read_text())
    assert second_result["state"] == "Completed"
    assert first_result["after"]["revision"] == second_result["revision"]
    receipts = list(
        harness.repo.glob(".agent-ops/backlog-harness/workflow-evidence/*/delivery.json")
    )
    assert len(receipts) == 1
    assert (
        json.loads(receipts[0].read_text())["main_commit"]
        == first_result["delivery"]["main_commit"]
    )
