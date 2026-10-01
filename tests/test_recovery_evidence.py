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
    assert result["digest"] == sha256(
        json.dumps(
            result["packet"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
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
