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


def effective_workflow(app, request, previous, current, *, retained_decision=None):
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
    provider_path = json.loads(
        app._stage_path(request["item_id"], "assignment").read_text()
    )["provider_path"]
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
        stage = "scope-continuation-" + digest(previous_admission)
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
    workflow = effective_workflow(app, request, previous, current)
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
