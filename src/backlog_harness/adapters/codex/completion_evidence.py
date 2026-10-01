"""Reconcile one completed Codex turn from retained evidence, without sending a prompt.

This capability proves a particular recorded invocation ended and its recorded
process group is absent. It does not establish domain acceptance, complete usage,
or authorization to resume a session another owner might subsequently change.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from ...contracts import digest, utcnow
from ...evidence import EvidenceStore, atomic_json
from ...native_evidence import native_records
from ...provider import TransitionBlocked
from ...workflow import require


def _read(path):
    require(path.is_file() and not path.is_symlink(), "Retained invocation evidence is missing")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise TransitionBlocked("Retained invocation evidence is malformed") from exc
    require(isinstance(value, dict), "Retained invocation evidence must be an object")
    return value


def prepare_native_request(invocation_path, prompt):
    """Persist an exact wire-text marker before submission; never retrofit old prompts."""
    path = Path(invocation_path)
    intent = EvidenceStore.reconcile(path)
    require(isinstance(prompt, str) and prompt.strip(), "Native prompt must be nonempty")
    identity = {
        key: intent[key] for key in ("run_id", "operation_id", "invocation_id", "request_digest")
    }
    require(
        isinstance(identity["request_digest"], str) and identity["request_digest"],
        "Request digest is required",
    )
    marker = (
        "[harness-invocation " + json.dumps(identity, sort_keys=True, separators=(",", ":")) + "]"
    )
    wire_text = marker + "\n" + prompt
    record = {
        **identity,
        "marker": marker,
        "prompt_sha256": sha256(prompt.encode()).hexdigest(),
        "wire_sha256": sha256(wire_text.encode()).hexdigest(),
    }
    record_path = path / "native-request.json"
    if record_path.exists():
        require(
            _read(record_path) == record, "Native request marker differs from retained submission"
        )
    else:
        require(not (path / "requested.json").exists(), "Cannot retrofit a marker after submission")
        atomic_json(record_path, record, exclusive=True)
    return wire_text


def _quiescence(path):
    process = _read(path / "process.json")
    pid = process.get("pid")
    require(
        type(pid) is int
        and pid > 1
        and isinstance(process.get("started"), str)
        and process["started"].strip(),
        "Recorded process identity is incomplete",
    )
    # The Codex adapter starts a new session: its PID is also its process-group ID.
    # Conservatively reject a reused live PID/group rather than infer it is unrelated.
    for probe in (os.kill, os.killpg):
        try:
            probe(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise TransitionBlocked("Cannot establish native process quiescence") from exc
        else:
            raise TransitionBlocked("Recorded native process or process group is still live")
    return {
        "pid": pid,
        "started": process["started"],
        "observed_at": utcnow(),
        "leader_absent": True,
        "process_group_absent": True,
        "process_record_digest": digest(process),
    }


def reconcile_native_completion(invocation_path):
    """Return exact terminal evidence, or block; this function cannot submit/retry work."""
    path = Path(invocation_path)
    intent = EvidenceStore.reconcile(path)
    require((path / "requested.json").is_file(), "Invocation was not recorded as submitted")
    request = _read(path / "native-request.json")
    identity = {
        key: intent[key] for key in ("run_id", "operation_id", "invocation_id", "request_digest")
    }
    require(
        all(request.get(key) == value for key, value in identity.items()),
        "Native request identity differs",
    )
    expected_marker = (
        "[harness-invocation " + json.dumps(identity, sort_keys=True, separators=(",", ":")) + "]"
    )
    require(request.get("marker") == expected_marker, "Native invocation marker differs")
    session_record = _read(path / "session.json")
    session = session_record.get("native_session_id")
    try:
        require(str(UUID(session)) == session, "Native session identity is not canonical")
    except (TypeError, ValueError, AttributeError) as exc:
        raise TransitionBlocked("Native session identity is invalid") from exc
    home = Path(intent["binding"].get("auth_context", ""))
    require(home.is_absolute(), "Bound native authentication context is missing")
    root = home / "sessions"
    try:
        records, evidence_digest = native_records(session, root)
    except (OSError, ValueError, TypeError) as exc:
        raise TransitionBlocked("Native evidence cannot be read completely") from exc
    require(
        all(isinstance(row, dict) and isinstance(row.get("payload"), dict) for row in records),
        "Native record structure is malformed",
    )
    metadata = [row["payload"] for row in records if row.get("type") == "session_meta"]
    require(
        len(metadata) == 1 and metadata[0].get("id") == session, "Native session metadata differs"
    )
    matches = []
    for index, row in enumerate(records):
        value = row["payload"]
        if (
            row.get("type") != "response_item"
            or value.get("type") != "message"
            or value.get("role") != "user"
        ):
            continue
        content = value.get("content")
        if not isinstance(content, list) or not all(isinstance(part, dict) for part in content):
            continue
        require(
            all(
                isinstance(part.get("text"), str)
                for part in content
                if part.get("type") == "input_text"
            ),
            "Native input text is malformed",
        )
        text = "".join(part["text"] for part in content if part.get("type") == "input_text")
        if expected_marker in text:
            require(
                text.startswith(expected_marker + "\n")
                and sha256(text.encode()).hexdigest() == request.get("wire_sha256")
                and sha256(text.split("\n", 1)[1].encode()).hexdigest()
                == request.get("prompt_sha256"),
                "Native submitted prompt differs from the retained request",
            )
            require(
                all(part.get("type") == "input_text" for part in content),
                "Unexpected native input attachments",
            )
            matches.append(index)
    require(len(matches) == 1, "Exact native invocation request is absent or ambiguous")
    message_index = matches[0]
    starts = [
        (index, row["payload"])
        for index, row in enumerate(records)
        if row.get("type") == "event_msg"
        and row["payload"].get("type") == "task_started"
        and index < message_index
    ]
    require(starts, "Native turn start is missing")
    start_index, start = starts[-1]
    turn = start.get("turn_id")
    try:
        require(str(UUID(turn)) == turn, "Native turn identity is not canonical")
    except (TypeError, ValueError, AttributeError) as exc:
        raise TransitionBlocked("Native turn identity is invalid") from exc
    require(
        sum(
            row.get("type") == "event_msg"
            and row["payload"].get("type") == "task_started"
            and row["payload"].get("turn_id") == turn
            for row in records
        )
        == 1,
        "Native turn start is ambiguous",
    )
    completions = [
        (index, row["payload"])
        for index, row in enumerate(records)
        if row.get("type") == "event_msg"
        and row["payload"].get("type") == "task_complete"
        and row["payload"].get("turn_id") == turn
    ]
    require(len(completions) == 1, "Native terminal completion is absent or ambiguous")
    end_index, completed = completions[0]
    require(start_index < message_index < end_index, "Native request is outside the completed turn")
    window = records[start_index + 1 : end_index]
    require(
        not completed.get("error") and completed.get("status", "completed") == "completed",
        "Native turn failed",
    )
    require(
        not any(
            row.get("type") == "event_msg"
            and row["payload"].get("type")
            in {
                "task_started",
                "task_complete",
                "turn_aborted",
                "task_failed",
                "turn_failed",
                "error",
            }
            for row in window
        ),
        "Native turn contains conflicting lifecycle evidence",
    )
    contexts = [row["payload"] for row in window if row.get("type") == "turn_context"]
    require(
        contexts and all(context.get("turn_id") == turn for context in contexts),
        "Native turn context differs",
    )
    require(
        not any(
            row.get("type") == "event_msg"
            and row["payload"].get("type")
            in {"task_started", "task_complete", "turn_aborted", "task_failed", "turn_failed"}
            for row in records[end_index + 1 :]
        ),
        "Native session advanced after this invocation",
    )
    text = completed.get("last_agent_message")
    require(isinstance(text, str) and text.strip(), "Completed native response is missing")
    quiescence = _quiescence(path)
    # Detect concurrent append/replacement across the process observation window.
    try:
        _, after_digest = native_records(session, root)
    except (OSError, ValueError, TypeError) as exc:
        raise TransitionBlocked("Native evidence changed during reconciliation") from exc
    require(after_digest == evidence_digest, "Native evidence changed during reconciliation")
    return {
        **identity,
        "native_session_id": session,
        "native_turn_id": turn,
        "outcome": "native_completed",
        "text": text,
        "response_sha256": sha256(text.encode()).hexdigest(),
        "native_evidence_sha256": evidence_digest,
        "session_record_digest": digest(session_record),
        "request_record_digest": digest(request),
        "quiescence": quiescence,
        "usage_coverage": "not established by completion reconciliation",
    }
