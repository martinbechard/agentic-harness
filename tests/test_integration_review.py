import json
from pathlib import Path
from uuid import uuid4

import pytest

from backlog_harness.contracts import digest
from backlog_harness.integration_review import verify_integration_review
from backlog_harness.provider import TransitionBlocked


@pytest.mark.parametrize(
    "fault", [None, "candidate", "parent", "prompt", "error", "evidence", "result", "turn"]
)
def test_fresh_integration_review_contract(tmp_path, fault):
    producer, reviewer = str(uuid4()), str(uuid4())
    record = {"candidate": "a" * 40, "workspace": str(tmp_path / "candidate")}
    value = {
        "candidate": record["candidate"],
        "verdict": "ACCEPT",
        "unresolved_findings": [],
        "evidence": ["Both parent deltas checked"],
    }
    meta = {
        "id": reviewer,
        "source": "exec",
        "cwd": record["workspace"],
        "git": {"commit_hash": record["candidate"]},
    }
    prompt = "Integration review identity: " + digest(record)
    end = {"type": "task_complete", "turn_id": "turn"}
    if fault == "candidate":
        meta["git"]["commit_hash"] = "b" * 40
    if fault == "parent":
        meta["parent_thread_id"] = producer
    if fault == "prompt":
        prompt = "unbound"
    if fault == "error":
        end["error"] = "failed"
    if fault == "evidence":
        value["evidence"] = []
    if fault == "turn":
        end["turn_id"] = "another"
    end["last_agent_message"] = json.dumps(value)
    records = [{"type": "session_meta", "payload": meta}] + [
        {"type": "event_msg", "payload": event}
        for event in [
            {"type": "task_started", "turn_id": "turn"},
            {"type": "user_message", "message": prompt},
            end,
        ]
    ]
    path = tmp_path / "2026/10/01" / (reviewer + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    if fault == "result":
        value = {**value, "evidence": ["Different result"]}
    if fault:
        with pytest.raises(TransitionBlocked):
            verify_integration_review(producer, reviewer, record, value, tmp_path)
    else:
        result = verify_integration_review(producer, reviewer, record, value, tmp_path)
        assert result["native_verified"] and result["fresh_context"]
        assert result["producer_session"] == producer
        assert result["reviewer_session"] == reviewer


def test_preseeded_check_receipt_cannot_substitute_for_harness_execution(config_file):
    import sys
    from hashlib import sha256

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json

    config, data = config_file
    argv = [sys.executable, "-c", "print('verified')"]
    data["workflow"]["checks"] = [argv]
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    forged = [
        {
            "argv": argv,
            "candidate": "candidate",
            "returncode": 0,
            "output": "",
            "evidence_sha256": sha256(b"").hexdigest(),
        }
    ]
    atomic_json(app._stage_path("item", "integration-checks"), forged)
    with pytest.raises(TransitionBlocked, match="execution intent"):
        app.validate_check_execution("item", "integration-checks", "candidate", forged)
    actual = app.checks(Path(data["workspace"]), "item", "candidate", "integration-checks")
    app.validate_check_execution("item", "integration-checks", "candidate", actual)
    assert actual[0]["output"] == "verified\n"
    intent = app._stage_path("item", "integration-checks-execution")
    preserved = intent.with_suffix(".retained")
    intent.rename(preserved)
    intent.symlink_to(preserved)
    with pytest.raises(TransitionBlocked, match="execution intent"):
        app.validate_check_execution("item", "integration-checks", "candidate", actual)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "candidate",
        "context",
        "scope",
        "omitted",
        "duplicate",
        "evidence",
        "changed",
        "case",
        "fresh",
    ],
)
def test_scoped_proof_applicability_requires_complete_bound_review(fault):
    from backlog_harness.integration_review import validate_proof_applicability

    context = {
        "candidate": "merged",
        "changed_dependencies": ["guidance.md"],
        "cases": ["behavior"],
    }
    decision = {
        "context_digest": digest(context),
        "candidate": "merged",
        "verdict": "ACCEPT",
        "scope_assessment": "Original inputs cover all behavior and expectation obligations",
        "dependency_assessments": [
            {
                "path": "guidance.md",
                "equivalent": True,
                "reason": "Formatting guidance leaves behavior unchanged",
                "evidence": "Both parent guidance blobs and behavior inputs inspected",
            }
        ],
        "case_assessments": [
            {
                "case": "behavior",
                "retained": True,
                "reason": "Behavior and expectations remain unchanged",
                "evidence": "Original accepted case and merged source compared",
            }
        ],
    }
    if fault == "candidate":
        decision["candidate"] = "other"
    elif fault == "context":
        decision["context_digest"] = "other"
    elif fault == "scope":
        decision["scope_assessment"] = ""
    elif fault == "omitted":
        decision["dependency_assessments"] = []
    elif fault == "duplicate":
        decision["dependency_assessments"] *= 2
    elif fault == "evidence":
        decision["dependency_assessments"][0]["evidence"] = ""
    elif fault == "changed":
        decision["dependency_assessments"][0]["equivalent"] = False
    elif fault == "case":
        decision["case_assessments"][0]["retained"] = False
    elif fault == "fresh":
        decision["verdict"] = "FRESH_PROOF_REQUIRED"
    review = {
        "proof_applicability": decision,
        "reviewer_session": "independent",
        "evidence_sha256": "native-evidence",
    }
    if fault == "missing":
        del review["proof_applicability"]
    proof = {
        "disposition": "review-required",
        "context": context,
        "context_digest": digest(context),
    }
    if fault:
        with pytest.raises(TransitionBlocked):
            validate_proof_applicability(proof, review)
    else:
        result = validate_proof_applicability(proof, review)
        assert result["disposition"] == "reviewed-applicability"
        assert result["context"] == context and result["decision"] == decision


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "unbound",
        "conflict",
        "wrong-role",
        "wrong-turn",
        "wrong-context",
        "missing-native-request",
        "empty",
        "blank",
        "typed",
        "result",
    ],
)
def test_native_response_item_prompt_and_supporting_evidence(tmp_path, fault):
    producer, reviewer = str(uuid4()), str(uuid4())
    record = {"candidate": "a" * 40, "workspace": str(tmp_path)}
    prompt = "Integration review identity: " + digest(record)
    if fault == "missing-native-request":
        prompt = '[harness-invocation {"invocation_id":"retained"}]\n' + prompt
    if fault == "unbound":
        prompt = "No identity"
    if fault == "conflict":
        prompt += "\nIntegration review identity: wrong"
    value = {
        "candidate": record["candidate"],
        "verdict": "ACCEPT",
        "unresolved_findings": [],
        "supporting_evidence": ["Both parent deltas inspected"],
    }
    if fault in ("empty", "blank", "typed"):
        value["supporting_evidence"] = {"empty": [], "blank": [" "], "typed": [True]}[fault]
    user = {
        "type": "message",
        "role": "assistant" if fault == "wrong-role" else "user",
        "content": [{"type": "input_text", "text": prompt}],
    }
    if fault == "wrong-turn":
        user["turn_id"] = "other"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": reviewer,
                "source": "exec",
                "cwd": str(tmp_path),
                "git": {"commit_hash": record["candidate"]},
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions\nBe careful."}
                ],
            },
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "other" if fault == "wrong-context" else "turn"},
        },
        {"type": "response_item", "payload": user},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn",
                "last_agent_message": json.dumps(value),
            },
        },
    ]
    path = tmp_path / "2026/10/01" / (reviewer + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    original = path.read_bytes()
    if fault == "result":
        value = {**value, "supporting_evidence": ["Changed"]}
    if fault:
        with pytest.raises(TransitionBlocked):
            verify_integration_review(producer, reviewer, record, value, tmp_path)
    else:
        result = verify_integration_review(producer, reviewer, record, value, tmp_path)
        assert result["supporting_evidence"] == value["supporting_evidence"]
        assert "evidence" not in result
    assert path.read_bytes() == original
