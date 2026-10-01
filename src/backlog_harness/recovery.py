"""AI attribution: Generated with AI assistance.

Validate immutable evidence for recovery of an interrupted work-item owner.
"""

from __future__ import annotations

import json
import math
import re
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import UUID

from .provider import Item, TransitionBlocked, git
from .workflow import require

_DIGEST = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _exact_keys(value, required, optional=()):
    require(isinstance(value, dict), "Recovery evidence must use JSON objects")
    keys = set(value)
    required_keys = set(required)
    require(required_keys <= keys, f"Recovery evidence is missing {sorted(required_keys - keys)}")
    require(keys <= required_keys | set(optional), "Recovery evidence has unsupported fields")


def _digest(value, label):
    require(isinstance(value, str) and _DIGEST.fullmatch(value), f"Invalid {label} hash")
    return value


def _uuid(value, label):
    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise TransitionBlocked(f"Invalid {label}") from exc
    require(value == canonical, f"Invalid {label}")
    return value


def _relative_path(value, label):
    require(isinstance(value, str) and value, f"Invalid {label}")
    path = PurePosixPath(value)
    require(
        not path.is_absolute()
        and value == path.as_posix()
        and ".." not in path.parts
        and ".git" not in path.parts
        and "\n" not in value
        and "\r" not in value,
        f"Invalid {label}",
    )
    return value


def _read_file(path, expected, label):
    require(
        path.is_absolute() and path.is_file() and not path.is_symlink(), f"Invalid {label} path"
    )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TransitionBlocked(f"Cannot read {label}") from exc
    require(sha256(data).hexdigest() == expected, f"Stale {label} hash")
    return data


def _validate_runtime_record(record):
    _exact_keys(
        record,
        {"path", "sha256", "native_session_id", "final_turn_id"},
    )
    path_text = record["path"]
    require(isinstance(path_text, str) and Path(path_text).is_absolute(), "Invalid runtime path")
    supplied_path = Path(path_text)
    require(not supplied_path.is_symlink(), "Invalid runtime path")
    path = supplied_path.resolve()
    require(str(path) == path_text, "Runtime path is not normalized")
    expected = _digest(record["sha256"], "runtime record")
    session_id = _uuid(record["native_session_id"], "native session ID")
    turn_id = _uuid(record["final_turn_id"], "final turn ID")
    data = _read_file(path, expected, "runtime record")
    require(data.endswith(b"\n"), "Runtime record has an incomplete final line")
    try:
        records = [json.loads(line) for line in data.splitlines()]
    except (UnicodeError, ValueError) as exc:
        raise TransitionBlocked("Runtime record is not valid JSONL") from exc
    require(all(isinstance(row, dict) for row in records), "Runtime record has a non-object line")
    metadata = [row.get("payload") for row in records if row.get("type") == "session_meta"]
    require(
        len(metadata) == 1
        and isinstance(metadata[0], dict)
        and metadata[0].get("id", metadata[0].get("session_id")) == session_id,
        "Runtime session identity differs",
    )
    completed = []
    for index, row in enumerate(records):
        payload = row.get("payload")
        if (
            row.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_complete"
            and payload.get("turn_id") == turn_id
        ):
            completed.append((index, payload))
    require(len(completed) == 1, "Final task completion is absent or ambiguous")
    completion_index, completion = completed[0]
    require(not completion.get("error"), "Final task did not complete successfully")
    later = [
        row
        for row in records[completion_index + 1 :]
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") in {"task_started", "task_complete"}
    ]
    require(not later, "Runtime record has a later lifecycle event")
    return path, data


