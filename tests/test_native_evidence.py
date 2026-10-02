import json
from uuid import uuid4

import pytest

from backlog_harness.contracts import digest
from backlog_harness.native_evidence import coordination_instructions, verify_native_review
from backlog_harness.provider import TransitionBlocked


@pytest.mark.parametrize("verdict_source", ["completion", "message", "fenced", "missing"])
def test_native_child_freshness_candidate_verdict(tmp_path, verdict_source):
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
    verdict = child[-1]["payload"]["last_agent_message"]
    if verdict_source != "completion":
        del child[-1]["payload"]["last_agent_message"]
        if verdict_source != "missing":
            child.insert(
                1,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": f"```json\n{verdict}\n```"
                        if verdict_source == "fenced"
                        else verdict,
                    },
                },
            )
    # An unrelated truncated session must not prevent discovery of the real child.
    (root / "unrelated.jsonl").write_text("{unfinished")
    write(producer, parent)
    write(reviewer, child)
    if verdict_source == "missing":
        with pytest.raises(TransitionBlocked, match="verdict is missing"):
            verify_native_review(producer, "/root/review", candidate, tmp_path)
        return
    proof = verify_native_review(producer, "/root/review", candidate, tmp_path)
    assert proof["reviewer_session"] == reviewer and proof["fresh_context"]
    with pytest.raises(TransitionBlocked):
        verify_native_review(producer, "/root/review", "b" * 40, tmp_path)
    parent[0]["payload"]["arguments"] = json.dumps({"fork_turns": "all"})
    write(producer, parent)
    with pytest.raises(TransitionBlocked, match="Fresh"):
        verify_native_review(producer, "/root/review", candidate, tmp_path)


