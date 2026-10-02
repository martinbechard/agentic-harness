"""Scoped question authority is a linear verified lifecycle, not a mutable approval hint."""

import json
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace

import pytest

from backlog_harness.application import Application
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import Item, TransitionBlocked
from backlog_harness.scope_admission import admitted_question_continuation, scoped_question_content


@pytest.fixture
def cycle(tmp_path):
    session = {"session_id": "owner", "native_session_id": "native", "binding": {}}

    def item(content, state):
        return Item(
            "one", "one.md", sha256(content.encode()).hexdigest(), state, "owner", 100, content
        )

    initial = item("Status: Running\nRequirement: preserve every byte.\n", "Running")
    admission = {"provider_receipt": {"after": asdict(initial)}, "session": session}
    current = [initial]
    stage = lambda _item, name: tmp_path / "stages" / (name + ".json")
    verified = []
    app = SimpleNamespace(
        root=tmp_path,
        provider=SimpleNamespace(item=lambda _: current[0]),
        _stage_path=stage,
        validate_invocation_result=lambda value: verified.append(value),
        result_json=lambda result: result["value"],
        authority=Application.authority,
        verify_provider_receipt=lambda record, receipt: verified.append(record),
    )
    question = {"question_id": "q-new", "text": "May I continue?"}
    result = {
        "role": "orchestrator",
        "invocation_id": "producer",
        "session": session,
        "outcome": "returned",
        "value": {"item_id": "one", "question": question},
    }
    atomic_json(stage("one", "scope-continuation-" + digest(admission)), result)
    paths = []

    def effect(target, authority):
        before = current[0]
        content = scoped_question_content(before.content, target, authority)
        after = item(content, target)
        record = {
            "item": asdict(before),
            "target": target,
            "authority": authority,
            "stage_operation": "one:provider-" + str(len(paths)),
            "scoped_question_content": content,
        }
        root = tmp_path / "provider-agent-operations" / component(digest(record))
        atomic_json(root / "requested.json", record)
        atomic_json(
            root / "receipt.json",
            {"operation": digest(record), "advancement_verified": True, "after": asdict(after)},
        )
        paths.append(root)
        current[0] = after
        return after

    effect("User Action Required", app.authority(result, "one", question=question))
    question = {
        **question,
        "item_id": "one",
        "item_revision": current[0].revision,
        "owner": "owner",
        "answer": None,
        "disposition": None,
    }

    def answer(disposition="approve"):
        before = current[0]
        response = {
            "text": "Proceed with the retained scope.",
            "digest": digest("Proceed with the retained scope."),
            "question_revision": before.revision,
        }
        operation = {"question": question, "answer": response, "before_revision": before.revision}
        path = stage(
            "one",
            "answer-operation-"
            + digest([question["question_id"], before.revision, response["text"]]),
        )
        atomic_json(path, operation)
        effect(
            "User Action Required",
            {
                "role": "operator",
                "item_id": "one",
                "observed_result": True,
                "invocation_id": "operator:"
                + digest([question["question_id"], response["text"], before.revision]),
                "question": question,
                "answer": response,
            },
        )
        assert admitted_question_continuation(app, "one", admission) is None
        decision = {
            "role": "orchestrator",
            "session": session,
            "invocation_id": "classify",
            "outcome": "returned",
            "value": {
                "question_id": question["question_id"],
                "answer_digest": response["digest"],
                "disposition": disposition,
            },
        }
        atomic_json(
            stage("one", "answer-" + digest([question["question_id"], response["digest"]])),
            decision,
        )
        authority = app.authority(
            decision, "one", question=question, answer=response, disposition=disposition
        )
        after = effect("Running" if disposition == "approve" else "User Action Required", authority)
        assert admitted_question_continuation(app, "one", admission) is None
        operation["result"] = {
            "state": after.state,
            "revision": after.revision,
            "disposition": disposition,
        }
        atomic_json(path, operation)
        atomic_json(
            stage("one", "continuation"),
            {
                "stage": "produce-review-" + digest([question["question_id"], response["digest"]]),
                "approval": authority,
            },
        )
        return path

    return app, admission, current, paths, answer, stage, verified


