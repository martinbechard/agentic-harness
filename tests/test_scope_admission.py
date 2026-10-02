import json
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import digest, load_config
from backlog_harness.delivery import preserved_candidate_mode
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import Item, TransitionBlocked, git
from backlog_harness.scope_admission import (
    admission_receipts,
    current_production_result,
    effective_workflow,
    resolve_scope_answer,
    validate_coverage,
    validate_decision_contract,
    validate_request,
    validate_retained_usage,
)
from backlog_harness.workflow import validate_content_amendment_request


def amendment_record(item, amended):
    return {
        "kind": "amend-content",
        "item": asdict(item),
        "target": item.state,
        "target_owner": item.owner,
        "target_original_high": item.original_high,
        "paths": [item.path],
        "expected_path": item.path,
        "admission_input_digest": "a" * 64,
        "amended_content": amended,
        "amended_content_sha256": sha256(amended.encode()).hexdigest(),
    }


def test_content_amendment_is_not_a_same_state_transition():
    content = "Work Item ID: one\nStatus: Running\n"
    item = Item("one", "backlog/one.md", "b" * 64, "Running", "owner", 100, content)
    record = amendment_record(item, content + "\nNew requirement.\n")
    validate_content_amendment_request(item, record)
    for change, message in [
        ({"kind": "transition"}, "Unknown provider operation"),
        ({"target": "Completed"}, "lifecycle authority"),
        ({"target_owner": "other"}, "lifecycle authority"),
        ({"paths": [item.path, "backlog/other.md"]}, "path differs"),
        ({"amended_content": "replacement"}, "bytes are invalid"),
        ({"admission_input_digest": None}, "admission is missing"),
    ]:
        with pytest.raises(TransitionBlocked, match=message):
            validate_content_amendment_request(item, {**record, **change})


def test_scope_admission_chain_is_linear_and_immutable(config_file):
    config, _ = config_file
    app = Application(config)
    root = app._stage_path("one", "unused").parent
    first_input = {"previous_admission_digest": None}
    first = {"input": first_input}
    atomic_json(root / f"scope-admission-receipt-{digest(first_input)}.json", first)
    second_input = {"previous_admission_digest": digest(first)}
    second = {"input": second_input}
    atomic_json(root / f"scope-admission-receipt-{digest(second_input)}.json", second)
    assert admission_receipts(app, "one") == [first, second]
    branch_input = {"previous_admission_digest": digest(first), "branch": True}
    atomic_json(
        root / f"scope-admission-receipt-{digest(branch_input)}.json", {"input": branch_input}
    )
    with pytest.raises(TransitionBlocked, match="branched"):
        admission_receipts(app, "one")


def test_effective_workflow_allows_catalog_growth_and_safe_path_narrowing(config_file):
    config, data = config_file
    old_check = data["workflow"]["checks"][0]
    new_check = ["python", "-m", "yaml"]
    data["workflow"].update(
        allowed_paths=["src/old.py", "src/changed.py"],
        checks=[old_check],
        preparation={
            "allowed_roots": ["src", "tests"],
            "check_commands": [old_check, new_check],
        },
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    frozen = {
        key: value for key, value in data["workflow"].items() if key != "preparation"
    }
    atomic_json(
        app._stage_path("one", "assignment"),
        {"workflow": frozen, "content": "original", "provider_path": "backlog/one.md"},
    )
    request = {
        "item_id": "one",
        "amended_content": "original\nexpanded",
        "review_requirements": None,
        "scope": {"allowed_paths": ["src/changed.py", "tests/new.py"], "checks": [old_check, new_check]},
    }
    current = load_config(config)
    value = effective_workflow(
        app, request, None, current, provider_path="backlog/one.md"
    )
    assert value["allowed_paths"] == ["src/changed.py", "tests/new.py"]
    assert value["checks"] == [old_check, new_check]
    request["scope"]["checks"] = [new_check]
    with pytest.raises(TransitionBlocked, match="drops a required check"):
        effective_workflow(app, request, None, current, provider_path="backlog/one.md")
    request["scope"]["checks"] = [old_check, new_check]
    request["scope"]["allowed_paths"] = ["src/changed.py", "outside/new.py"]
    with pytest.raises(TransitionBlocked, match="configured authority"):
        effective_workflow(app, request, None, current, provider_path="backlog/one.md")
    request["scope"]["allowed_paths"] = ["src/changed.py", "tests/new.py"]
    request["scope"]["checks"].append(["python", "-m", "unconfigured"])
    with pytest.raises(TransitionBlocked, match="command authority"):
        effective_workflow(app, request, None, current, provider_path="backlog/one.md")
    request["scope"]["checks"].pop()
    data["workflow"]["primary_branch"] = "master"
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(TransitionBlocked, match="workflow gates"):
        effective_workflow(
            app,
            request,
            None,
            load_config(config),
            provider_path="backlog/one.md",
        )


def test_effective_workflow_appends_bound_review_requirements_across_admissions(config_file):
    config, data = config_file
    historical_path = "backlog/defect-backlog/one.md"
    provider_path = "backlog/user-action-required/one.md"
    old = {
        "id": "original-review",
        "canonical_reference": "backlog/one.md#acceptance",
        "acceptance_text": "Review the original source acceptance.",
        "required_gate": "Verify the original source acceptance.",
    }
    added = {
        "id": "expanded-review",
        "canonical_reference": "backlog/one.md#expanded",
        "acceptance_text": "Review the expanded catalog acceptance.",
        "required_gate": "Verify the expanded catalog acceptance.",
    }
    original_review = {
        "provider_revision": "original-revision",
        "preparation_digest": "1" * 64,
        "original_preparation_digest": "2" * 64,
        "correction_resolution_digest": "3" * 64,
        "requirements": [old],
    }
    data["workflow"]["review_requirements"] = original_review
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": data["workflow"]["checks"],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    frozen = {
        key: deepcopy(value)
        for key, value in data["workflow"].items()
        if key != "preparation"
    }
    atomic_json(
        app._stage_path("one", "assignment"),
        {"workflow": frozen, "content": "original", "provider_path": historical_path},
    )
    amended = "original\nexpanded review"
    proposed = {
        **deepcopy(original_review),
        "provider_revision": sha256((provider_path + "\0" + amended).encode()).hexdigest(),
        "requirements": [deepcopy(old), deepcopy(added)],
    }
    request = {
        "item_id": "one",
        "scope": {
            "allowed_paths": data["workflow"]["allowed_paths"],
            "checks": data["workflow"]["checks"],
        },
        "amended_content": amended,
        "review_requirements": proposed,
    }
    selected = effective_workflow(
        app,
        request,
        None,
        load_config(config),
        provider_path=provider_path,
    )
    assert selected["review_requirements"]["requirements"][0] == old
    assert selected["review_requirements"] == proposed
    assert frozen["review_requirements"] == original_review

    further = amended + "\nfurther review"
    final = {
        **deepcopy(proposed),
        "provider_revision": sha256((provider_path + "\0" + further).encode()).hexdigest(),
        "requirements": [
            deepcopy(old),
            deepcopy(added),
            {
                "id": "further-review",
                "canonical_reference": "backlog/one.md#further",
                "acceptance_text": "Review the further acceptance.",
                "required_gate": "Verify the further acceptance.",
            },
        ],
    }
    request.update(amended_content=further, review_requirements=final)
    assert effective_workflow(
        app,
        request,
        {"workflow": selected},
        load_config(config),
        provider_path=provider_path,
    )["review_requirements"] == final