def test_native_review_coordination_wording_does_not_replace_evidence(tmp_path):
    root = tmp_path / "2026/10/02"
    root.mkdir(parents=True)
    producer, reviewer = str(uuid4()), str(uuid4())
    candidate = "a" * 40
    context = {
        "version": 1,
        "config_digest": "1" * 64,
        "source_revision": "2" * 64,
        "resource_coordination": "resource-claim",
        "claims_required": False,
        "claim_exemption": "Active recovery prohibits claims",
        "policy_digest": "3" * 64,
        "evidence": [],
    }

    def write(identity, records):
        (root / f"rollout-{identity}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )

    def parent(message):
        return [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call",
                    "arguments": json.dumps(
                        {"fork_turns": "none", "task_name": "review", "message": message}
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
    write(reviewer, child)
    # Appending another section used to make END COORDINATION CONTEXT count as
    # a second start marker. Neither layout nor copying proves reviewer behavior.
    messages = [
        "Review exact candidate." + coordination_instructions(context),
        "Review exact candidate."
        + coordination_instructions(context)
        + "\nSOURCE REVIEW PACKET\n{}",
        "Review exact candidate; follow the project's coordination rules.",
        "Review exact candidate." + coordination_instructions({**context, "claims_required": True}),
    ]
    for message in messages:
        write(producer, parent(message))
        assert (
            verify_native_review(
                producer,
                "/root/review",
                candidate,
                tmp_path,
            )["verdict"]
            == "ACCEPT"
        )
        with pytest.raises(TransitionBlocked, match="candidate"):
            verify_native_review(
                producer,
                "/root/review",
                "b" * 40,
                tmp_path,
            )

    # Perfect prompt copying still cannot substitute for an independent verdict.
    write(producer, parent(coordination_instructions(context)))
    child[-1]["payload"]["last_agent_message"] = json.dumps(
        {"candidate": candidate, "verdict": "REJECT", "unresolved_findings": ["Defect"]}
    )
    write(reviewer, child)
    with pytest.raises(TransitionBlocked, match="did not accept"):
        verify_native_review(
            producer,
            "/root/review",
            candidate,
            tmp_path,
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "reordered",
        "extra-fields",
        "assessment-missing",
        "assessment-candidate",
        "requirements-digest",
        "missing-conclusion",
        "duplicate-conclusion",
        "invalid-conclusion",
        "reject-conclusion",
        "weakening-finding",
        "evidence-path",
        "harness-check",
        "missing-check",
        "check-candidate",
        "check-command",
    ],
)
def test_selected_review_validates_substance_without_prompt_packets(tmp_path, fault):
    root = tmp_path / "2026/10/02"
    root.mkdir(parents=True)
    producer, reviewer = str(uuid4()), str(uuid4())
    candidate = "a" * 40
    commands = [["python", "-m", "focused"]]
    contract = {
        "requirements": [
            {
                "id": "strength",
                "canonical_reference": "item#strength",
                "acceptance_text": "Preserve evaluation strength",
            },
            {
                "id": "coverage",
                "canonical_reference": "item#coverage",
                "acceptance_text": "Retain catalog coverage",
            },
        ]
    }
    assessment = {
        "candidate": candidate,
        "requirements_digest": digest(contract),
        "unresolved_findings": [],
        "conclusions": [
            {
                "id": r["id"],
                "canonical_reference": r["canonical_reference"],
                "verdict": "ACCEPT",
                "conclusion": "Reviewed substantive requirement",
                "evidence_paths": ["role.yaml"],
            }
            for r in contract["requirements"]
        ],
    }
    checks = [{"candidate": candidate, "argv": commands[0], "returncode": 0}]
    if fault == "reordered":
        assessment["conclusions"].reverse()
    if fault == "extra-fields":
        assessment["conclusions"][0]["notes"] = "Additional useful context"
    if fault == "assessment-candidate":
        assessment["candidate"] = "b" * 40
    if fault == "requirements-digest":
        assessment["requirements_digest"] = "stale"
    if fault == "missing-conclusion":
        assessment["conclusions"].pop()
    if fault == "duplicate-conclusion":
        assessment["conclusions"][1] = assessment["conclusions"][0]
    if fault == "invalid-conclusion":
        assessment["conclusions"][0] = None
    if fault == "reject-conclusion":
        assessment["conclusions"][0]["verdict"] = "REJECT"
    if fault == "weakening-finding":
        assessment["unresolved_findings"] = ["Evaluation weakened"]
    if fault == "evidence-path":
        assessment["conclusions"][0]["evidence_paths"] = ["unrelated.py"]
    if fault == "harness-check":
        checks[0]["returncode"] = 1
    if fault == "missing-check":
        checks.clear()
    if fault == "check-candidate":
        checks[0]["candidate"] = "b" * 40
    if fault == "check-command":
        checks[0]["argv"] = ["unrelated"]
    verdict = {"candidate": candidate, "verdict": "ACCEPT", "unresolved_findings": []}
    if fault != "assessment-missing":
        verdict["source_review_assessment"] = assessment
    # No native command receipts, magic markers, copied output, or prompt JSON packet.
    parent = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "review",
                "arguments": json.dumps(
                    {
                        "fork_turns": "none",
                        "message": "Review the candidate against the selected requirements.",
                    }
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "review", "output": reviewer},
        },
    ]
    child = [
        {
            "type": "session_meta",
            "payload": {
                "id": reviewer,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": producer}}},
                "git": {"commit_hash": candidate},
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": json.dumps(verdict)},
        },
    ]
    for identity, records in [(producer, parent), (reviewer, child)]:
        (root / f"rollout-{identity}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in records)
        )
    context = {
        "commands": commands,
        "review_requirements": contract,
        "allowed_source_paths": ["role.yaml"],
        "harness_checks": checks,
    }
    if fault in (None, "reordered", "extra-fields"):
        result = verify_native_review(
            producer, reviewer, candidate, tmp_path, source_review=context
        )
        assert result["source_review_assessment"] == assessment
        assert "pre_review_checks" not in result
    else:
        with pytest.raises(TransitionBlocked):
            verify_native_review(producer, reviewer, candidate, tmp_path, source_review=context)


@pytest.mark.parametrize(
    "condition", ["complete", "running", "failed", "missing_usage", "invalid_usage"]
)
def test_child_usage_requires_completed_accountable_child(tmp_path, condition):
    from backlog_harness.native_evidence import child_usage

    producer, reviewer = str(uuid4()), str(uuid4())
    root = tmp_path / "2026/10/02"
    root.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": reviewer,
                "parent_thread_id": producer,
                "timestamp": "2026-10-02T12:00:01+00:00",
            },
        }
    ]
    if condition != "missing_usage":
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "output_tokens": "12" if condition == "invalid_usage" else 12,
                        }
                    },
                },
            }
        )
    if condition != "running":
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": "interrupted" if condition == "failed" else None,
                },
            }
        )
    (root / f"rollout-{reviewer}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    result = child_usage(
        producer, "2026-10-02T12:00:00+00:00", "2026-10-02T12:01:00+00:00", tmp_path
    )
    # Unknown usage must not become a zero-cost success that permits more spending.
    assert result == ((12, [reviewer]) if condition == "complete" else (None, []))
