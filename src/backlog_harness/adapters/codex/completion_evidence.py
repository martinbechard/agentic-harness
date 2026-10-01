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


def reconcile_native_accounting(path, events, telemetry_path, provider_id=None):
    """Derive accounting after supervisor loss; receiver diagnostic history stays unknown."""
    from ...evidence import read_jsonl
    from ...native_evidence import child_usage
    from ...telemetry import Sink, normalize, spans

    path, telemetry_path = Path(path), Path(telemetry_path)
    intent = EvidenceStore.reconcile(path)
    proof = reconcile_native_completion(path)
    require(events and events[-1].get("type") == "turn.completed", "Terminal stdout is missing")
    require(
        all(event.get("invocation_id") == intent["invocation_id"] for event in events),
        "Event invocation differs",
    )
    require(
        not any(event.get("type") in {"error", "turn.failed"} for event in events),
        "Failed native output",
    )
    messages = [event.get("text") for event in events if event.get("item_type") == "agent_message"]
    require(messages and messages[-1] == proof["text"], "Native and stdout final results differ")
    root = Path(intent["binding"]["auth_context"]) / "sessions"
    records, fingerprint = native_records(proof["native_session_id"], root)
    require(fingerprint == proof["native_evidence_sha256"], "Native evidence changed")
    previous, counter, active = 0, None, False
    last_counter, last_output = -1, -1
    selected = []
    for index, row in enumerate(records):
        value = row["payload"]
        if row.get("type") == "event_msg" and value.get("type") == "task_started":
            active = value.get("turn_id") == proof["native_turn_id"]
        if active:
            selected.append(row)
            if (row.get("type") == "response_item" and value.get("role") != "user") or (
                row.get("type") == "event_msg"
                and value.get("type") in {"agent_message", "agent_reasoning"}
            ):
                last_output = index
        if (
            row.get("type") == "event_msg"
            and value.get("type") == "token_count"
            and value.get("info") is not None
        ):
            amount = (value.get("info") or {}).get("total_token_usage", {}).get("output_tokens")
            require(type(amount) is int and amount >= 0, "Native usage counter is missing")
            if active:
                counter = amount
                last_counter = index
            elif counter is None:
                previous = amount
        if row.get("type") == "event_msg" and value.get("type") == "task_complete":
            active = False
    require(type(counter) is int and counter >= previous, "Native cumulative usage is unproven")
    require(last_counter > last_output, "Native usage has uncovered model output")
    parent_records = [
        r
        for r in selected
        if r.get("type") == "token_usage_record"
        and r["payload"].get("session_id") == proof["native_session_id"]
    ]
    model_items = [
        r["payload"]
        for r in selected
        if r.get("type") == "event_msg"
        and r["payload"].get("type") in {"item_started", "item_completed"}
        and r["payload"].get("item", {}).get("type") in {"Reasoning", "AgentMessage"}
    ]
    if model_items:
        from datetime import datetime

        require(parent_records, "Parent usage coverage is missing")
        end = datetime.fromisoformat(parent_records[-1]["timestamp"]).timestamp() * 1000
        require(
            all(
                type(v.get("started_at_ms")) is int and v["started_at_ms"] <= end
                for v in model_items
            ),
            "Native usage has uncovered model output",
        )
    require(
        events[-1].get("usage", {}).get("output_tokens") == counter,
        "Native and stdout usage differ",
    )
    children, child_ids = child_usage(
        proof["native_session_id"], events[0]["at"], events[-1]["at"], root
    )
    require(type(children) is int, "Native child accounting is incomplete")
    rows, _, partial = read_jsonl(telemetry_path)
    require(not partial and rows, "Telemetry is missing or partial")
    correlation = {
        "harness.run.id": intent["run_id"],
        "harness.invocation.id": intent["invocation_id"],
        "harness.agent.role": intent["binding"]["role"],
        "harness.agent.adapter": intent["binding"]["adapter"],
    }
    if intent.get("item_id") is not None:
        require(provider_id, "Provider attribution is missing")
        correlation.update(
            {"harness.work_item.id": intent["item_id"], "harness.provider.id": provider_id}
        )
    for payload in rows:
        require(normalize(payload, correlation) == payload, "Telemetry attribution is incomplete")
    sink = Sink(telemetry_path, {})
    seen, observed, found = set(), 0, False
    for payload in rows:
        for _, _, span in spans(payload):
            attrs = {entry["key"]: entry["value"] for entry in span.get("attributes", [])}
            require(
                attrs.get("harness.invocation.id") == {"stringValue": intent["invocation_id"]},
                "Telemetry invocation differs",
            )
            identity = span["traceId"].lower(), span["spanId"].lower()
            if identity in seen:
                continue
            seen.add(identity)
            if "gen_ai.usage.output_tokens" in attrs:
                amount = attrs["gen_ai.usage.output_tokens"].get("intValue")
                require(
                    type(amount) in {int, str} and str(amount).isdigit(),
                    "Telemetry usage is malformed",
                )
                observed += int(amount)
                found = True
    require(
        found and observed == counter - previous + children,
        "Native and telemetry usage do not reconcile",
    )
    prefixes = []
    for session_id in [proof["native_session_id"], *child_ids]:
        paths = list(root.glob(f"*/*/*/*{session_id}.jsonl"))
        require(len(paths) == 1 and not paths[0].is_symlink(), "Native session evidence differs")
        data = paths[0].read_bytes()
        if session_id == proof["native_session_id"]:
            require(
                sha256(data).hexdigest() == fingerprint, "Native evidence changed during recovery"
            )
        prefixes.append(
            {"session_id": session_id, "bytes": len(data), "sha256": sha256(data).hexdigest()}
        )
    # Prefixes permit later legitimate turns while detecting changes to reviewed history.
    return {
        "version": 1,
        "invocation_id": intent["invocation_id"],
        "intent_digest": digest(
            {key: value for key, value in intent.items() if key not in {"outcome", "partial"}}
        ),
        "request_digest": digest(_read(path / "native-request.json")),
        "session_digest": digest(_read(path / "session.json")),
        "native_turn_id": proof["native_turn_id"],
        "native_prefixes": prefixes,
        "events_digest": digest(events),
        "telemetry_sha256": sink.evidence_digest(),
        "span_count": len(sink.seen),
        "parent_previous": previous,
        "parent_cumulative": counter,
        "child_output": children,
        "observed_output": observed,
    }