def _validate_scope(packet, repository):
    candidate = packet["candidate"]
    scope = packet["scope"]
    _exact_keys(candidate, {"checkout", "head", "base", "allowed_paths", "evidence"})
    _exact_keys(scope, {"allowed_paths", "checks"})
    checkout = candidate["checkout"]
    require(
        isinstance(checkout, str) and Path(checkout).is_absolute(), "Invalid candidate checkout"
    )
    require(str(repository) == checkout, "Candidate checkout differs from the repository")
    head, base = candidate["head"], candidate["base"]
    require(isinstance(head, str) and _COMMIT.fullmatch(head), "Invalid candidate commit")
    require(isinstance(base, str) and _COMMIT.fullmatch(base), "Invalid candidate base")

    allowed = candidate["allowed_paths"]
    require(isinstance(allowed, list) and allowed, "Candidate allowed paths are missing")
    normalized_allowed = [_relative_path(value, "allowed path") for value in allowed]
    require(len(set(normalized_allowed)) == len(normalized_allowed), "Duplicate allowed path")
    require(scope["allowed_paths"] == normalized_allowed, "Candidate and scope paths differ")

    checks = scope["checks"]
    require(
        isinstance(checks, list)
        and checks
        and all(
            isinstance(argv, list) and argv and all(isinstance(arg, str) and arg for arg in argv)
            for argv in checks
        ),
        "Explicit check commands are required",
    )

    require(git(repository, "rev-parse", "HEAD") == head, "Candidate HEAD changed")
    require(not git(repository, "status", "--porcelain"), "Candidate is dirty")
    require(
        git(repository, "rev-parse", "--verify", head + "^{commit}") == head,
        "Invalid candidate commit",
    )
    require(
        git(repository, "rev-parse", "--verify", base + "^{commit}") == base,
        "Invalid candidate base",
    )
    try:
        git(repository, "merge-base", "--is-ancestor", base, head)
    except TransitionBlocked as exc:
        raise TransitionBlocked("Candidate base is not an ancestor") from exc
    changed = git(repository, "diff", "--name-only", base, head).splitlines()
    require(changed and set(changed) <= set(normalized_allowed), "Candidate changes exceed scope")

    evidence = candidate["evidence"]
    require(isinstance(evidence, list) and evidence, "Candidate file evidence is missing")
    evidence_files = []
    for entry in evidence:
        _exact_keys(entry, {"path", "sha256"})
        relative = _relative_path(entry["path"], "candidate evidence path")
        expected = _digest(entry["sha256"], "candidate evidence")
        supplied_path = repository / relative
        path = supplied_path.resolve()
        require(path.is_relative_to(repository), "Candidate evidence escapes the checkout")
        evidence_files.append(
            (supplied_path, _read_file(supplied_path, expected, "candidate evidence"))
        )
    require(
        len({path for path, _ in evidence_files}) == len(evidence_files),
        "Duplicate candidate evidence path",
    )
    return head, evidence_files