def test_scoped_cycle_reads_wait_and_approves_exact_latest_answer(cycle):
    app, admission, _current, _paths, answer, stage, verified = cycle
    assert admitted_question_continuation(app, "one", admission) is None
    atomic_json(stage("one", "answer-operation-old"), {"question": {"question_id": "old"}})
    path = answer()
    binding = admitted_question_continuation(app, "one", admission)
    assert binding["binding"]["answer_operation_digest"] == digest(json.loads(path.read_text()))
    assert binding["binding"]["admission_digest"] == digest(admission)
    assert binding["stage"].startswith("scope-answer-continuation-")
    assert admitted_question_continuation(app, "one", admission) == binding
    assert verified


def test_deferred_scoped_answer_does_not_authorize_production(cycle):
    app, admission, _current, _paths, answer, _stage, _verified = cycle
    answer("defer")
    assert admitted_question_continuation(app, "one", admission) is None


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "uncertain",
        "owner",
        "content",
        "question",
        "native",
        "stale-answer",
        "continuation",
    ],
)
def test_scoped_cycle_rejects_broken_authority(cycle, fault):
    app, admission, _current, paths, answer, stage, _verified = cycle
    answer_path = answer()
    if fault in {"missing", "uncertain", "owner", "content"}:
        path = paths[0] / "receipt.json"
        value = json.loads(path.read_text())
        if fault == "missing":
            path.unlink()
        else:
            if fault == "uncertain":
                value["advancement_verified"] = False
            elif fault == "owner":
                value["after"]["owner"] = "other"
            else:
                value["after"]["content"] += "Unauthorized scope\n"
            atomic_json(path, value)
    elif fault in {"question", "native"}:
        path = stage("one", "scope-continuation-" + digest(admission))
        value = json.loads(path.read_text())
        if fault == "question":
            value["value"]["question"]["question_id"] = "stale"
        else:
            value["session"]["native_session_id"] = "other"
        atomic_json(path, value)
    elif fault == "stale-answer":
        value = json.loads(answer_path.read_text())
        value["before_revision"] = "stale"
        atomic_json(answer_path, value)
    else:
        atomic_json(stage("one", "continuation"), {"stage": "old", "approval": {}})
    with pytest.raises(TransitionBlocked):
        admitted_question_continuation(app, "one", admission)


def test_scoped_question_content_never_rewrites_requirements():
    before = "Status: Running\nRequirement: exact original\n"
    after = scoped_question_content(before, "User Action Required", {"question": "q"})
    assert after.startswith(before.replace("Status: Running", "Status: User Action Required"))
    with pytest.raises(TransitionBlocked):
        scoped_question_content(before + "Status: Running\n", "User Action Required", {})


@pytest.mark.parametrize(
    "field,value", [("owner", "other"), ("item_revision", "stale"), ("unexpected", True)]
)
def test_scoped_answer_rejects_noncanonical_enrichment(cycle, field, value):
    app, admission, _current, paths, answer, _stage, _verified = cycle
    answer()
    path = paths[1] / "requested.json"
    record = json.loads(path.read_text())
    record["authority"]["question"][field] = value
    # Rebind the operation identity so rejection tests the question, not its filename hash.
    root = app.root / "provider-agent-operations" / component(digest(record))
    receipt = json.loads((paths[1] / "receipt.json").read_text())
    receipt["operation"] = digest(record)
    atomic_json(root / "requested.json", record)
    atomic_json(root / "receipt.json", receipt)
    path.unlink()
    with pytest.raises(TransitionBlocked, match="stale question"):
        admitted_question_continuation(app, "one", admission)


def test_usage_hold_cannot_be_replayed_as_question_approval(cycle):
    from dataclasses import replace

    app, admission, current, paths, _answer, _stage, _verified = cycle
    old = paths[0]
    record = json.loads((old / "requested.json").read_text())
    record["target"] = "Holding"
    record["authority"] = {
        "role": "coordinator",
        "item_id": "one",
        "invocation_id": "guard",
        "observed_result": True,
        "incident": "usage_unknown",
    }
    current[0] = replace(current[0], state="Holding")
    operation = digest(record)
    new = old.parent / component(operation)
    atomic_json(new / "requested.json", record)
    atomic_json(
        new / "receipt.json",
        {
            "operation": operation,
            "advancement_verified": True,
            "after": asdict(current[0]),
        },
    )
    (old / "requested.json").unlink()
    with pytest.raises(TransitionBlocked, match="not a question/answer transition"):
        admitted_question_continuation(app, "one", admission)