def test_scope_admission_replays_the_validated_pre_extension_review_contract(
    config_file, monkeypatch
):
    config, data = config_file
    provider_path = "backlog/one.md"
    original_review = {
        "provider_revision": "original-revision",
        "preparation_digest": "1" * 64,
        "requirements": [
            {
                "id": "original-review",
                "canonical_reference": "backlog/one.md#acceptance",
                "acceptance_text": "Review the original source acceptance.",
                "required_gate": "Verify the original source acceptance.",
            }
        ],
    }
    data["workflow"]["review_requirements"] = original_review
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": data["workflow"]["checks"],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    frozen = {
        key: deepcopy(value)
        for key, value in data["workflow"].items()
        if key != "preparation"
    }
    assignment = {
        "workflow": frozen,
        "content": "original",
        "provider_path": provider_path,
    }
    acceptance = {"session": {"session_id": "owner", "native_session_id": "native"}}
    atomic_json(app._stage_path("one", "assignment"), assignment)
    atomic_json(app._stage_path("one", "accept"), acceptance)
    operator_request = {
        "version": 1,
        "item_id": "one",
        "expected_revision": "a" * 64,
        "previous_admission_digest": None,
        "authority_sources": [],
        "original_preparation_digest": "b" * 64,
        "original_assignment_digest": digest(assignment),
        "acceptance_digest": digest(acceptance),
        "previous_result_digest": "c" * 64,
        "scope_answer": None,
        "candidate": {},
        "scope": {
            "allowed_paths": data["workflow"]["allowed_paths"],
            "checks": data["workflow"]["checks"],
        },
        "amended_content": "original\nexpanded",
    }
    operator_digest = digest(operator_request)
    decision = {
        "invocation_id": "legacy-scope-decision",
        "text": json.dumps(
            {
                "operation": "amend-content",
                "authorized": True,
                "request_digest": operator_digest,
            }
        ),
    }
    approved = {
        **operator_request,
        "remaining_estimate": 10,
        "authority": {
            "operator_request_digest": operator_digest,
            "decision_digest": digest(decision),
            "invocation_id": decision["invocation_id"],
            "scope_answer_digest": None,
        },
    }
    workflow = effective_workflow(
        app,
        approved,
        None,
        load_config(config),
        provider_path=provider_path,
        retained_decision=decision,
    )
    provider_request = {
        "kind": "amend-content",
        "admission_input_digest": digest(approved),
        "stage_operation": "legacy-provider-amendment",
        "item": {
            "path": provider_path,
            "revision": operator_request["expected_revision"],
        },
    }
    operation = digest(provider_request)
    operation_path = app.root / "provider-agent-operations" / component(operation)
    operation_path.mkdir(parents=True)
    atomic_json(operation_path / "requested.json", provider_request)
    receipt = {
        "version": 1,
        "input": approved,
        "decision": decision,
        "provider_receipt": {
            "operation": operation,
            "after": asdict(Item("one", provider_path, "amended-revision", "Running", "owner", 100, approved["amended_content"])),
        },
        "provider_revision": "amended-revision",
        "workflow": workflow,
        "candidate": operator_request["candidate"],
        "session": acceptance["session"],
        "usage": {"generated_tokens": 1},
        "remaining_high": 10,
    }
    atomic_json(
        app._stage_path("one", "scope-admission-receipt-" + digest(approved)),
        receipt,
    )
    monkeypatch.setattr(app, "verify_provider_receipt", lambda *args: None)
    monkeypatch.setattr(app, "validate_invocation_result", lambda *args: None)
    monkeypatch.setattr(
        app.provider,
        "item",
        lambda item_id: Item(**receipt["provider_receipt"]["after"]),
    )
    assert app.scope_admission("one") == receipt
    assert receipt["workflow"]["review_requirements"] == original_review

    changed_protocol = {
        **decision,
        "text": json.dumps(
            {
                "operation": "amend-content",
                "authorized": True,
                "operator_request_digest": operator_digest,
            }
        ),
    }
    with pytest.raises(TransitionBlocked, match="decision contract differs"):
        effective_workflow(
            app,
            approved,
            None,
            load_config(config),
            provider_path=provider_path,
            retained_decision=changed_protocol,
        )


