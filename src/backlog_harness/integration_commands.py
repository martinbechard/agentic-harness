"""Run declared integration generators with confinement and bounded group cleanup."""

import asyncio
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .workflow import require


@dataclass(frozen=True)
class GeneratorResult:
    returncode: int | None
    stdout: str
    stderr: str
    pid: int | None
    failure_reason: str | None
    process_group_quiescent: bool

    def receipt(self):
        return {**asdict(self), "stdout": self.stdout[-4000:], "stderr": self.stderr[-4000:]}


def _group_quiescent(group):
    """Zombie entries cannot execute; uncertain process inspection is not quiescence."""
    try:
        observed = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=False, timeout=1
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if observed.returncode:
        return False
    for row in observed.stdout.splitlines():
        fields = row.split()
        if len(fields) >= 2 and fields[0] == str(group) and not fields[1].startswith("Z"):
            return False
    return True


def _cleanup(process):
    for sig, grace in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 0.7)):
        if _group_quiescent(process.pid):
            return True
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()  # Reap the owned parent while observing its remaining descendants.
            if _group_quiescent(process.pid):
                return True
            time.sleep(0.02)
    return _group_quiescent(process.pid)


def run_generator(argv, workspace, *, timeout=300, cancellation_event=None, record_result=None):
    """Return truthful termination evidence; propagate interruptions after retaining cleanup.

    The caller's existing requested-effect record fences retries even when the supervisor
    itself is killed before this function can record a result.
    """
    require(
        type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
        "Generator timeout must be positive and finite",
    )
    workspace = Path(workspace).resolve()
    sandbox = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
    require(
        sandbox is not None,
        "Confined integration generators require macOS sandbox-exec; unrestricted fallback is prohibited",
    )
    temporary = workspace / ".git" / "integration-tmp"
    temporary.mkdir(exist_ok=True)
    profile = (
        "(version 1)(allow default)(deny file-write* (require-not (subpath "
        + json.dumps(str(workspace))
        + ')))(allow file-write* (literal "/dev/null"))'
    )
    process = None
    reason = None
    interrupted = None
    stdout = stderr = ""
    try:
        process = subprocess.Popen(
            [sandbox, "-p", profile, *argv],
            cwd=workspace,
            env={**os.environ, "TMPDIR": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                reason = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except OSError as exc:
        reason = "launch_failed" if process is None else "runtime_failed"
        stderr = str(exc)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError) as exc:
        reason = "cancelled"
        interrupted = exc
    finally:
        if process is not None:
            # Even a successful parent cannot leave writing descendants running.
            lingering = not _group_quiescent(process.pid)
            quiescent = _cleanup(process)
            if lingering and reason is None:
                reason = "descendants_remaining"
            try:
                stdout, stderr = process.communicate(timeout=0.5)
            except (subprocess.TimeoutExpired, OSError):
                quiescent = False
                reason = reason or "output_unresolved"
                for stream in (process.stdout, process.stderr):
                    if stream:
                        stream.close()
            result = GeneratorResult(process.poll(), stdout, stderr, process.pid, reason, quiescent)
        else:
            result = GeneratorResult(None, stdout, stderr, None, reason, True)
        if record_result is not None:
            record_result(result.receipt())
    if interrupted is not None:
        raise interrupted
    return result
