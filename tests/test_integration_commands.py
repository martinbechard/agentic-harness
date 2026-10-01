import shutil
import sys

import pytest

from backlog_harness.integration_commands import run_generator
from backlog_harness.provider import TransitionBlocked


def test_generator_never_falls_back_to_unrestricted_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(TransitionBlocked, match="unrestricted fallback"):
        run_generator([sys.executable, "-c", "raise AssertionError('must not execute')"], tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS confinement contract")
def test_generator_cannot_write_outside_isolated_clone(tmp_path):
    workspace = tmp_path / "clone"
    (workspace / ".git").mkdir(parents=True)
    outside = tmp_path / "untouched"
    outside.write_text("original")
    result = run_generator(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(" + repr(str(outside)) + ").write_text('changed')",
        ],
        workspace,
    )
    assert result.returncode != 0
    assert outside.read_text() == "original"
    result = run_generator(
        [sys.executable, "-c", "from pathlib import Path; Path('output').write_text('generated')"],
        workspace,
    )
    assert result.returncode == 0, result.stderr
    assert (workspace / "output").read_text() == "generated"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS confinement contract")
@pytest.mark.parametrize("cancel", [False, True])
def test_generator_timeout_or_cancellation_stops_child_group(tmp_path, cancel):
    import threading
    import time

    workspace = tmp_path / "clone"
    (workspace / ".git").mkdir(parents=True)
    script = workspace / "spawn.py"
    script.write_text("""import subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-c',"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"])
Path('child.pid').write_text(str(child.pid))
print('started',flush=True)
time.sleep(60)
""")
    event = threading.Event()
    if cancel:

        def cancellation():
            deadline = time.monotonic() + 5
            while not (workspace / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            event.set()

        thread = threading.Thread(target=cancellation)
        thread.start()
    receipts = []
    start = time.monotonic()
    result = run_generator(
        [sys.executable, str(script)],
        workspace,
        timeout=3 if cancel else 0.5,
        cancellation_event=event,
        record_result=receipts.append,
    )
    if cancel:
        thread.join(timeout=5)
    assert time.monotonic() - start < 5
    assert result.failure_reason == ("cancelled" if cancel else "timeout")
    assert result.returncode != 0 and result.process_group_quiescent
    assert receipts == [result.receipt()]
    assert "started" in result.stdout
    import subprocess

    state = subprocess.run(
        ["ps", "-p", (workspace / "child.pid").read_text(), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert not state or state.startswith("Z")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS confinement contract")
def test_keyboard_interrupt_retains_receipt_and_cleans_child(tmp_path):
    import json
    import os
    import signal
    import subprocess
    import time

    workspace = tmp_path / "clone"
    (workspace / ".git").mkdir(parents=True)
    driver = tmp_path / "driver.py"
    driver.write_text("""import json,sys
from pathlib import Path
from backlog_harness.integration_commands import run_generator
workspace=Path(sys.argv[1])
code=\"import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid)); time.sleep(60)\"
run_generator([sys.executable,'-c',code],workspace,
              record_result=lambda value: (workspace/'result.json').write_text(json.dumps(value)))
""")
    env = {
        **os.environ,
        "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"),
    }
    process = subprocess.Popen(
        [sys.executable, str(driver), str(workspace)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not (workspace / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (workspace / "child.pid").exists()
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        receipt = json.loads((workspace / "result.json").read_text())
        assert receipt["failure_reason"] == "cancelled" and receipt["process_group_quiescent"]
        assert process.returncode != 0
        state = subprocess.run(
            ["ps", "-p", (workspace / "child.pid").read_text(), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