@pytest.mark.parametrize("fault", ["missing", "wrong"])
def test_current_scope_decision_replay_requires_exact_operator_digest(fault):
    operator_request = {
        "version": 1,
        "item_id": "one",
        "expected_revision": "a" * 64,
        "previous_admission_digest": None,
        "authority_sources": [],
        "original_preparation_digest": "b" * 64,
        "original_assignment_digest": "c" * 64,
        "acceptance_digest": "d" * 64,
        "previous_result_digest": "e" * 64,
        "scope_answer": None,
        "candidate": {},
        "scope": {},
        "review_requirements": None,
        "amended_content": "original\nexpanded",
    }
    operator_digest = digest(operator_request)
    result = {"operation": "amend-content", "authorized": True}
    if fault != "missing":
        result["operator_request_digest"] = "f" * 64
    decision = {"invocation_id": "current-scope-decision", "text": json.dumps(result)}
    approved = {
        **operator_request,
        "remaining_estimate": 10,
        "authority": {
            "operator_request_digest": operator_digest,
            "decision_digest": digest(decision),
            "invocation_id": decision["invocation_id"],
            "scope_answer_digest": None,
        },
    }
    app = SimpleNamespace(result_json=lambda value: json.loads(value["text"]))
    with pytest.raises(TransitionBlocked, match="decision contract differs"):
        validate_decision_contract(app, approved, decision)

    accepted = {
        **result,
        "operator_request_digest": operator_digest,
    }
    decision = {**decision, "text": json.dumps(accepted)}
    approved["authority"]["decision_digest"] = digest(decision)
    assert validate_decision_contract(app, approved, decision) == (accepted, True)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("old-only", "must append"),
        ("alter-old", "must append"),
        ("wrong-revision", "differs from amended content"),
        ("lineage", "changed review lineage"),
        ("duplicate-id", "must be unique"),
    ],
)
def test_effective_workflow_rejects_nonadditive_review_selection(config_file, fault, message):
    config, data = config_file
    provider_path = "backlog/one.md"
    original = {
        "id": "original-review",
        "canonical_reference": "backlog/one.md#acceptance",
        "acceptance_text": "Original acceptance.",
        "required_gate": "Original gate.",
    }
    added = {
        "id": "expanded-review",
        "canonical_reference": "backlog/one.md#expanded",
        "acceptance_text": "Expanded acceptance.",
        "required_gate": "Expanded gate.",
    }
    contract = {
        "provider_revision": "original-revision",
        "preparation_digest": "1" * 64,
        "requirements": [original],
    }
    data["workflow"]["review_requirements"] = contract
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": data["workflow"]["checks"],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    frozen = {
        key: deepcopy(value)
        for key, value in data["workflow"].items()
        if key != "preparation"
    }
    atomic_json(
        app._stage_path("one", "assignment"),
        {"workflow": frozen, "content": "original", "provider_path": provider_path},
    )
    amended = "original\nexpanded"
    proposed = {
        **deepcopy(contract),
        "provider_revision": sha256((provider_path + "\0" + amended).encode()).hexdigest(),
        "requirements": [deepcopy(original), deepcopy(added)],
    }
    if fault == "old-only":
        proposed["requirements"] = [deepcopy(original)]
    elif fault == "alter-old":
        proposed["requirements"][0]["acceptance_text"] = "Changed acceptance."
    elif fault == "wrong-revision":
        proposed["provider_revision"] = "wrong"
    elif fault == "lineage":
        proposed["preparation_digest"] = "2" * 64
    else:
        proposed["requirements"][1]["id"] = original["id"]
    request = {
        "item_id": "one",
        "scope": {
            "allowed_paths": data["workflow"]["allowed_paths"],
            "checks": data["workflow"]["checks"],
        },
        "amended_content": amended,
        "review_requirements": proposed,
    }
    with pytest.raises(TransitionBlocked, match=message):
        effective_workflow(
            app,
            request,
            None,
            load_config(config),
            provider_path=provider_path,
        )


