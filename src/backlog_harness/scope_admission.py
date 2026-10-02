"""Deterministic validation for same-execution provider content amendments."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path, PurePosixPath

from .contracts import digest, plain
from .provider import Item, TransitionBlocked, git
from .workflow import require

_DIGEST = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_REQUEST_KEYS = {
    "version",
    "item_id",
    "expected_revision",
    "previous_admission_digest",
    "authority_sources",
    "original_preparation_digest",
    "original_assignment_digest",
    "acceptance_digest",
    "previous_result_digest",
    "scope_answer",
    "candidate",
    "scope",
    "review_requirements",
    "amended_content",
}


def _exact(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), f"Invalid {label} fields")


def _sha(value, label):
    require(isinstance(value, str) and _DIGEST.fullmatch(value), f"Invalid {label} digest")
    return value


def _commit(value, label):
    require(isinstance(value, str) and _COMMIT.fullmatch(value), f"Invalid {label} commit")
    return value


def _relative(value, label):
    require(isinstance(value, str) and value, f"Invalid {label}")
    path = PurePosixPath(value)
    require(
        value == path.as_posix()
        and not path.is_absolute()
        and ".." not in path.parts
        and ".git" not in path.parts,
        f"Invalid {label}",
    )
    return value


def admission_receipts(app, item_id):
    """Return a unique immutable admission chain; branches and stale links are invalid."""
    values = []
    for path in sorted(
        app._stage_path(item_id, "unused").parent.glob("scope-admission-receipt-*.json")
    ):
        require(path.is_file() and not path.is_symlink(), "Scope admission evidence is unsafe")
        value = json.loads(path.read_text())
        require(
            path.name == f"scope-admission-receipt-{digest(value['input'])}.json",
            "Scope admission identity differs",
        )
        values.append(value)
    if not values:
        return []
    by_previous = {}
    for value in values:
        previous = value["input"].get("previous_admission_digest")
        by_previous.setdefault(previous, []).append(value)
    require(len(by_previous.get(None, [])) == 1, "Scope admission root is ambiguous")
    chain, current = [], by_previous[None][0]
    seen = set()
    while True:
        identity = digest(current)
        require(identity not in seen, "Scope admission chain is cyclic")
        seen.add(identity)
        chain.append(current)
        children = by_previous.get(identity, [])
        require(len(children) <= 1, "Scope admission chain is branched")
        if not children:
            break
        current = children[0]
    require(len(chain) == len(values), "Scope admission chain is disconnected")
    return chain


def _validate_authority_sources(request, input_path):
    sources = request["authority_sources"]
    require(isinstance(sources, list) and sources, "Scope amendment authority is missing")
    retained = {}
    for row in sources:
        _exact(row, {"path", "sha256", "reason"}, "scope amendment authority")
        require(
            isinstance(row["path"], str)
            and Path(row["path"]).is_absolute()
            and isinstance(row["reason"], str)
            and row["reason"].strip(),
            "Scope amendment authority reference is invalid",
        )
        path = Path(row["path"])
        require(path.is_file() and not path.is_symlink(), "Scope amendment authority is unavailable")
        require(sha256(path.read_bytes()).hexdigest() == _sha(row["sha256"], "authority"), "Scope amendment authority changed")
        retained[str(path.resolve())] = (row["sha256"], path.read_text())
    require(input_path.is_file() and not input_path.is_symlink(), "Scope amendment input is unsafe")
    return retained


def _validate_candidate(candidate, repository, base, allowed_paths):
    _exact(candidate, {"repository", "base", "head", "tree"}, "candidate")
    require(Path(candidate["repository"]).resolve() == repository.resolve(), "Candidate repository differs")
    bound_base = _commit(candidate["base"], "candidate base")
    head = _commit(candidate["head"], "candidate head")
    tree = _commit(candidate["tree"], "candidate tree")
    require(bound_base == base, "Candidate base differs")
    require(git(repository, "rev-parse", "HEAD") == head, "Candidate HEAD changed")
    require(git(repository, "rev-parse", head + "^{tree}") == tree, "Candidate tree changed")
    require(not git(repository, "status", "--porcelain"), "Candidate is dirty")
    try:
        git(repository, "merge-base", "--is-ancestor", bound_base, head)
    except TransitionBlocked as exc:
        raise TransitionBlocked("Candidate base is not an ancestor") from exc
    changed = git(repository, "diff", "--name-only", bound_base, head).splitlines()
    require(set(changed) <= set(allowed_paths), "Amended scope loses preserved candidate changes")
    return changed


def _validate_scope(scope):
    _exact(scope, {"allowed_paths", "checks"}, "scope")
    paths = scope["allowed_paths"]
    require(
        isinstance(paths, list)
        and paths
        and len(paths) == len(set(paths)),
        "Scope amendment needs unique paths",
    )
    paths = [_relative(value, "scope path") for value in paths]
    checks = scope["checks"]
    require(
        isinstance(checks, list)
        and checks
        and all(
            isinstance(argv, list)
            and argv
            and all(isinstance(arg, str) and arg for arg in argv)
            for argv in checks
        ),
        "Scope amendment needs exact checks",
    )
    require(len({json.dumps(argv) for argv in checks}) == len(checks), "Duplicate scope check")
    return {"allowed_paths": paths, "checks": checks}


def _review_extension(prior, proposed, provider_path, amended_content):
    """Permit only an additive review selection for the deterministic amended revision."""
    prior_review = prior.get("review_requirements")
    if prior_review is None:
        require(proposed is None, "Scope amendment invents a source-review route")
        return None, []
    require(isinstance(proposed, dict), "Scope amendment review selection is missing")
    from .contracts import ConfigError, validate_review_requirements

    try:
        validate_review_requirements(proposed)
    except ConfigError as exc:
        raise TransitionBlocked(str(exc)) from exc
    expected_revision = sha256(
        provider_path.encode() + b"\0" + amended_content.encode()
    ).hexdigest()
    require(
        proposed.get("provider_revision") == expected_revision,
        "Scope amendment review revision differs from amended content",
    )
    for key, value in prior_review.items():
        if key not in {"provider_revision", "requirements"}:
            require(proposed.get(key) == value, "Scope amendment changed review lineage")
    require(
        set(proposed) == set(prior_review),
        "Scope amendment changed review selection shape",
    )
    retained = prior_review["requirements"]
    selected = proposed["requirements"]
    require(
        len(selected) > len(retained) and selected[: len(retained)] == retained,
        "Scope amendment must append source-review requirements",
    )
    return plain(proposed), plain(selected[len(retained) :])


def _retained_review_selection(app, request, prior, decision):
    """Recognize the exact decision contract used by retained pre-extension admissions."""
    _, current_shape = validate_decision_contract(app, request, decision)
    require(not current_shape, "Retained scope admission review selection is missing")
    return plain(prior.get("review_requirements"))


def validate_decision_contract(app, request, decision):
    """Bind a retained decision to its exact old or current operator input contract."""
    require(isinstance(decision, dict), "Scope admission decision is missing")
    operator_request = {
        key: value for key, value in request.items() if key not in {"remaining_estimate", "authority"}
    }
    current_shape = set(operator_request) == _REQUEST_KEYS
    legacy_shape = set(operator_request) == _REQUEST_KEYS - {"review_requirements"}
    require(
        current_shape or legacy_shape,
        "Invalid retained scope amendment input fields",
    )
    require(
        set(request) == set(operator_request) | {"remaining_estimate", "authority"},
        "Invalid retained scope admission fields",
    )
    authority = request.get("authority")
    _exact(
        authority,
        {
            "operator_request_digest",
            "decision_digest",
            "invocation_id",
            "scope_answer_digest",
        },
        "retained scope amendment authority",
    )
    operator_digest = digest(operator_request)
    result = app.result_json(decision)
    digest_field = "operator_request_digest" if current_shape else "request_digest"
    require(
        type(request["remaining_estimate"]) is int
        and request["remaining_estimate"] > 0
        and isinstance(decision.get("invocation_id"), str)
        and bool(decision["invocation_id"])
        and (
            authority["scope_answer_digest"] is None
            or (
                isinstance(authority["scope_answer_digest"], str)
                and _DIGEST.fullmatch(authority["scope_answer_digest"])
            )
        )
        and result.get(digest_field) == operator_digest
        and authority["operator_request_digest"] == operator_digest
        and authority["decision_digest"] == digest(decision)
        and authority["invocation_id"] == decision.get("invocation_id"),
        "Retained scope admission decision contract differs",
    )
    return result, current_shape


def effective_workflow(
    app,
    request,
    previous,
    current,
    *,
    provider_path,
    retained_decision=None,
):
    """Apply admitted scope and additive review changes while retaining every other gate."""
    from .estimation import configured_workflow, validate_preparation_scope

    frozen = json.loads(app._stage_path(request["item_id"], "assignment").read_text())["workflow"]
    prior = previous["workflow"] if previous else frozen
    scope = _validate_scope(request["scope"])
    require(all(argv in scope["checks"] for argv in prior["checks"]), "Scope amendment drops a required check")
    selected = configured_workflow(current, request["item_id"])
    configured_controls = {
        key: value
        for key, value in selected.items()
        if key not in {"allowed_paths", "checks", "preparation", "review_requirements"}
    }
    frozen_controls = {
        key: value
        for key, value in frozen.items()
        if key
        not in {"allowed_paths", "checks", "preparation_evidence", "review_requirements"}
    }
    prior_controls = {
        key: value
        for key, value in prior.items()
        if key
        not in {"allowed_paths", "checks", "preparation_evidence", "review_requirements"}
    }
    require(
        configured_controls == frozen_controls == prior_controls,
        "Scope amendment changed workflow gates",
    )
    require(
        selected.get("review_requirements") == frozen.get("review_requirements"),
        "Configured source-review selection changed after assignment",
    )
    validate_preparation_scope(scope, selected, current)
    if "review_requirements" in request:
        review, _ = _review_extension(
            prior,
            request["review_requirements"],
            provider_path,
            request["amended_content"],
        )
    else:
        review = _retained_review_selection(app, request, prior, retained_decision)
    result = plain(prior)
    result.update(scope)
    if review is not None:
        result["review_requirements"] = review
    return result


def resolve_scope_answer(app, item_id, request, previous_value, answers, authority_sources):
    """Bind only an answer to the exact question returned by the retained producer."""
    question = previous_value.get("question")
    scope_answer = request["scope_answer"]
    if not question:
        require(scope_answer is None, "Scope amendment invents question authority")
        return None
    bound = [
        value
        for value in answers
        if value.get("question") == question
        and isinstance(value.get("result"), dict)
        and value["result"].get("disposition") == "approve"
    ]
    require(len(bound) <= 1, "Scope amendment answer is ambiguous")
    if bound:
        require(scope_answer is None, "Scope amendment duplicates the persisted answer")
        saved = bound[0]
        answer_text = saved.get("answer", {}).get("text")
        require(
            isinstance(answer_text, str)
            and answer_text.strip()
            and saved["answer"].get("digest") == digest(answer_text),
            "Scope amendment answer is incomplete",
        )
        continuation = app._stage_path(item_id, "continuation")
        require(continuation.is_file(), "Scope amendment answer continuation is missing")
        approval = json.loads(continuation.read_text()).get("approval", {})
        require(
            approval.get("question") == question
            and approval.get("answer") == saved["answer"]
            and approval.get("disposition") == "approve",
            "Scope amendment answer continuation differs",
        )
        return {
            "question_digest": digest(question),
            "answer_digest": digest(answer_text),
            "source": "answer-operation:" + digest(saved),
        }
    require(
        isinstance(scope_answer, dict)
        and set(scope_answer)
        == {
            "question_id",
            "question_digest",
            "answer_text",
            "authority_reference",
            "authority_digest",
        }
        and scope_answer.get("question_id") == question.get("question_id")
        and scope_answer.get("question_digest") == digest(question)
        and isinstance(scope_answer.get("answer_text"), str)
        and scope_answer["answer_text"].strip()
        and isinstance(scope_answer.get("authority_reference"), str)
        and scope_answer["authority_reference"],
        "Scope amendment cannot bypass the latest unanswered question",
    )
    reference = str(Path(scope_answer["authority_reference"]).resolve())
    require(reference in authority_sources, "Question authority source is invalid")
    source_digest, text = authority_sources[reference]
    require(
        source_digest == _sha(scope_answer["authority_digest"], "question authority")
        and question.get("question_id", "") in text
        and question.get("text", "") in text
        and scope_answer["answer_text"] in text,
        "Question authority does not bind the latest question and answer",
    )
    return {
        "question_digest": digest(question),
        "answer_digest": digest(scope_answer["answer_text"]),
        "source": "authority-source:" + source_digest,
    }


def current_production_result(app, item_id, previous_admission, acceptance, expected_digest):
    """Resolve the one production stage that the legacy execution would currently resume."""
    continuation_path = app._stage_path(item_id, "continuation")
    if previous_admission:
        approved = admitted_question_continuation(app, item_id, previous_admission)
        stage = (
            approved["stage"] if approved else "scope-continuation-" + digest(previous_admission)
        )
    elif continuation_path.exists():
        require(not continuation_path.is_symlink(), "Production continuation evidence is unsafe")
        continuation = json.loads(continuation_path.read_text())
        stage = continuation.get("stage")
        require(isinstance(stage, str) and stage, "Production continuation stage is invalid")
    else:
        stage = "produce-review"
        from .recovery_flow import proof_continuation, work_continuation

        followup = work_continuation(app, item_id, acceptance)
        if followup:
            stage = "continue-work-" + digest(followup)
        proof_followup = proof_continuation(app, item_id, acceptance)
        if proof_followup:
            stage = "continue-proof-" + digest(proof_followup["request"])
    path = app._stage_path(item_id, stage)
    require(path.is_file() and not path.is_symlink(), "Current production result is missing")
    result = json.loads(path.read_text())
    require(digest(result) == expected_digest, "Scope amendment selected a stale production result")
    require(
        result.get("role") == "orchestrator"
        and result.get("session") == acceptance.get("session")
        and isinstance(result.get("evidence_path"), str)
        and app.process_stopped(Path(result["evidence_path"])),
        "Scope amendment requires the stopped canonical execution",
    )
    return result


def validate_request(app, item: Item, supplied, input_path, current):
    """Validate exact historical roots and the current clean candidate before admission."""
    try:
        request = json.loads(json.dumps(supplied, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("Scope amendment input must be JSON-compatible") from exc
    _exact(request, _REQUEST_KEYS, "scope amendment input")
    require(request["version"] == 1, "Unsupported scope amendment version")
    require(request["item_id"] == item.item_id, "Scope amendment names another item")
    require(request["expected_revision"] == item.revision, "Scope amendment revision is stale")
    require(item.state == "Running", "Scope amendment requires Running work")
    amended = request["amended_content"]
    require(
        isinstance(amended, str) and amended.startswith(item.content) and amended != item.content,
        "Scope amendment must append to the exact current provider content",
    )
    authority_sources = _validate_authority_sources(request, Path(input_path).resolve())
    chain = admission_receipts(app, item.item_id)
    previous = chain[-1] if chain else None
    require(
        request["previous_admission_digest"] == (digest(previous) if previous else None),
        "Scope amendment does not extend the current admission",
    )
    for name, key in [
        ("preparation", "original_preparation_digest"),
        ("assignment", "original_assignment_digest"),
        ("accept", "acceptance_digest"),
    ]:
        path = app._stage_path(item.item_id, name)
        require(path.is_file() and not path.is_symlink(), f"Original {name} evidence is missing")
        require(digest(json.loads(path.read_text())) == _sha(request[key], name), f"Original {name} evidence changed")
    acceptance = json.loads(app._stage_path(item.item_id, "accept").read_text())
    require(
        acceptance.get("session", {}).get("session_id") == item.owner,
        "Scope amendment owner differs from acceptance",
    )
    answers = []
    for path in app._stage_path(item.item_id, "unused").parent.glob("*.json"):
        value = json.loads(path.read_text())
        if path.name.startswith("answer-operation-"):
            answers.append(value)
    previous_result = current_production_result(
        app,
        item.item_id,
        previous,
        acceptance,
        _sha(request["previous_result_digest"], "previous result"),
    )
    previous_value = app.result_json(previous_result)
    resolved_answer = resolve_scope_answer(
        app, item.item_id, request, previous_value, answers, authority_sources
    )
    base = json.loads(app._stage_path(item.item_id, "base").read_text())["commit"]
    root = current.data.get("candidate_root")
    require(root, "Scope amendment requires isolated candidate storage")
    from .evidence import component

    candidate_repo = Path(root).resolve() / component(item.item_id)
    require((candidate_repo / ".git").is_dir(), "Scope amendment candidate is absent")
    workflow = effective_workflow(
        app,
        request,
        previous,
        current,
        provider_path=item.path,
    )
    _, added_review_requirements = _review_extension(
        previous["workflow"] if previous else json.loads(
            app._stage_path(item.item_id, "assignment").read_text()
        )["workflow"],
        request["review_requirements"],
        item.path,
        request["amended_content"],
    )
    _validate_candidate(request["candidate"], candidate_repo, base, workflow["allowed_paths"])
    return (
        request,
        workflow,
        previous_result,
        acceptance,
        resolved_answer,
        added_review_requirements,
    )


def coverage_bindings(workflow, added_review_requirements):
    """Expose only candidate-facing evidence routes to the admission decision."""
    return {
        "checks": workflow["checks"],
        "review_requirements": added_review_requirements,
        "retained_gates": [
            {"kind": key, "digest": digest(workflow[key])}
            for key in ("proof_requirements", "design_review")
            if workflow.get(key)
        ],
    }


def validate_coverage(result, appended, workflow, added_review_requirements):
    require(
        result.get("coverage_complete") is True
        and result.get("unsupported_new_requirements") == [],
        "Coordinator did not cover the amended requirements",
    )
    coverage = result.get("requirements_coverage")
    require(isinstance(coverage, list) and coverage, "Amended requirement evidence is missing")
    bindings = coverage_bindings(workflow, added_review_requirements)
    review_ids = {row["id"] for row in added_review_requirements}
    gates = {row["digest"] for row in bindings["retained_gates"]}
    covered_reviews = set()
    for row in coverage:
        _exact(row, {"excerpt", "kind", "value"}, "requirement coverage")
        require(
            isinstance(row["excerpt"], str)
            and row["excerpt"].strip()
            and " ".join(row["excerpt"].split()) in " ".join(appended.split()),
            "Requirement coverage excerpt is absent",
        )
        if row["kind"] == "check":
            require(row["value"] in workflow["checks"], "Requirement coverage check is unauthorized")
        elif row["kind"] == "review_requirement":
            require(row["value"] in review_ids, "Requirement coverage review differs")
            covered_reviews.add(row["value"])
        else:
            require(row["kind"] == "gate" and row["value"] in gates, "Requirement coverage gate differs")
    require(
        covered_reviews == review_ids,
        "Added source-review requirements lack amendment coverage",
    )


def validate_retained_usage(usage):
    """Require complete known accounting before spending on an amendment decision."""
    require(
        isinstance(usage, dict)
        and usage.get("status") == "below"
        and usage.get("may_generate") is True
        and type(usage.get("generated_tokens")) is int
        and type(usage.get("ceiling")) in {int, float},
        "Scope amendment requires complete usage below the retained ceiling",
    )
    return usage


def validate_current_candidate(repository, candidate, allowed_paths):
    return _validate_candidate(candidate, repository, candidate["base"], allowed_paths)


def question_continuation_stage(binding):
    return "scope-answer-continuation-" + digest(binding)


def admitted_question_continuation(app, item_id, admission):
    """Read only the verified question/answer chain descending from this admission."""
    from dataclasses import asdict

    from .evidence import component
    from .workflow import validate_transition

    def read(path):
        require(path.is_file() and not path.is_symlink(), "Scoped question evidence is missing")
        return json.loads(path.read_text())

    cursor = admission["provider_receipt"]["after"]
    current = asdict(app.provider.item(item_id))
    session = admission["session"]
    stage = "scope-continuation-" + digest(admission)
    pending_question = pending_result = answer_operation = None
    binding = approval = None
    operations = []
    for path in (app.root / "provider-agent-operations").glob("*/requested.json"):
        record = read(path)
        if record.get("item", {}).get("item_id") == item_id:
            operations.append((path, record))
    visited = set()
    while cursor != current:
        require(cursor["revision"] not in visited, "Scoped question lifecycle cycle")
        visited.add(cursor["revision"])
        matches = [(path, record) for path, record in operations if record.get("item") == cursor]
        require(
            len(matches) == 1,
            "Scope admission provider revision changed without a unique question effect",
        )
        path, record = matches[0]
        operation_id = digest(record)
        require(
            path.parent.name == component(operation_id) and record.get("kind") is None,
            "Scoped question operation identity differs",
        )
        receipt = read(path.parent / "receipt.json")
        require(
            receipt.get("operation") == operation_id
            and receipt.get("advancement_verified") is True,
            "Scoped question provider advancement is unverified",
        )
        authority = record["authority"]
        before, target = Item(**cursor), record["target"]
        validate_transition(before, target, authority)
        app.verify_provider_receipt(
            record,
            {
                "operation_id": record["stage_operation"],
                "before_revision": cursor["revision"],
                **receipt,
            },
        )
        after = receipt["after"]
        require(
            after["owner"] == session["session_id"] == cursor["owner"]
            and after["original_high"] == cursor["original_high"],
            "Scoped question canonical owner or estimate changed",
        )
        if before.state == "Running" and target == "User Action Required":
            result = read(app._stage_path(item_id, stage))
            app.validate_invocation_result(result)
            value = app.result_json(result)
            require(
                result.get("role") == "orchestrator"
                and result.get("session") == session
                and value.get("item_id") == item_id
                and value.get("question") == authority.get("question")
                and authority == app.authority(result, item_id, question=value["question"]),
                "Scoped question does not bind the admitted producer",
            )
            pending_question = {
                **value["question"],
                "item_id": item_id,
                "item_revision": after["revision"],
                "owner": after["owner"],
                "answer": None,
                "disposition": None,
            }
            pending_result = result
            answer_operation = binding = approval = None
        elif before.state == "User Action Required" and target in {
            "Running",
            "User Action Required",
        }:
            require(
                pending_question is not None and authority.get("question") == pending_question,
                "Scoped answer names a stale question",
            )
            answer = authority.get("answer", {})
            require(
                isinstance(answer.get("text"), str)
                and answer["text"].strip()
                and answer.get("digest") == digest(answer["text"]),
                "Scoped answer is incomplete",
            )
            if authority.get("role") == "operator":
                require(
                    target == "User Action Required"
                    and answer.get("question_revision") == cursor["revision"],
                    "Scoped operator answer revision differs",
                )
                answer_operation = read(
                    app._stage_path(
                        item_id,
                        "answer-operation-"
                        + digest(
                            [pending_question["question_id"], cursor["revision"], answer["text"]]
                        ),
                    )
                )
                require(
                    answer_operation.get("question") == pending_question
                    and answer_operation.get("answer") == answer
                    and answer_operation.get("before_revision") == cursor["revision"]
                    and authority.get("invocation_id")
                    == "operator:"
                    + digest([pending_question["question_id"], answer["text"], cursor["revision"]]),
                    "Scoped persisted answer differs",
                )
            else:
                require(
                    answer_operation is not None and answer_operation["answer"] == answer,
                    "Scoped disposition has no actual operator answer",
                )
                result = read(
                    app._stage_path(
                        item_id,
                        "answer-" + digest([pending_question["question_id"], answer["digest"]]),
                    )
                )
                app.validate_invocation_result(result)
                value = app.result_json(result)
                disposition = value.get("disposition")
                require(
                    result.get("role") == "orchestrator"
                    and result.get("session") == session
                    and value.get("question_id") == pending_question["question_id"]
                    and value.get("answer_digest") == answer["digest"]
                    and disposition in {"approve", "defer", "decline", "ambiguous"}
                    and target
                    == ("Running" if disposition == "approve" else "User Action Required")
                    and authority
                    == app.authority(
                        result,
                        item_id,
                        question=pending_question,
                        answer=answer,
                        disposition=disposition,
                    ),
                    "Scoped answer disposition differs",
                )
                if target == "Running":
                    binding = {
                        "admission_digest": digest(admission),
                        "question_result_digest": digest(pending_result),
                        "answer_operation_digest": digest(answer_operation),
                    }
                    approval = authority
                    stage = question_continuation_stage(binding)
        else:
            require(False, "Scope admission lifecycle is not a question/answer transition")
        require(
            record.get("scoped_question_content")
            == scoped_question_content(cursor["content"], target, authority)
            == after["content"],
            "Scoped question changed admitted content or lifecycle evidence",
        )
        cursor = after
    if binding is None or current["state"] != "Running":
        return None
    if "result" not in answer_operation:
        return None
    require(
        answer_operation.get("result", {}).get("state") == "Running"
        and answer_operation["result"].get("revision") == current["revision"]
        and answer_operation["result"].get("disposition") == "approve",
        "Scoped approved answer is incomplete; use resume-answer",
    )
    continuation = read(app._stage_path(item_id, "continuation"))
    require(
        continuation
        == {
            "stage": "produce-review-"
            + digest([pending_question["question_id"], answer_operation["answer"]["digest"]]),
            "approval": approval,
        },
        "Scoped approved continuation differs",
    )
    return {"binding": binding, "approval": approval, "stage": stage}


def scoped_question_content(before, target, authority):
    """Bind lifecycle additions while preserving every admitted requirement byte."""
    require(
        len(re.findall(r"^Status: .+$", before, re.MULTILINE)) == 1,
        "Scoped question requires one exact Status header",
    )
    expected = re.sub(r"^Status: .+$", "Status: " + target, before, count=1, flags=re.MULTILINE)
    return (
        expected
        + "\n## Harness Transition Evidence\n```json\n"
        + json.dumps({"authority": authority}, sort_keys=True)
        + "\n```\n"
    )


def retained_scope_question_prompt(app, item_id, stage, prompt, result, coordination_context):
    """Reconstruct only the a53 approval suffix for an already completed scoped question."""
    from .evidence import EvidenceStore, component
    from .native_evidence import coordination_instructions, source_review_instructions

    admission = app.scope_admission(item_id)
    require(admission is not None and stage == "scope-continuation-" + digest(admission),
            "Historical scope replay is not the original admitted stage")
    require(admitted_question_continuation(app, item_id, admission) is None,
            "Historical scope replay cannot replace an approved answer")
    require(result.get("outcome") == "returned" and result.get("role") == "orchestrator"
            and result.get("session") == admission["session"],
            "Historical scope question is not a terminal canonical result")
    path = Path(result["evidence_path"])
    expected = (app.root / "runs" / component("item:" + item_id) / "operations"
                / component(item_id + ":" + stage) / "invocations" / component(result["invocation_id"]))
    require(path.absolute() == expected and path.resolve() == expected and app.process_stopped(path),
            "Historical scope question invocation is not retained and stopped")
    retained_files = ("intent.json", "config.json", "execution-context.json", "session.json",
                      "requested.json", "outcomes.jsonl", "events.jsonl", "resume-binding.json",
                      "telemetry.json", "telemetry-report.json")
    require(all((path / name).is_file() and not (path / name).is_symlink()
                for name in retained_files), "Historical scope invocation evidence is incomplete")
    intent = EvidenceStore.reconcile(path)
    from dataclasses import asdict

    from .contracts import AgentBinding, resume_binding_compatible

    try:
        actual_binding = AgentBinding(**result["binding"])
        previous_binding = AgentBinding(**admission["session"]["binding"])
    except (KeyError, TypeError) as error:
        raise TransitionBlocked("Historical scope invocation binding is invalid") from error
    require(result.get("version") == 1 and result.get("purpose") == "implementation"
            and result["binding"] == asdict(actual_binding)
            and actual_binding.role == previous_binding.role == "orchestrator"
            and resume_binding_compatible(previous_binding, actual_binding)
            and intent.get("action") == stage and intent.get("item_id") == item_id
            and intent.get("run_id") == "item:" + item_id
            and intent.get("binding") == result["binding"],
            "Historical scope invocation identity or binding differs")
    _sha(intent.get("config_digest"), "historical scope configuration")
    def retained(name):
        return json.loads((path / name).read_text())

    require(retained("config.json") == {"version": 1, "digest": intent["config_digest"],
                                       "binding": result["binding"]}
            and retained("execution-context.json") == {"purpose": "implementation",
                "provider_operation": None, "read_only": False}
            and retained("session.json") == {"version": 1,
                "session_id": result["session"]["session_id"],
                "native_session_id": result["session"]["native_session_id"],
                "binding": result["binding"]}
            and retained("resume-binding.json") == {
                "session_id": result["session"]["session_id"],
                "native_session_id": result["session"]["native_session_id"],
                "previous_binding": admission["session"]["binding"],
                "current_binding": result["binding"],
                "current_config_digest": intent["config_digest"]},
            "Historical scope retained configuration, context or session differs")
    require(intent.get("outcome") == "returned" and intent.get("partial") is False
            and intent.get("request_digest") == result.get("request_digest")
            and intent.get("invocation_id") == result.get("invocation_id")
            and intent.get("operation_id") == item_id + ":" + stage,
            "Historical scope question terminal evidence differs")
    metadata = retained("telemetry.json")
    require(isinstance(metadata, dict) and set(metadata) == {"path"}
            and isinstance(metadata["path"], str), "Historical scope telemetry metadata differs")
    telemetry_path = Path(metadata["path"])
    require(telemetry_path.is_absolute() and telemetry_path.is_file()
            and not telemetry_path.is_symlink() and telemetry_path.resolve() == telemetry_path
            and telemetry_path.is_relative_to(app.root.resolve()),
            "Historical scope telemetry path is unsafe")
    # The returned/report-present guards above keep this reconstruction read-only.
    recovered = app.recover_invocation(path)
    require(recovered is not None, "Historical scope terminal reconstruction is incomplete")
    # Codex retains the original SessionHandle while session.json records resumed tuning.
    # The exact session record and resume audit were checked above; normalize only that handle.
    recorded_session = retained("session.json")
    require(recovered.get("session") == {key: value for key, value in recorded_session.items() if key != "version"},
            "Historical scope reconstructed session differs")
    recovered = {**recovered, "session": admission["session"]}
    require(result == recovered, "Historical scope result differs from retained terminal output")
    value = app.result_json(result)
    question = value.get("question")
    require(value.get("item_id") == item_id and isinstance(question, dict)
            and all(isinstance(question.get(key), str) and question[key].strip()
                    for key in ("question_id", "text")),
            "Historical scope result is not a pending question")
    continuation_path = app._stage_path(item_id, "continuation")
    require(continuation_path.is_file() and not continuation_path.is_symlink(),
            "Historical scope approval bytes are missing")
    continuation = json.loads(continuation_path.read_text())
    legacy = "\nPersisted canonical approval: " + json.dumps(continuation["approval"])
    proof_path = app._stage_path(item_id, "proof-review")
    if proof_path.exists():
        require(proof_path.is_file() and not proof_path.is_symlink(), "Historical proof review is unsafe")
        legacy += (
            "\nReuse this retained fresh proof review; include its reviewer_task (or reviewer_session if absent) as "
            "proof_reviewer_session in your response. Do not repeat proof or review: " + proof_path.read_text()
        )
    tail = source_review_instructions(admission["workflow"], admission["input"]["amended_content"],
                                      app.candidate_repository(item_id))
    if coordination_context is not None:
        tail += coordination_instructions(coordination_context)
        legacy = legacy.replace("Follow current claim-free crisis authority; do not invoke claims. ", "")
    require(not tail or prompt.endswith(tail), "Historical scope prompt layout differs")
    prefix = prompt[:-len(tail)] if tail else prompt
    reconstructed = prefix + legacy + tail
    require(digest(reconstructed) == result["request_digest"],
            "Stage request changed; reconcile prior evidence")
    return reconstructed
