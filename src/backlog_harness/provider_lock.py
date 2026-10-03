"""Serialize provider invocations sharing one configured harness state directory."""

import asyncio
import fcntl
import json
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


@asynccontextmanager
async def provider_lock(state):
    path = Path(state) / "provider.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class CoordinationUnavailable(RuntimeError):
    """No compatible runner currently owns the configured provider coordination lease."""


def require_coordinator(config):
    """An old runner cannot satisfy this check merely by leaving files behind."""
    path = Path(config["state"]) / "provider-coordination.lock"
    try:
        with path.open("r+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner = json.load(stream)
                if (
                    isinstance(owner, dict)
                    and owner.get("version") == 1
                    and owner.get("project") == config["project"]
                ):
                    return
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    raise CoordinationUnavailable("A live runner with shared provider coordination is required")


@contextmanager
def coordinator(config):
    """Hold a local capability lease for the sole upgraded runner's lifetime."""
    path = Path(config["state"]) / "provider-coordination.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CoordinationUnavailable(
                "Another coordinated runner owns this state directory"
            ) from None
        stream.seek(0)
        stream.truncate()
        json.dump({"version": 1, "project": config["project"], "pid": os.getpid()}, stream)
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