def test_requirement_coverage_binds_source_acceptance_without_admin_gates():
    check = ["python", "-m", "unittest"]
    proof = {"required": True, "artifact": "report.json"}
    workflow = {
        "allowed_paths": ["src/a.py"],
        "checks": [check],
        "completion": "main-branch",
        "proof_requirements": proof,
    }
    added = [{"id": "expanded-review"}]
    gate = digest(proof)
    result = {
        "coverage_complete": True,
        "unsupported_new_requirements": [],
        "requirements_coverage": [
            {"excerpt": "run the suite", "kind": "check", "value": check},
            {"excerpt": "retain source proof", "kind": "gate", "value": gate},
            {
                "excerpt": "review the expanded catalog",
                "kind": "review_requirement",
                "value": "expanded-review",
            },
        ],
    }
    appended = "run the suite, retain source proof, and review the expanded catalog."
    validate_coverage(result, appended, workflow, added)
    with pytest.raises(TransitionBlocked, match="excerpt is absent"):
        validate_coverage(result, "Different text", workflow, added)
    result["requirements_coverage"][1]["value"] = digest("main-branch")
    with pytest.raises(TransitionBlocked, match="gate differs"):
        validate_coverage(result, appended, workflow, added)
    unsupported = {
        **result,
        "unsupported_new_requirements": ["browser proof has no selected source-proof route"],
    }
    with pytest.raises(TransitionBlocked, match="did not cover"):
        validate_coverage(unsupported, appended, workflow, added)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "unknown"},
        {"may_generate": False},
        {"generated_tokens": None},
        {"ceiling": None},
    ],
)
def test_scope_amendment_rejects_unknown_or_exhausted_usage(change):
    usage = {
        "status": "below",
        "may_generate": True,
        "generated_tokens": 10,
        "ceiling": 100,
    }
    with pytest.raises(TransitionBlocked, match="complete usage"):
        validate_retained_usage({**usage, **change})


def test_scope_amendment_rejects_stale_revision_before_reading_authority(tmp_path):
    item = Item("one", "backlog/one.md", "a" * 64, "Running", "owner", 100, "old")
    request = {
        "version": 1,
        "item_id": "one",
        "expected_revision": "b" * 64,
        "previous_admission_digest": None,
        "authority_sources": [],
        "original_preparation_digest": "c" * 64,
        "original_assignment_digest": "d" * 64,
        "acceptance_digest": "e" * 64,
        "previous_result_digest": "f" * 64,
        "scope_answer": None,
        "candidate": {},
        "scope": {},
        "review_requirements": None,
        "amended_content": "old\nnew",
    }
    with pytest.raises(TransitionBlocked, match="revision is stale"):
        validate_request(SimpleNamespace(), item, request, tmp_path / "missing.json", None)


def test_scope_answer_rejects_unrelated_older_answer_and_binds_latest_authority(tmp_path):
    latest = {
        "question_id": "reconcile-verifier-validation-blocker",
        "text": "Should the original scope be explicitly revised?",
    }
    old = {
        "question_id": "restore-configured-claim-helper",
        "text": "Restore the claim helper?",
    }
    app = SimpleNamespace(
        result_json=lambda value: value,
        _stage_path=lambda item_id, stage: tmp_path / item_id / f"{stage}.json",
    )
    old_answer = {
        "question": old,
        "answer": {"text": "Do not restore it."},
        "result": {"disposition": "approve"},
    }
    request = {"scope_answer": None}
    with pytest.raises(TransitionBlocked, match="latest unanswered question"):
        resolve_scope_answer(app, "one", request, {"question": latest}, [old_answer], {})

    exact_answer = {
        "question": latest,
        "answer": {"text": "Revise it.", "digest": digest("Revise it.")},
        "result": {"disposition": "approve"},
    }
    continuation = app._stage_path("one", "continuation")
    continuation.parent.mkdir(parents=True)
    atomic_json(
        continuation,
        {
            "approval": {
                "question": latest,
                "answer": exact_answer["answer"],
                "disposition": "approve",
            }
        },
    )
    assert resolve_scope_answer(
        app, "one", request, {"question": latest}, [old_answer, exact_answer], {}
    )["answer_digest"] == digest("Revise it.")
    deferred = {**exact_answer, "result": {"disposition": "defer"}}
    with pytest.raises(TransitionBlocked, match="latest unanswered question"):
        resolve_scope_answer(app, "one", request, {"question": latest}, [deferred], {})

    authority = tmp_path / "authority.txt"
    answer = "Consolidate the common cause into the original item."
    authority.write_text(latest["question_id"] + "\n" + latest["text"] + "\n" + answer)
    source_digest = sha256(authority.read_bytes()).hexdigest()
    request["scope_answer"] = {
        "question_id": latest["question_id"],
        "question_digest": digest(latest),
        "answer_text": answer,
        "authority_reference": str(authority),
        "authority_digest": source_digest,
    }
    resolved = resolve_scope_answer(
        app,
        "one",
        request,
        {"question": latest},
        [old_answer],
        {str(authority.resolve()): (source_digest, authority.read_text())},
    )
    assert resolved == {
        "question_digest": digest(latest),
        "answer_digest": digest(answer),
        "source": "authority-source:" + source_digest,
    }
    request["scope_answer"]["question_digest"] = digest(old)
    with pytest.raises(TransitionBlocked, match="latest unanswered question"):
        resolve_scope_answer(
            app,
            "one",
            request,
            {"question": latest},
            [old_answer],
            {str(authority.resolve()): (source_digest, authority.read_text())},
        )


