"""Persist and apply run-bound scheduling-capacity requests from local operators."""

import json
import os
import time
import uuid
from pathlib import Path

_REQUEST_KEYS = {
    "schema_version",
    "request_id",
    "expected_run",
    "expected_revision",
    "capacity",
}
_STATE_KEYS = {
    "schema_version",
    "revision",
    "capacity",
    "mode",
    "request",
    "updated_at",
}


def _scheduling_mode(capacity):
    return "solo" if capacity == 1 else "parallel"


def _valid_capacity(value):
    return type(value) is int and 1 <= value <= 8


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class SchedulingControl:
    """Own the harness side of the single-pending local scheduling-control channel.

    A dashboard publishes ``request.json`` atomically without replacing an existing
    request. The harness validates the request against the live run and accepted
    revision, persists the accepted choice, writes a durable receipt, and only then
    removes the request. An accepted state takes precedence during recovery so a
    crash between state and receipt writes cannot apply the choice twice.
    """

    def __init__(self, state_root, run, configured_capacity):
        if not isinstance(run, str) or not run:
            raise ValueError("Scheduling control requires a run identity")
        if not _valid_capacity(configured_capacity):
            raise ValueError("Capacity must be an integer from 1 through 8")
        self.run = run
        self.root = Path(state_root) / "control"
        self.request_path = self.root / "request.json"
        self.result_path = self.root / "result.json"
        self.scheduling_path = self.root / "scheduling.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.capacity = configured_capacity
        self.revision = 0
        self.request = None
        if self.scheduling_path.exists():
            try:
                state = json.loads(self.scheduling_path.read_text())
                self._validate_state(state)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid accepted scheduling state: {exc}") from exc
            self.capacity = state["capacity"]
            self.revision = state["revision"]
            self.request = state["request"]

    @property
    def request_id(self):
        """Return the last accepted request identity, when one exists."""
        return self.request["request_id"] if self.request else None

    def _validate_state(self, state):
        if not isinstance(state, dict) or set(state) != _STATE_KEYS:
            raise ValueError("accepted scheduling state has unknown or missing fields")
        if state["schema_version"] != 1:
            raise ValueError("accepted scheduling state has an unsupported schema")
        if type(state["revision"]) is not int or state["revision"] < 1:
            raise ValueError("accepted scheduling revision must be a positive integer")
        if not _valid_capacity(state["capacity"]):
            raise ValueError("accepted scheduling capacity must be an integer from 1 through 8")
        if state["mode"] != _scheduling_mode(state["capacity"]):
            raise ValueError("accepted scheduling mode does not match its capacity")
        self._validate_request(state["request"])
        if state["request"]["capacity"] != state["capacity"]:
            raise ValueError("accepted scheduling request does not match its capacity")
        if state["request"]["expected_revision"] + 1 != state["revision"]:
            raise ValueError("accepted scheduling request does not match its revision")
        if not isinstance(state["updated_at"], str) or not state["updated_at"].isdigit():
            raise ValueError("accepted scheduling timestamp must be Unix nanoseconds")

    def _validate_request(self, request):
        if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
            raise ValueError("request has unknown or missing fields")
        if request["schema_version"] != 1:
            raise ValueError("request has an unsupported schema")
        try:
            identity = uuid.UUID(request["request_id"])
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("request_id must be a UUID") from exc
        if str(identity) != request["request_id"]:
            raise ValueError("request_id must use canonical lowercase UUID text")
        if not isinstance(request["expected_run"], str) or not request["expected_run"]:
            raise ValueError("expected_run must be nonempty text")
        if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not _valid_capacity(request["capacity"]):
            raise ValueError("capacity must be an integer from 1 through 8")

    def _result(self, request, status, reason=None):
        capacity = self.capacity if status == "applied" else None
        return {
            "schema_version": 1,
            "request_id": request.get("request_id") if isinstance(request, dict) else None,
            "expected_run": request.get("expected_run") if isinstance(request, dict) else None,
            "expected_revision": (
                request.get("expected_revision") if isinstance(request, dict) else None
            ),
            "status": status,
            "capacity": capacity,
            "mode": _scheduling_mode(capacity) if capacity is not None else None,
            "revision": self.revision,
            "reason": reason,
            "completed_at": str(time.time_ns()),
            "request": request,
        }

    def _finish(self, result):
        _atomic_json(self.result_path, result)
        self.request_path.unlink(missing_ok=True)

    def _reject(self, harness, request, reason):
        result = self._result(request, "rejected", reason)
        self._finish(result)
        harness.emit(
            "scheduling_change_rejected",
            request_id=result["request_id"],
            reason=reason,
            scheduling_revision=self.revision,
        )

    async def process(self, harness):
        """Process one pending request and leave a durable applied or rejected receipt."""
        if not self.request_path.exists():
            return False
        try:
            request = json.loads(self.request_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._reject(harness, None, "invalid_request")
            return True

        accepted = self.request
        if isinstance(request, dict) and accepted and request.get("request_id") == self.request_id:
            if request == accepted:
                self._finish(self._result(request, "applied"))
            else:
                self._reject(harness, request, "request_id_conflict")
            return True

        if self.result_path.exists() and isinstance(request, dict):
            try:
                previous = json.loads(self.result_path.read_text())
            except (OSError, json.JSONDecodeError):
                previous = None
            if isinstance(previous, dict) and request.get("request_id") == previous.get(
                "request_id"
            ):
                if request == previous.get("request"):
                    self._finish(previous)
                else:
                    self._reject(harness, request, "request_id_conflict")
                return True

        try:
            self._validate_request(request)
        except ValueError:
            self._reject(harness, request, "invalid_request")
            return True
        if request["expected_run"] != self.run:
            self._reject(harness, request, "stale_run")
            return True
        if request["expected_revision"] != self.revision:
            self._reject(harness, request, "stale_revision")
            return True

        next_revision = self.revision + 1

        def persist():
            state = {
                "schema_version": 1,
                "revision": next_revision,
                "capacity": request["capacity"],
                "mode": _scheduling_mode(request["capacity"]),
                "request": request,
                "updated_at": str(time.time_ns()),
            }
            _atomic_json(self.scheduling_path, state)

        await harness.set_capacity(
            request["capacity"], next_revision, request["request_id"], persist
        )
        self.capacity = request["capacity"]
        self.revision = next_revision
        self.request = request
        self._finish(self._result(request, "applied"))
        return True
