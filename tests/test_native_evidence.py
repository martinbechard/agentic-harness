import json
from hashlib import sha256
from uuid import uuid4

import pytest

from backlog_harness.contracts import digest
from backlog_harness.native_evidence import coordination_instructions, verify_native_review
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


def test_native_review_requires_exact_bound_coordination_context(tmp_path):
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
    write(producer, parent("Review exact candidate." + coordination_instructions(context)))
    write(reviewer, child)
    assert verify_native_review(
        producer,
        "/root/review",
        candidate,
        tmp_path,
        coordination_context=context,
    )["verdict"] == "ACCEPT"
    write(producer, parent("Review exact candidate."))
    with pytest.raises(TransitionBlocked, match="coordination context"):
        verify_native_review(
            producer,
            "/root/review",
            candidate,
            tmp_path,
            coordination_context=context,
        )
    changed = {**context, "claims_required": True}
    write(producer, parent("Review exact candidate." + coordination_instructions(changed)))
    with pytest.raises(TransitionBlocked, match="differs"):
        verify_native_review(
            producer,
            "/root/review",
            candidate,
            tmp_path,
            coordination_context=context,
        )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "invented",
        "late",
        "output-hash",
        "truncated",
        "assessment-candidate",
        "missing-conclusion",
        "reject-conclusion",
        "weakening-finding",
        "harness-check",
    ],
)
def test_selected_source_review_binds_pre_spawn_checks_and_conclusions(tmp_path, fault):
    root = tmp_path / "2026/10/02"
    root.mkdir(parents=True)
    producer, reviewer = str(uuid4()), str(uuid4())
    candidate = "a" * 40
    commands = [["python", "-m", "focused"], ["git", "diff", "--check"]]
    outputs = [
        "Chunk ID: one\nProcess exited with code 0\nFinal output:\npassed\n",
        "Chunk ID: two\nProcess exited with code 0\nFinal output:\n",
    ]
    if fault == "truncated":
        outputs[0] += "Warning: truncated output\n"
    receipts = [
        {
            "candidate": candidate,
            "argv": command,
            "returncode": 0,
            "output": output,
            "output_sha256": sha256(output.encode()).hexdigest(),
        }
        for command, output in zip(commands, outputs)
    ]
    contract = {
        "provider_revision": "revision",
        "preparation_digest": "1" * 64,
        "original_preparation_digest": "2" * 64,
        "correction_resolution_digest": "3" * 64,
        "requirements": [
            {
                "id": "role-suite",
                "canonical_reference": "item#acceptance",
                "acceptance_text": "Preserve evaluation strength",
                "required_gate": {
                    "gate": "Role-suite agreement",
                    "requirement": "Verify role-suite agreement without weakening evaluation",
                },
            }
        ],
    }
    source_paths = [{"path": "role.yaml", "sha256": "4" * 64}]
    packet = {
        "version": 1,
        "candidate": candidate,
        "canonical_acceptance": "Canonical item",
        "preparation_evidence": {"decision_digest": "1" * 64},
        "review_requirements": contract,
        "requirements_digest": digest(contract),
        "source_evidence": source_paths,
        "pre_review_checks": receipts,
        "check_receipt_hashes": [digest(row) for row in receipts],
    }
    assessment = {
        "candidate": "b" * 40 if fault == "assessment-candidate" else candidate,
        "requirements_digest": digest(contract),
        "check_receipt_hashes": packet["check_receipt_hashes"],
        "unresolved_findings": (
            ["Passing checks do not preserve evaluation strength"]
            if fault == "weakening-finding"
            else []
        ),
        "conclusions": [
            {
                "id": "role-suite",
                "canonical_reference": "item#acceptance",
                "verdict": "REJECT" if fault == "reject-conclusion" else "ACCEPT",
                "conclusion": "Role, suite, and generated output agree",
                "evidence_paths": ["role.yaml"],
            }
        ],
    }
    if fault == "missing-conclusion":
        assessment["conclusions"] = []
    if fault == "output-hash":
        packet["pre_review_checks"][0]["output_sha256"] = "0" * 64

    parent = []

    def call(name, arguments, call_id, output):
        parent.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": name,
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "id": "output-" + call_id,
                        "call_id": call_id,
                        "output": output,
                    },
                },
            ]
        )

    anchor = "Process exited with code 0\nFinal output:\n" + candidate + "\n"
    call(
        "exec_command",
        {"cmd": "git rev-parse HEAD", "workdir": str(tmp_path)},
        "head-before",
        anchor,
    )
    if fault != "invented":
        for index, (command, output) in enumerate(zip(commands, outputs)):
            import shlex

            call(
                "exec_command",
                {"cmd": shlex.join(command), "workdir": str(tmp_path)},
                "check-" + str(index),
                output,
            )
    call(
        "exec_command",
        {"cmd": "git rev-parse HEAD", "workdir": str(tmp_path)},
        "head-after",
        anchor,
    )
    spawn = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "spawn",
                "arguments": json.dumps(
                    {
                        "fork_turns": "none",
                        "task_name": "review",
                        "message": "Assess exact evidence.\nSOURCE REVIEW PACKET\n"
                        + json.dumps(packet),
                    }
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "spawn",
                "output": json.dumps({"agent_id": reviewer}),
            },
        },
    ]
    if fault == "late":
        prior = parent[2:6]
        parent = parent[:2] + spawn + parent[6:] + prior
    else:
        parent += spawn
    child = [
        {
            "type": "session_meta",
            "payload": {
                "id": reviewer,
                "parent_thread_id": producer,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": producer}}},
                "git": {"commit_hash": candidate},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": json.dumps(
                    {
                        "candidate": candidate,
                        "verdict": "ACCEPT",
                        "unresolved_findings": [],
                        "source_review_assessment": assessment,
                    }
                ),
            },
        },
    ]
    (root / f"rollout-{producer}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in parent)
    )
    (root / f"rollout-{reviewer}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in child)
    )
    source_review = {
        "workdir": str(tmp_path),
        "commands": commands,
        "canonical_acceptance": "Canonical item",
        "preparation_evidence": {"decision_digest": "1" * 64},
        "review_requirements": contract,
        "source_evidence": source_paths,
        "harness_checks": [
            {
                "candidate": candidate,
                "argv": command,
                "returncode": 1 if fault == "harness-check" and index == 0 else 0,
            }
            for index, command in enumerate(commands)
        ],
    }
    if fault:
        with pytest.raises(TransitionBlocked):
            verify_native_review(
                producer,
                reviewer,
                candidate,
                tmp_path,
                source_review=source_review,
            )
    else:
        result = verify_native_review(
            producer,
            reviewer,
            candidate,
            tmp_path,
            source_review=source_review,
        )
        assert result["source_review_assessment"] == assessment
        assert [row["argv"] for row in result["pre_review_checks"]] == commands