def test_scope_amendment_rejects_an_older_result_than_the_current_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr("backlog_harness.scope_admission.admitted_question_continuation", lambda *args: None)
    item_id = "one"
    root = tmp_path / item_id
    root.mkdir()
    acceptance = {"session": {"session_id": "owner", "native_session_id": "native"}}
    previous_admission = {"input": {"previous_admission_digest": None}}
    older = {
        "role": "orchestrator",
        "session": acceptance["session"],
        "evidence_path": str(tmp_path / "older"),
        "text": json.dumps({"item_id": item_id, "request_completion": False}),
    }
    latest = {
        **older,
        "evidence_path": str(tmp_path / "latest"),
        "text": json.dumps(
            {
                "item_id": item_id,
                "question": {"question_id": "latest", "text": "Expand the scope?"},
            }
        ),
    }
    app = SimpleNamespace(
        _stage_path=lambda selected, stage: root / f"{stage}.json",
        process_stopped=lambda path: True,
    )
    atomic_json(root / "historical-result.json", older)
    atomic_json(root / ("scope-continuation-" + digest(previous_admission) + ".json"), latest)
    with pytest.raises(TransitionBlocked, match="stale production result"):
        current_production_result(
            app, item_id, previous_admission, acceptance, digest(older)
        )
    assert (
        current_production_result(
            app, item_id, previous_admission, acceptance, digest(latest)
        )
        == latest
    )


def test_provider_amendment_receipt_preserves_lifecycle_and_exact_bytes(provider):
    ready = provider.item("item-one")
    content = ready.content.replace("Status: Ready", "Status: Running").replace(
        "Owner: Unowned", "Owner: owner"
    )
    path = provider.repository / ready.path
    path.write_text(content)
    git(provider.repository, "add", "--", ready.path)
    git(provider.repository, "commit", "-m", "Start item")
    before = Item(
        ready.item_id,
        ready.path,
        sha256(ready.path.encode() + b"\0" + content.encode()).hexdigest(),
        "Running",
        "owner",
        ready.original_high,
        content,
    )
    head = git(provider.repository, "rev-parse", "HEAD")
    amended = content + "\n## Added scope\n\nRun the full suite.\n"
    path.write_text(amended)
    git(provider.repository, "add", "--", before.path)
    git(provider.repository, "commit", "-m", "Amend item")
    commit = git(provider.repository, "rev-parse", "HEAD")
    record = {
        **amendment_record(before, amended),
        "head": head,
        "stage_operation": "one:provider-amend",
    }
    app = object.__new__(Application)
    app.config = SimpleNamespace(repository=provider.repository)
    result = {
        "operation_id": record["stage_operation"],
        "before_revision": before.revision,
        "commit": commit,
        "after": {
            "item_id": before.item_id,
            "path": before.path,
            "state": before.state,
            "owner": before.owner,
            "original_high": before.original_high,
        },
    }
    receipt = app.verify_provider_receipt(record, result)
    assert receipt["after"]["content"] == amended
    for change, message in [
        ({"after": {**result["after"], "owner": "other"}}, "lifecycle authority"),
        ({"before_revision": "stale"}, "operation differs"),
    ]:
        with pytest.raises(TransitionBlocked, match=message):
            app.verify_provider_receipt(record, {**result, **change})


def test_delivery_requires_the_admitted_checkpoint_in_final_lineage(tmp_path):
    repository = tmp_path / "candidate"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    (repository / "value.txt").write_text("base\n")
    git(repository, "add", "--", "value.txt")
    git(repository, "commit", "-m", "Base")
    base = git(repository, "rev-parse", "HEAD")
    (repository / "value.txt").write_text("checkpoint\n")
    git(repository, "commit", "-am", "Checkpoint")
    checkpoint = git(repository, "rev-parse", "HEAD")
    (repository / "value.txt").write_text("descendant\n")
    git(repository, "commit", "-am", "Descendant")
    descendant = git(repository, "rev-parse", "HEAD")
    app = SimpleNamespace(
        recovery_record=lambda item_id: None,
        scope_admission=lambda item_id: {"candidate": {"head": checkpoint}},
    )
    assert preserved_candidate_mode(app, "one", repository, descendant) is True
    git(repository, "checkout", base)
    (repository / "value.txt").write_text("replacement\n")
    git(repository, "commit", "-am", "Replacement")
    replacement = git(repository, "rev-parse", "HEAD")
    with pytest.raises(TransitionBlocked, match="checkpoint is not preserved"):
        preserved_candidate_mode(app, "one", repository, replacement)


