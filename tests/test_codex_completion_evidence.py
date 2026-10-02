"""Retained Codex turn shapes; no CLI/model invocation is made by reconciliation."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from backlog_harness.adapters.codex.completion_evidence import (
    prepare_native_request,
    reconcile_native_completion,
)
from backlog_harness.contracts import digest, load_config
from backlog_harness.evidence import EvidenceStore, atomic_json
from backlog_harness.provider import TransitionBlocked


@pytest.fixture
def retained(config_file, tmp_path):
    config, _ = config_file
    snapshot = load_config(config)
    home = tmp_path / "native-home"
    binding = replace(snapshot.binding("orchestrator"), auth_context=str(home))
    store = EvidenceStore(tmp_path / "evidence", "run")
    path = store.begin(
        "item:answer",
        str(uuid4()),
        snapshot,
        binding,
        action="answer",
        item_id="item",
        request_digest=digest("Exact supplied answer"),
    )
    prompt = prepare_native_request(path, "Exact supplied answer")
    # Shape normalized from the retained real ACP producer rollout dated 2026-10-01:
    # task_started -> context/user message -> task_complete with last_agent_message.
    # All text, IDs and timestamps below are fixture data; no thoughts/auth copied.
    session, turn = str(uuid4()), str(uuid4())
    records = [
        {"type": "session_meta", "payload": {"id": session}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        {"type": "turn_context", "payload": {"turn_id": turn}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "fixture-user",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn,
                "last_agent_message": '{"candidate":"fixture","request_completion":true}',
            },
        },
    ]
    native = home / "sessions/2026/10/01" / f"rollout-fixture-{session}.jsonl"
    native.parent.mkdir(parents=True)

    def save():
        native.write_text("".join(json.dumps(row) + "\n" for row in records))

    save()
    EvidenceStore.session(path, {"session_id": "portable", "native_session_id": session})
    EvidenceStore.requested(path)
    # Real isolated local process identity; terminate before recovery. This makes
    # quiescence checks genuine OS observations, not a caller-supplied true flag.
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        started = (
            subprocess.run(
                ["ps", "-p", str(process.pid), "-o", "lstart="], capture_output=True, check=True
            )
            .stdout.decode()
            .strip()
        )
        assert started
        atomic_json(path / "process.json", {"pid": process.pid, "started": started})
    finally:
        process.terminate()
        process.wait(timeout=5)
    return path, native, records, save, session, turn


def test_exact_retained_completed_turn_recovers_without_prompt_or_mutation(retained, monkeypatch):
    path, native, records, _save, session, turn = retained
    before = native.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Reconciliation must not launch a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = reconcile_native_completion(path)
    assert result["native_session_id"] == session and result["native_turn_id"] == turn
    assert result["text"] == records[-1]["payload"]["last_agent_message"]
    assert result["native_evidence_sha256"] == sha256(before).hexdigest()
    assert result["quiescence"]["leader_absent"] and result["quiescence"]["process_group_absent"]
    assert result["usage_coverage"] == "not established by completion reconciliation"
    assert native.read_bytes() == before and not (path / "result.json").exists()
    assert reconcile_native_completion(path)["response_sha256"] == result["response_sha256"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing-marker",
        "changed-request",
        "duplicate-request",
        "failed",
        "aborted",
        "missing-completion",
        "duplicate-completion",
        "later-live",
        "later-complete",
        "wrong-context",
        "wrong-session",
        "duplicate-start",
        "missing-process",
        "live-process",
        "partial",
        "old-unmarked",
        "wrong-invocation",
        "malformed-text",
        "wrong-turn",
        "invalid-start-turn",
        "malformed-content",
        "missing-response",
    ],
)
def test_missing_ambiguous_failed_or_live_evidence_cannot_advance(retained, fault):
    path, native, rows, save, _session, turn = retained
    if fault == "missing-marker":
        rows[3]["payload"]["content"][0]["text"] = "Exact supplied answer"
    elif fault == "changed-request":
        rows[3]["payload"]["content"][0]["text"] += " changed"
    elif fault == "malformed-text":
        rows[3]["payload"]["content"][0]["text"] = None
    elif fault == "invalid-start-turn":
        rows[1]["payload"]["turn_id"] = "not-a-uuid"
    elif fault == "malformed-content":
        rows[3]["payload"]["content"] = "not a content list"
    elif fault == "wrong-turn":
        rows[-1]["payload"]["turn_id"] = str(uuid4())
    elif fault == "missing-response":
        rows[-1]["payload"].pop("last_agent_message")
    elif fault == "duplicate-request":
        rows.insert(4, rows[3])
    elif fault == "failed":
        rows[-1]["payload"]["error"] = {"message": "failed"}
    elif fault == "aborted":
        rows.insert(4, {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn}})
    elif fault == "missing-completion":
        rows.pop()
    elif fault == "duplicate-completion":
        rows.append(rows[-1])
    elif fault in {"later-live", "later-complete"}:
        other = str(uuid4())
        rows.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": other}})
        if fault == "later-complete":
            rows.append(
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": other}}
            )
    elif fault == "wrong-context":
        rows[2]["payload"]["turn_id"] = str(uuid4())
    elif fault == "wrong-session":
        rows[0]["payload"]["id"] = str(uuid4())
    elif fault == "duplicate-start":
        rows.insert(2, rows[1])
    elif fault == "missing-process":
        (path / "process.json").unlink()
    elif fault == "live-process":
        atomic_json(path / "process.json", {"pid": os.getpid(), "started": "live fixture parent"})
    elif fault == "old-unmarked":
        (path / "native-request.json").unlink()
    elif fault == "wrong-invocation":
        marker = json.loads((path / "native-request.json").read_text())
        marker["invocation_id"] = str(uuid4())
        atomic_json(path / "native-request.json", marker)
    save()
    if fault == "partial":
        native.write_bytes(native.read_bytes().rstrip(b"\n"))
    with pytest.raises(TransitionBlocked):
        reconcile_native_completion(path)


def test_old_request_cannot_receive_retroactive_marker(retained):
    path, *_ = retained
    (path / "native-request.json").unlink()
    with pytest.raises(TransitionBlocked, match="retrofit"):
        prepare_native_request(path, "Exact supplied answer")


def test_quiescent_leader_does_not_hide_live_process_group(retained, monkeypatch):
    path, *_ = retained

    def absent(pid, signal):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", absent)
    monkeypatch.setattr(os, "killpg", lambda pid, signal: None)
    with pytest.raises(TransitionBlocked, match="group is still live"):
        reconcile_native_completion(path)


def test_changed_native_evidence_during_quiescence_blocks(retained, monkeypatch):
    path, native, _rows, _save, *_ = retained

    def append_after_read(pid, signal):
        native.write_bytes(
            native.read_bytes()
            + json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}).encode()
            + b"\n"
        )
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", append_after_read)
    with pytest.raises(TransitionBlocked, match="changed during"):
        reconcile_native_completion(path)


@pytest.mark.parametrize("fault", ["malformed-receipt", "invalid-session", "uninspectable-process"])
def test_unreadable_completion_authority_cannot_be_recovered(retained, monkeypatch, fault):
    path, *_ = retained
    if fault == "malformed-receipt":
        (path / "native-request.json").write_text("invalid json")
        message = "malformed"
    elif fault == "invalid-session":
        atomic_json(path / "session.json", {"native_session_id": "not-a-uuid"})
        message = "identity is invalid"
    else:

        def denied(pid, sig):
            raise PermissionError("inspection denied")

        monkeypatch.setattr(os, "kill", denied)
        message = "Cannot establish native process quiescence"
    with pytest.raises(TransitionBlocked, match=message):
        reconcile_native_completion(path)
    assert not (path / "result.json").exists()


def test_matching_retained_request_reuses_marker_but_changed_prompt_is_rejected(retained):
    path, *_ = retained
    before = (path / "native-request.json").read_bytes()
    assert prepare_native_request(path, "Exact supplied answer").endswith("Exact supplied answer")
    assert (path / "native-request.json").read_bytes() == before
    with pytest.raises(TransitionBlocked, match="marker differs"):
        prepare_native_request(path, "Different request")
    assert (path / "native-request.json").read_bytes() == before


@pytest.mark.parametrize("change", ["deleted", "partial"])
def test_native_evidence_lost_during_quiescence_blocks(retained, monkeypatch, change):
    path, native, *_ = retained

    def change_after_read(pid, signal):
        if change == "deleted":
            native.unlink(missing_ok=True)
        else:
            native.write_text("{invalid json}\n")
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", change_after_read)
    with pytest.raises(TransitionBlocked, match="changed during|absent or ambiguous"):
        reconcile_native_completion(path)
    assert not (path / "result.json").exists()
