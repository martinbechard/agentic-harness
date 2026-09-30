import json
from uuid import uuid4

import pytest

from backlog_harness.native_evidence import verify_native_review
from backlog_harness.provider import TransitionBlocked


def test_native_child_freshness_candidate_verdict(tmp_path):
    root = tmp_path / "2026/09/30"
    root.mkdir(parents=True)
    producer, reviewer = str(uuid4()), str(uuid4())
    candidate = "a" * 40

    def write(identity, records):
        (root / f"rollout-{identity}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )

    parent = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "call",
                "arguments": json.dumps(
                    {"fork_turns": "none", "task_name": "review", "message": "encrypted"}
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call",
                "output": json.dumps({"task_name": "/root/review"}),
            },
        },
    ]
    child = [
        {
            "type": "session_meta",
            "payload": {
                "id": reviewer,
                "parent_thread_id": producer,
                "agent_path": "/root/review",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": producer}}},
                "git": {"commit_hash": candidate},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": json.dumps(
                    {"candidate": candidate, "verdict": "ACCEPT", "unresolved_findings": []}
                ),
            },
        },
    ]
    write(producer, parent)
    write(reviewer, child)
    proof = verify_native_review(producer, "/root/review", candidate, tmp_path)
    assert proof["reviewer_session"] == reviewer and proof["fresh_context"]
    with pytest.raises(TransitionBlocked):
        verify_native_review(producer, "/root/review", "b" * 40, tmp_path)
    parent[0]["payload"]["arguments"] = json.dumps({"fork_turns": "all"})
    write(producer, parent)
    with pytest.raises(TransitionBlocked, match="Fresh"):
        verify_native_review(producer, "/root/review", candidate, tmp_path)