def validate_recovered_accounting(result):
    """Validate the saved native accounting proof without assuming receiver counters."""
    from ...telemetry import Sink

    path = Path(result["evidence_path"])
    report = result["telemetry"]
    proof = _read(path / "native-accounting.json")
    require(
        report.get("rejected_exports") is None
        and report.get("coverage") == "native_usage_reconciled",
        "Not a native accounting recovery receipt",
    )
    require(
        digest(proof) == report.get("native_accounting_sha256"), "Native accounting receipt changed"
    )
    intent = EvidenceStore.reconcile(path)
    require(
        proof["invocation_id"] == result["invocation_id"] == intent["invocation_id"],
        "Recovered invocation differs",
    )
    require(
        proof["intent_digest"]
        == digest(
            {key: value for key, value in intent.items() if key not in {"outcome", "partial"}}
        ),
        "Recovered intent differs",
    )
    require(proof["events_digest"] == digest(result["events"]), "Recovered events differ")
    require(
        proof["request_digest"] == digest(_read(path / "native-request.json"))
        and proof["session_digest"] == digest(_read(path / "session.json")),
        "Recovered request or session differs",
    )
    sink = Sink(Path(result["telemetry_path"]), {})
    require(
        sink.evidence_digest() == proof["telemetry_sha256"] == report["evidence_sha256"]
        and len(sink.seen) == proof["span_count"] == report["span_count"],
        "Recovered telemetry differs",
    )
    root = Path(intent["binding"]["auth_context"]) / "sessions"
    for prefix in proof["native_prefixes"]:
        paths = list(root.glob(f"*/*/*/*{prefix['session_id']}.jsonl"))
        require(
            len(paths) == 1 and not paths[0].is_symlink(), "Recovered native session is missing"
        )
        with paths[0].open("rb") as stream:
            data = stream.read(prefix["bytes"])
        require(
            len(data) == prefix["bytes"] and sha256(data).hexdigest() == prefix["sha256"],
            "Recovered native history changed",
        )
    return proof