def validate_packet(packet: dict, item: Item, repository: Path) -> dict:
    """Validate an operator-supplied recovery packet without changing external state.

    The caller supplies the current provider item and the preserved candidate checkout.
    The packet must bind the stopped native sessions, candidate commit, file evidence,
    remaining estimate, and explicit workflow scope to that exact item revision and owner.
    The result contains a normalized JSON copy and its canonical SHA-256 digest. The
    function performs read-only file and Git checks. Invalid or stale evidence raises
    ``TransitionBlocked``; it never acquires claims, launches a model, or mutates state.
    """
    repository = Path(repository).resolve()
    try:
        normalized = json.loads(json.dumps(packet, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TransitionBlocked("Recovery packet must be JSON-compatible") from exc
    _exact_keys(
        normalized,
        {
            "version",
            "item_id",
            "revision",
            "previous_owner",
            "runtime_records",
            "candidate",
            "remaining_high",
            "scope",
            "historical_usage",
        },
        {"preserved_execution"},
    )
    require(normalized["version"] == 1, "Unsupported recovery packet version")
    require(normalized["item_id"] == item.item_id, "Recovery packet names another item")
    require(normalized["revision"] == item.revision, "Recovery packet revision is stale")
    require(normalized["previous_owner"] == item.owner, "Recovery previous owner differs")
    require(
        type(normalized["remaining_high"]) is int and normalized["remaining_high"] > 0,
        "Remaining high estimate must be a positive integer",
    )
    require(normalized["historical_usage"] == "unknown", "Historical usage must remain unknown")

    runtime_records = normalized["runtime_records"]
    require(
        isinstance(runtime_records, list) and runtime_records, "Native runtime records are required"
    )
    runtime_files = [_validate_runtime_record(record) for record in runtime_records]
    if item.state == "Ready":
        preserved = normalized.get("preserved_execution")
        require(item.owner in {None, "Unowned"}, "Ready recovery has a conflicting owner")
        _exact_keys(
            preserved,
            {
                "execution_id",
                "canonical_sha256",
                "remaining_work",
                "receipt",
                "candidate_approval_required",
                "snapshot_operation",
                "runtime_operations",
            },
        )
        require(
            preserved["candidate_approval_required"] is True,
            "Ready preserved-candidate recovery requires exact candidate approval",
        )
        require(
            isinstance(preserved["execution_id"], str)
            and preserved["execution_id"]
            and preserved["execution_id"] in item.content
            and normalized["candidate"]["head"] in item.content
            and preserved["canonical_sha256"] == sha256(item.content.encode()).hexdigest()
            and isinstance(preserved["remaining_work"], str)
            and preserved["remaining_work"].strip()
            and preserved["remaining_work"] in item.content,
            "Ready recovery canonical execution, candidate or remaining work differs",
        )
        _exact_keys(preserved["receipt"], {"path", "sha256"})
        receipt_path = Path(preserved["receipt"]["path"])
        receipt_bytes = _read_file(
            receipt_path, preserved["receipt"]["sha256"], "execution receipt"
        )
        try:
            receipt = json.loads(receipt_bytes)
        except (ValueError, UnicodeError) as exc:
            raise TransitionBlocked("Preserved execution receipt is not valid JSON") from exc
        require(isinstance(receipt, dict), "Preserved execution receipt must be an object")
        snapshot_op = preserved["snapshot_operation"]
        snapshot_entry = receipt.get(snapshot_op) if isinstance(snapshot_op, str) else None
        require(
            isinstance(snapshot_entry, dict) and isinstance(snapshot_entry.get("receipt"), dict),
            "Preserved snapshot receipt must be an object",
        )
        require(
            isinstance(snapshot_op, str)
            and snapshot_op.startswith(preserved["execution_id"] + "/")
            and snapshot_op.endswith("/snapshot")
            and snapshot_entry["receipt"].get("candidate_sha") == normalized["candidate"]["head"],
            "Preserved snapshot does not bind the exact execution and candidate",
        )
        operations = preserved["runtime_operations"]
        require(
            isinstance(operations, dict)
            and set(operations) == {r["native_session_id"] for r in runtime_records},
            "Preserved runtime operation identities differ",
        )
        attempt_prefix = snapshot_op.removesuffix("snapshot")
        for record in runtime_records:
            operation = operations[record["native_session_id"]]
            require(
                isinstance(operation, str) and operation.startswith(attempt_prefix),
                "Runtime operation is outside the preserved attempt",
            )
            entry = receipt.get(operation)
            require(
                isinstance(entry, dict) and isinstance(entry.get("receipt"), dict),
                "Preserved runtime receipt must be an object",
            )
            result = entry["receipt"]
            require(
                result.get("op_id") == operation
                and result.get("session_id") == record["native_session_id"]
                and type(result.get("started_unix")) in {int, float}
                and math.isfinite(result["started_unix"])
                and type(result.get("finished_unix")) in {int, float}
                and math.isfinite(result["finished_unix"])
                and result["finished_unix"] >= result["started_unix"],
                "Preserved execution receipt does not bind completed native sessions",
            )
        runtime_files.append((receipt_path, receipt_bytes))
    else:
        require(item.state == "Running", "Recovery requires Ready or Running")
        require("preserved_execution" not in normalized, "Ready evidence on another state")
    require(
        len({path for path, _ in runtime_files}) == len(runtime_files),
        "Duplicate runtime record path",
    )
    require(
        len({record["native_session_id"] for record in runtime_records}) == len(runtime_records),
        "Duplicate native session ID",
    )

    head, evidence_files = _validate_scope(normalized, repository)

    require(git(repository, "rev-parse", "HEAD") == head, "Candidate changed during validation")
    require(not git(repository, "status", "--porcelain"), "Candidate changed during validation")
    for path, original in runtime_files + evidence_files:
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise TransitionBlocked("Evidence changed during validation") from exc
        require(current == original, "Evidence changed during validation")

    canonical = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return {"packet": normalized, "digest": sha256(canonical).hexdigest()}


def validate_active_owner_binding(item, runtime, binding):
    """Bind stopped evidence to the active acceptance section, not historical mentions."""
    require(isinstance(binding, dict), "Active owner binding is required")
    heading = "## Running Acceptance Evidence"
    require(item.content.splitlines().count(heading) == 1, "Active acceptance section is ambiguous")
    section = item.content.split(heading + "\n", 1)[1].split("\n## ", 1)[0]
    require(
        binding.get("section_sha256") == sha256(section.encode()).hexdigest(),
        "Active owner binding is stale",
    )
    require("Owner: " + item.owner in section.splitlines(), "Active acceptance owner differs")
    identities = set(
        re.findall(
            r"^Canonical (?:Conversation|Task): `?([0-9a-f-]{36})`?\s*$", section, re.MULTILINE
        )
    )
    require(
        identities == {runtime["native_session_id"]},
        "Stopped runtime is not the active canonical owner",
    )
    return binding
