"""AI attribution: Generated with AI assistance.

Verify immutable recovery evidence without replaying work or changing repositories.
"""

from __future__ import annotations

import copy
import json
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from backlog_harness.provider import Item, TransitionBlocked, git
from backlog_harness.recovery import validate_packet


def _write_jsonl(path: Path, records: list[dict]) -> str:
    data = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for record in records
    )
    path.write_bytes(data)
    return sha256(data).hexdigest()


def _recovery_case(tmp_path: Path) -> tuple[dict, Item, Path, Path]:
    repository = tmp_path / "candidate"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    (repository / "answer.txt").write_text("before\n")
    git(repository, "add", "answer.txt")
    git(repository, "commit", "-m", "Base")
    base = git(repository, "rev-parse", "HEAD")
    (repository / "answer.txt").write_text("after\n")
    git(repository, "add", "answer.txt")
    git(repository, "commit", "-m", "Candidate")
    head = git(repository, "rev-parse", "HEAD")

    evidence = repository / ".agents/temp/result.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"verdict":"GOOD"}\n')
    exclude = repository / ".git/info/exclude"
    exclude.write_text(exclude.read_text() + "\n.agents/\n")

    native_session_id = str(uuid4())
    final_turn_id = str(uuid4())
    runtime = tmp_path / f"rollout-{native_session_id}.jsonl"
    runtime_hash = _write_jsonl(
        runtime,
        [
            {
                "type": "session_meta",
                "payload": {"id": native_session_id},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": final_turn_id},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": final_turn_id},
            },
        ],
    )
    item = Item(
        item_id="item-one",
        path="backlog/item-one.md",
        revision="revision-one",
        state="Running",
        owner="desktop-owner",
        original_high=100,
        content="item",
    )
    packet = {
        "version": 1,
        "item_id": item.item_id,
        "revision": item.revision,
        "previous_owner": item.owner,
        "runtime_records": [
            {
                "path": str(runtime.resolve()),
                "sha256": runtime_hash,
                "native_session_id": native_session_id,
                "final_turn_id": final_turn_id,
            }
        ],
        "candidate": {
            "checkout": str(repository.resolve()),
            "head": head,
            "base": base,
            "allowed_paths": ["answer.txt"],
            "evidence": [
                {
                    "path": ".agents/temp/result.json",
                    "sha256": sha256(evidence.read_bytes()).hexdigest(),
                }
            ],
        },
        "remaining_high": 25,
        "scope": {
            "allowed_paths": ["answer.txt"],
            "checks": [["/usr/bin/env", "python3", "-m", "pytest", "tests/test_answer.py"]],
        },
        "historical_usage": "unknown",
    }
    return packet, item, repository, runtime


