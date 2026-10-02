"""Durable invocation evidence. Requested effects are never replayed implicitly."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from .contracts import utcnow


class EvidenceError(RuntimeError):
    pass


@contextmanager
def operation_lock(path):
    """A shared OS lock fences concurrent processes using the same operation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvidenceError("Another process owns this operation") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@asynccontextmanager
async def async_operation_lock(path):
    while True:
        lock = operation_lock(path)
        try:
            lock.__enter__()
        except EvidenceError:
            await asyncio.sleep(0.05)
            continue
        try:
            yield
        finally:
            lock.__exit__(None, None, None)
        return


def component(identity: str) -> str:
    return sha256(identity.encode()).hexdigest()


def fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(tmp, path)
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def append(self, value):
        line = (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            + b"\n"
        )
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A partial previous line cannot be joined to another record.
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as previous:
                    previous.seek(-1, os.SEEK_END)
                    if previous.read(1) != b"\n":
                        raise EvidenceError(
                            "Incomplete final evidence line; reconciliation required"
                        )
            with self.path.open("ab") as stream:
                os.chmod(self.path, 0o600)
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            fsync_directory(self.path.parent)


def read_jsonl(path: Path, offset=0):
    if not path.exists():
        return [], offset, False
    records = []
    with path.open("rb") as stream:
        if path.stat().st_size < offset:
            raise EvidenceError("Evidence file shrank during incremental read")
        stream.seek(offset)
        while line := stream.readline():
            if not line.endswith(b"\n"):
                return records, offset, True
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeError) as exc:
                raise EvidenceError("Invalid complete evidence line") from exc
            offset = stream.tell()
    return records, offset, False


class EvidenceStore:
    def __init__(self, root: Path, run_id: str):
        self.root, self.run_id = root, run_id
        self.run = root / "runs" / component(run_id)

    def begin(
        self,
        operation_id,
        invocation_id,
        snapshot,
        binding,
        *,
        action,
        item_id=None,
        request_digest=None,
        coordination_digest=None,
    ):
        path = (
            self.run
            / "operations"
            / component(operation_id)
            / "invocations"
            / component(invocation_id)
        )
        record = {
            "version": 1,
            "run_id": self.run_id,
            "operation_id": operation_id,
            "invocation_id": invocation_id,
            "config_digest": snapshot.file_digest,
            "binding": asdict(binding),
            "action": action,
            "item_id": item_id,
            "request_digest": request_digest,
            "created_at": utcnow(),
        }
        if coordination_digest is not None:
            record["coordination_digest"] = coordination_digest
        # Persist references and digests only: arbitrary adapter options may contain secrets.
        atomic_json(
            path / "config.json",
            {"version": 1, "digest": snapshot.file_digest, "binding": asdict(binding)},
            exclusive=True,
        )
        atomic_json(path / "intent.json", record, exclusive=True)
        return path

    @staticmethod
    def requested(path):
        atomic_json(path / "requested.json", {"version": 1, "at": utcnow()}, exclusive=True)

    @staticmethod
    def session(path, value):
        atomic_json(path / "session.json", {"version": 1, **value}, exclusive=True)

    @staticmethod
    def outcome(path, classification, **facts):
        JsonlWriter(path / "outcomes.jsonl").append(
            {"version": 1, "at": utcnow(), "classification": classification, **facts}
        )

    @staticmethod
    def reconcile(path):
        try:
            intent = json.loads((path / "intent.json").read_text())
            if intent.get("version") != 1 or component(intent["invocation_id"]) != path.name:
                raise EvidenceError("Invocation identity or version mismatch")
            if component(intent["operation_id"]) != path.parents[1].name:
                raise EvidenceError("Operation identity mismatch")
            if component(intent["run_id"]) != path.parents[3].name:
                raise EvidenceError("Run identity mismatch")
            outcomes, _, partial = read_jsonl(path / "outcomes.jsonl")
            state = "not_submitted"
            if (path / "requested.json").exists():
                state = "unresolved"
                if outcomes and not partial:
                    state = outcomes[-1]["classification"]
            return {**intent, "outcome": state, "partial": partial}
        except (OSError, ValueError, KeyError) as exc:
            raise EvidenceError(f"Cannot reconcile invocation: {exc}") from exc