def test_amend_scope_commits_once_publishes_admission_and_replays_without_calls(
    config_file, provider, tmp_path, monkeypatch
):
    import asyncio
    import shutil

    from backlog_harness.evidence import component

    config, data = config_file
    item_path = provider.repository / "backlog/feature-backlog/item-one.md"
    running_content = item_path.read_text().replace("Status: Ready", "Status: Running").replace(
        "Owner: Unowned", "Owner: owner-session"
    )
    item_path.write_text(running_content)
    (provider.repository / "src").mkdir()
    (provider.repository / "src/one.py").write_text("VALUE = 1\n")
    git(provider.repository, "add", "--", "backlog/feature-backlog/item-one.md", "src/one.py")
    git(provider.repository, "commit", "-m", "Start retained item")
    base = git(provider.repository, "rev-parse", "HEAD")
    candidate_root = tmp_path / "candidates"
    candidate_root.mkdir()
    candidate = candidate_root / component("item-one")
    shutil.copytree(provider.repository, candidate)
    git(candidate, "config", "user.email", "test@example.invalid")
    git(candidate, "config", "user.name", "Test")
    (candidate / "src/one.py").write_text("VALUE = 2\n")
    git(candidate, "add", "--", "src/one.py")
    git(candidate, "commit", "-m", "Preserved candidate")
    candidate_head = git(candidate, "rev-parse", "HEAD")

    old_check = ["python", "-m", "unittest"]
    new_check = ["python", "-m", "yaml"]
    original_requirement = {
        "id": "original-review",
        "canonical_reference": "backlog/feature-backlog/item-one.md#acceptance",
        "acceptance_text": "Review the original source acceptance.",
        "required_gate": "Verify the original source acceptance.",
    }
    original_review = {
        "provider_revision": "original-revision",
        "preparation_digest": "1" * 64,
        "requirements": [original_requirement],
    }
    data.update(
        repository=str(provider.repository),
        provider_interaction="agent",
        operational_root=str(tmp_path / "ops"),
        candidate_root=str(candidate_root),
        workspace=str(candidate),
    )
    data["workflow"].update(
        allowed_paths=["src/one.py"],
        checks=[old_check],
        review_requirements=original_review,
        preparation={
            "allowed_roots": ["src"],
            "check_commands": [old_check, new_check],
        },
    )
    data["profiles"]["control"].update(
        permissions=["workspace-write"],
        skills=["manage-work-items", "manage-work-items-file"],
    )
    for skill in data["profiles"]["control"]["skills"]:
        path = tmp_path / "methodology" / "skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    running = Item(
        "item-one",
        "backlog/feature-backlog/item-one.md",
        sha256(("backlog/feature-backlog/item-one.md\0" + running_content).encode()).hexdigest(),
        "Running",
        "owner-session",
        100,
        running_content,
    )
    project = provider.repository / "PROJECT.yaml"
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": "PROJECT.yaml",
                "sha256": sha256(project.read_bytes()).hexdigest(),
                "excerpt": "execution_mode: SOLO",
                "supports": ["mode", "admission"],
            }
        ],
    }
    atomic_json(
        app.provider.cache_path,
        {
            "source_revision": app.provider.source_revision(),
            "source_manifest": app.provider.source_manifest(),
            "items": [asdict(running)],
            "non_items": [],
            "dependencies": {running.item_id: []},
            "policy": policy,
        },
    )
    preparation = {"decision": {"prepared": True}}
    assignment = {
        "content": running.content,
        "provider_revision": running.revision,
        "provider_path": "backlog/defect-backlog/item-one.md",
        "original_high": 100,
        "historical_original_high": 100,
        "workflow": {
            key: value for key, value in data["workflow"].items() if key != "preparation"
        },
        "candidate_root": str(candidate_root),
        "workspace": str(candidate),
    }
    acceptance = {
        "role": "orchestrator",
        "invocation_id": "acceptance",
        "session": {
            "session_id": "owner-session",
            "native_session_id": "native-session",
            "binding": asdict(app.config.binding("orchestrator")),
        },
        "text": json.dumps({"item_id": "item-one", "accepted": True}),
    }
    previous = {
        "role": "orchestrator",
        "invocation_id": "blocked-result",
        "session": acceptance["session"],
        "evidence_path": str(tmp_path / "stopped"),
        "text": json.dumps(
            {"item_id": "item-one", "request_completion": False, "blockers": ["scope"]}
        ),
    }
    for stage, value in [
        ("preparation", preparation),
        ("assignment", assignment),
        ("accept", acceptance),
        ("produce-review", previous),
        ("base", {"commit": base}),
    ]:
        atomic_json(app._stage_path("item-one", stage), value)
    authority = tmp_path / "authority.txt"
    authority.write_text("Expand the retained item to cover the complete suite repair.\n")
    amended = (
        running.content
        + "\n## Expanded requirement\n\nRun the expanded check. Review expanded source acceptance.\n"
    )
    expanded_requirement = {
        "id": "expanded-review",
        "canonical_reference": "backlog/feature-backlog/item-one.md#expanded-requirement",
        "acceptance_text": "Review expanded source acceptance.",
        "required_gate": "Verify expanded source acceptance.",
    }
    selected_review = {
        **deepcopy(original_review),
        "provider_revision": sha256((running.path + "\0" + amended).encode()).hexdigest(),
        "requirements": [deepcopy(original_requirement), expanded_requirement],
    }
    request = {
        "version": 1,
        "item_id": "item-one",
        "expected_revision": running.revision,
        "previous_admission_digest": None,
        "authority_sources": [
            {
                "path": str(authority),
                "sha256": sha256(authority.read_bytes()).hexdigest(),
                "reason": "operator scope authority",
            }
        ],
        "original_preparation_digest": digest(preparation),
        "original_assignment_digest": digest(assignment),
        "acceptance_digest": digest(acceptance),
        "previous_result_digest": digest(previous),
        "scope_answer": None,
        "candidate": {
            "repository": str(candidate),
            "base": base,
            "head": candidate_head,
            "tree": git(candidate, "rev-parse", "HEAD^{tree}"),
        },
        "scope": {"allowed_paths": ["src/one.py", "src/two.py"], "checks": [old_check, new_check]},
        "review_requirements": selected_review,
        "amended_content": amended,
    }
    request_path = tmp_path / "scope-request.json"
    request_path.write_text(json.dumps(request))
    calls = []
    retained_results = {}
    wrong_digest_once = True

    async def invoke(item_id, stage, role, prompt, **kwargs):
        nonlocal wrong_digest_once
        if stage in retained_results:
            return retained_results[stage]
        calls.append(stage)
        if stage.startswith("scope-admission-decision-"):
            payload = json.loads(prompt[prompt.rfind("\n{") + 1 :])
            requested = payload["request"]
            excerpt = (
                "Confirm sequential admission."
                if "Confirm sequential admission." in requested["amended_content"][len(running.content) :]
                else "Run the expanded check."
            )
            value = {
                "operation": "amend-content",
                "authorized": True,
                "item_id": item_id,
                "expected_revision": requested["expected_revision"],
                "previous_admission_digest": requested["previous_admission_digest"],
                "candidate": requested["candidate"],
                "scope": requested["scope"],
                "amended_content_sha256": sha256(
                    requested["amended_content"].encode()
                ).hexdigest(),
                "remaining_high": 20,
                "reason": "Current authority covers the complete remaining work.",
                "coverage_complete": True,
                "unsupported_new_requirements": [],
                "requirements_coverage": [
                    {"excerpt": excerpt, "kind": "check", "value": new_check},
                    *[
                        {
                            "excerpt": (
                                "Review sequential source acceptance."
                                if row["id"] == "sequential-review"
                                else "Review expanded source acceptance."
                            ),
                            "kind": "review_requirement",
                            "value": row["id"],
                        }
                        for row in payload["eligible_coverage_bindings"][
                            "review_requirements"
                        ]
                    ],
                ],
            }
            if wrong_digest_once:
                value["request_digest"] = digest(requested)
                wrong_digest_once = False
            else:
                value["operator_request_digest"] = payload["operator_request_digest"]
            result = {
                "role": "coordinator",
                "invocation_id": stage,
                "session": {"session_id": "coordinator", "native_session_id": "coordinator-native"},
                "evidence_path": str(tmp_path / stage),
                "text": json.dumps(value),
            }
            if "operator_request_digest" in value:
                retained_results[stage] = result
            return result
        operation = kwargs["provider_operation"]
        record = json.loads(
            (
                app.root
                / "provider-agent-operations"
                / component(operation)
                / "requested.json"
            ).read_text()
        )
        item_path.write_text(record["amended_content"])
        git(provider.repository, "add", "--", record["item"]["path"])
        git(provider.repository, "commit", "-m", "Amend provider item")
        result = {
            "role": "coordinator",
            "invocation_id": stage,
            "session": {"session_id": "provider", "native_session_id": "provider-native"},
            "evidence_path": str(tmp_path / stage),
            "text": json.dumps(
                {
                    "operation_id": record["stage_operation"],
                    "before_revision": record["item"]["revision"],
                    "commit": git(provider.repository, "rev-parse", "HEAD"),
                    "after": {
                        "item_id": running.item_id,
                        "path": running.path,
                        "state": running.state,
                        "owner": running.owner,
                        "original_high": running.original_high,
                    },
                }
            ),
        }
        retained_results[stage] = result
        return result

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "validate_invocation_result", lambda result: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *args: None)
    monkeypatch.setattr(app, "validate_management_readiness", lambda role: None)
    monkeypatch.setattr(app, "process_stopped", lambda path: True)
    monkeypatch.setattr(
        app,
        "usage_view",
        lambda *args, **kwargs: {
            "status": "below",
            "may_generate": True,
            "generated_tokens": 10,
            "ceiling": 100,
            "original_high": 100,
        },
    )
    monkeypatch.setattr(
        "backlog_harness.estimation.validate_preparation_invocation", lambda *args: None
    )
    originals = {
        stage: app._stage_path("item-one", stage).read_bytes()
        for stage in ("preparation", "assignment", "accept", "produce-review")
    }
    old_request = {key: value for key, value in request.items() if key != "review_requirements"}
    rejected_path = app._stage_path(
        "item-one", "scope-admission-decision-" + digest(old_request)
    )
    atomic_json(rejected_path, {"authorized": False, "reason": "old contract rejected"})
    rejected_bytes = rejected_path.read_bytes()
    with pytest.raises(TransitionBlocked, match="exact scope amendment"):
        asyncio.run(app.amend_scope("item-one", request_path))
    assert app.provider.item("item-one").content == running.content
    assert rejected_path.read_bytes() == rejected_bytes
    assert calls == [next(stage for stage in calls if stage.startswith("scope-admission"))]
    real_atomic_json = atomic_json
    publication_failed = False

    def fail_first_publication(path, value, **kwargs):
        nonlocal publication_failed
        if not publication_failed and Path(path).name.startswith("scope-admission-receipt-"):
            publication_failed = True
            raise OSError("simulated final admission publication failure")
        return real_atomic_json(path, value, **kwargs)

    monkeypatch.setattr("backlog_harness.application.atomic_json", fail_first_publication)
    with pytest.raises(OSError, match="publication failure"):
        asyncio.run(app.amend_scope("item-one", request_path))
    assert app.provider.item("item-one").content == amended
    assert len(calls) == 3
    assert not list(
        app._stage_path("item-one", "unused").parent.glob("scope-admission-receipt-*.json")
    )
    monkeypatch.setattr("backlog_harness.application.atomic_json", real_atomic_json)
    receipt = asyncio.run(app.amend_scope("item-one", request_path))
    assert receipt["provider_revision"] == app.provider.item("item-one").revision
    assert receipt["workflow"]["review_requirements"] == selected_review
    provider_request = json.loads(
        (
            app.root
            / "provider-agent-operations"
            / component(receipt["provider_receipt"]["operation"])
            / "requested.json"
        ).read_text()
    )
    assert provider_request["item"]["path"] == running.path
    assert assignment["provider_path"] != provider_request["item"]["path"]
    assert app.item_workflow("item-one")["allowed_paths"] == request["scope"]["allowed_paths"]
    assert git(candidate, "rev-parse", "HEAD") == candidate_head
    assert all(app._stage_path("item-one", stage).read_bytes() == value for stage, value in originals.items())
    assert calls[0] == calls[1]
    assert calls[0].startswith("scope-admission")
    assert calls[2].startswith("provider-amend")
    assert asyncio.run(app.amend_scope("item-one", request_path)) == receipt
    assert len(calls) == 3

    retained_continuation = {
        **previous,
        "invocation_id": "scope-continuation",
        "text": json.dumps(
            {
                "item_id": "item-one",
                "request_completion": False,
                "blockers": ["additional authorized scope is required"],
            }
        ),
    }
    atomic_json(
        app._stage_path("item-one", "scope-continuation-" + digest(receipt)),
        retained_continuation,
    )
    second_request = {
        **request,
        "expected_revision": receipt["provider_revision"],
        "previous_admission_digest": digest(receipt),
        "previous_result_digest": digest(retained_continuation),
        "amended_content": amended
        + "\n## Further requirement\n\nConfirm sequential admission. "
        "Review sequential source acceptance.\n",
    }
    sequential_requirement = {
        "id": "sequential-review",
        "canonical_reference": "backlog/feature-backlog/item-one.md#further-requirement",
        "acceptance_text": "Review sequential source acceptance.",
        "required_gate": "Verify sequential source acceptance.",
    }
    second_request["review_requirements"] = {
        **deepcopy(selected_review),
        "provider_revision": sha256(
            (running.path + "\0" + second_request["amended_content"]).encode()
        ).hexdigest(),
        "requirements": [
            deepcopy(original_requirement),
            deepcopy(expanded_requirement),
            sequential_requirement,
        ],
    }
    second_path = tmp_path / "scope-request-2.json"
    second_path.write_text(json.dumps(second_request))
    second_receipt = asyncio.run(app.amend_scope("item-one", second_path))
    assert second_receipt["input"]["previous_admission_digest"] == digest(receipt)
    assert second_receipt["workflow"]["review_requirements"] == second_request[
        "review_requirements"
    ]
    assert app.provider.item("item-one").content == second_request["amended_content"]
    assert len(calls) == 5
    assert asyncio.run(app.amend_scope("item-one", second_path)) == second_receipt
    assert len(calls) == 5
    receipt = second_receipt
    amended = second_request["amended_content"]

    class InspectedContinuation(Exception):
        pass

    async def inspect_continuation(item_id, stage, role, prompt, **kwargs):
        assert stage == "scope-continuation-" + digest(receipt)
        assert role == "orchestrator"
        assert kwargs["session"].session_id == "owner-session"
        assert kwargs["session"].native_session_id == "native-session"
        assert amended in prompt
        assert json.dumps(request["scope"]["allowed_paths"]) in prompt
        raise InspectedContinuation

    async def no_guard(item_id):
        return None

    historical_work = {
        "item_id": "item-one",
        "revision": running.revision,
        "owner": "owner-session",
        "candidate": candidate_head,
        "previous_invocation_id": previous["invocation_id"],
        "previous_result_digest": digest(previous),
        "instruction": "Historical work continuation before the amendment.",
    }
    historical_proof = {
        "item_id": "item-one",
        "revision": running.revision,
        "owner": "owner-session",
        "candidate": candidate_head,
        "proof_result_digest": "1" * 64,
        "proof_request_digest": "2" * 64,
        "instruction": "Historical proof continuation before the amendment.",
    }
    retained_continuations = {}
    for name, value in [
        ("work-continuation", historical_work),
        ("work-continuation-registration", historical_work),
        ("proof-continuation", historical_proof),
        ("proof-continuation-registration", historical_proof),
    ]:
        path = app._stage_path("item-one", name)
        atomic_json(path, value)
        retained_continuations[path] = path.read_bytes()
    monkeypatch.setattr(app, "invoke", inspect_continuation)
    monkeypatch.setattr(app, "enforce_guard", no_guard)
    with pytest.raises(InspectedContinuation):
        asyncio.run(app._run_item("item-one"))
    assert all(path.read_bytes() == value for path, value in retained_continuations.items())