def test_validate_packet_returns_normalized_immutable_evidence(tmp_path):
    packet, item, repository, _ = _recovery_case(tmp_path)
    original = copy.deepcopy(packet)

    result = validate_packet(packet, item, repository)

    assert packet == original
    assert result["packet"]["candidate"]["checkout"] == str(repository.resolve())
    assert result["packet"]["runtime_records"] == packet["runtime_records"]
    assert (
        result["digest"]
        == sha256(
            json.dumps(
                result["packet"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )
    assert git(repository, "status", "--porcelain") == ""
    assert git(repository, "rev-parse", "HEAD") == packet["candidate"]["head"]


def test_validate_packet_rejects_missing_runtime_records(tmp_path):
    packet, item, repository, _ = _recovery_case(tmp_path)
    del packet["runtime_records"]

    with pytest.raises(TransitionBlocked, match="runtime_records"):
        validate_packet(packet, item, repository)


def test_validate_packet_rejects_wrong_previous_owner(tmp_path):
    packet, item, repository, _ = _recovery_case(tmp_path)
    packet["previous_owner"] = "another-owner"

    with pytest.raises(TransitionBlocked, match="previous owner"):
        validate_packet(packet, item, repository)


def test_validate_packet_rejects_dirty_candidate(tmp_path):
    packet, item, repository, _ = _recovery_case(tmp_path)
    (repository / "answer.txt").write_text("changed after candidate\n")

    with pytest.raises(TransitionBlocked, match="dirty"):
        validate_packet(packet, item, repository)


def test_validate_packet_rejects_stale_runtime_record(tmp_path):
    packet, item, repository, runtime = _recovery_case(tmp_path)
    runtime.write_text(runtime.read_text() + "{}\n")

    with pytest.raises(TransitionBlocked, match="runtime record hash"):
        validate_packet(packet, item, repository)


@pytest.mark.parametrize("later_type", ["task_started", "task_complete"])
def test_validate_packet_rejects_later_runtime_lifecycle_event(tmp_path, later_type):
    packet, item, repository, runtime = _recovery_case(tmp_path)
    records = [json.loads(line) for line in runtime.read_text().splitlines()]
    records.append(
        {
            "type": "event_msg",
            "payload": {"type": later_type, "turn_id": str(uuid4())},
        }
    )
    packet["runtime_records"][0]["sha256"] = _write_jsonl(runtime, records)

    with pytest.raises(TransitionBlocked, match="later lifecycle event"):
        validate_packet(packet, item, repository)


def _ready_recovery_case(tmp_path):
    from dataclasses import replace

    packet, item, repository, runtime = _recovery_case(tmp_path)
    execution_id = "prior-execution/item-one"
    remaining = "Finish semantic validation, independent review and exact-candidate approval."
    content = f"Execution: {execution_id}\nCandidate: {packet['candidate']['head']}\n{remaining}\n"
    item = replace(item, state="Ready", owner="Unowned", content=content)
    receipt = tmp_path / "preserved-receipt.json"
    snapshot_op = execution_id + "/3/snapshot"
    operations = {
        r["native_session_id"]: execution_id + "/3/code" for r in packet["runtime_records"]
    }
    receipts = {snapshot_op: {"receipt": {"candidate_sha": packet["candidate"]["head"]}}}
    for session, operation in operations.items():
        receipts[operation] = {
            "receipt": {
                "op_id": operation,
                "session_id": session,
                "started_unix": 1,
                "finished_unix": 2,
            }
        }
    receipt.write_text(json.dumps(receipts))
    packet["previous_owner"] = item.owner
    packet["preserved_execution"] = {
        "execution_id": execution_id,
        "snapshot_operation": snapshot_op,
        "runtime_operations": operations,
        "candidate_approval_required": True,
        "canonical_sha256": sha256(content.encode()).hexdigest(),
        "remaining_work": remaining,
        "receipt": {"path": str(receipt), "sha256": sha256(receipt.read_bytes()).hexdigest()},
    }
    return packet, item, repository, runtime


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "owner",
        "revision",
        "execution",
        "candidate",
        "receipt",
        "gates",
        "live",
        "approval",
        "snapshot",
        "session",
    ],
)
def test_ready_preserved_execution_validation(tmp_path, defect):
    from dataclasses import replace

    packet, item, repository, runtime = _ready_recovery_case(tmp_path)
    if defect == "owner":
        item = replace(item, owner="live-owner")
    elif defect == "revision":
        packet["revision"] = "stale"
    elif defect == "execution":
        packet["preserved_execution"]["execution_id"] = "other-execution"
    elif defect == "candidate":
        packet["candidate"]["head"] = packet["candidate"]["base"]
    elif defect == "receipt":
        Path(packet["preserved_execution"]["receipt"]["path"]).write_text("changed")
    elif defect == "approval":
        packet["preserved_execution"]["candidate_approval_required"] = False
    elif defect in {"snapshot", "session"}:
        preserved = packet["preserved_execution"]
        path = Path(preserved["receipt"]["path"])
        receipt = json.loads(path.read_text())
        if defect == "snapshot":
            receipt[preserved["snapshot_operation"]]["receipt"]["candidate_sha"] = packet[
                "candidate"
            ]["base"]
        else:
            operation = next(iter(preserved["runtime_operations"].values()))
            receipt[operation]["receipt"]["session_id"] = str(uuid4())
        path.write_text(json.dumps(receipt))
        preserved["receipt"]["sha256"] = sha256(path.read_bytes()).hexdigest()
    elif defect == "gates":
        packet["preserved_execution"]["remaining_work"] = "Skip approval"
    elif defect == "live":
        rows = [json.loads(line) for line in runtime.read_text().splitlines()]
        rows.append(
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(uuid4())}}
        )
        packet["runtime_records"][0]["sha256"] = _write_jsonl(runtime, rows)
    if defect:
        with pytest.raises(TransitionBlocked):
            validate_packet(packet, item, repository)
    else:
        assert validate_packet(packet, item, repository)["packet"] == packet


@pytest.mark.parametrize(
    "defect",
    [
        "json",
        "list",
        "snapshot-list",
        "runtime-list",
        "nested-list",
        "boolean-time",
        "infinite-time",
    ],
)
def test_ready_recovery_rejects_malformed_execution_receipt(tmp_path, defect):
    packet, item, repository, _ = _ready_recovery_case(tmp_path)
    preserved = packet["preserved_execution"]
    path = Path(preserved["receipt"]["path"])
    receipt = json.loads(path.read_text())
    op = next(iter(preserved["runtime_operations"].values()))
    if defect == "list":
        receipt = []
    elif defect == "snapshot-list":
        receipt[preserved["snapshot_operation"]] = []
    elif defect == "runtime-list":
        receipt[op] = []
    elif defect == "nested-list":
        receipt[op]["receipt"] = []
    elif defect == "boolean-time":
        receipt[op]["receipt"]["started_unix"] = True
    elif defect == "infinite-time":
        receipt[op]["receipt"]["finished_unix"] = float("inf")
    path.write_text("not-json" if defect == "json" else json.dumps(receipt))
    preserved["receipt"]["sha256"] = sha256(path.read_bytes()).hexdigest()
    with pytest.raises(TransitionBlocked):
        validate_packet(packet, item, repository)
