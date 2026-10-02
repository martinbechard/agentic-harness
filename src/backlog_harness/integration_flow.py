"""Reconciliation service composed from existing checks and scoped native invocation."""

import asyncio
import base64
import json
import threading
from hashlib import sha256
from pathlib import Path

from .contracts import digest
from .evidence import atomic_json, operation_lock
from .integration_reconciliation import (
    MAX_INTEGRATION_ATTEMPTS,
    authorize_candidate,
    finalize_resolution,
    integration_suffix,
    prepare_candidate,
)
from .integration_review import validate_proof_applicability
from .provider import TransitionBlocked, blob, git
from .workflow import require, validate_candidate


def validate_checks(checks, candidate, commands, *, allow_failure=False):
    require(
        isinstance(checks, list)
        and checks
        and all(isinstance(row, dict) for row in checks)
        and [row.get("argv") for row in checks] == [list(argv) for argv in commands],
        "Integration check coverage differs from configured commands",
    )
    for row in checks:
        output = (
            base64.b64decode(row["output_base64"], validate=True)
            if "output_base64" in row
            else row.get("output", "").encode()
        )
        require(
            row.get("candidate") == candidate
            and type(row.get("returncode")) is int
            and row["returncode"] >= 0
            and (allow_failure or row["returncode"] == 0)
            and row.get("evidence_sha256") == sha256(output).hexdigest()
            and row.get("output", "") == output.decode(errors="replace"),
            "Integration check output or candidate evidence differs",
        )


def _read(path):
    require(path.is_file() and not path.is_symlink(), "Integration evidence must be a regular file")
    return json.loads(path.read_text())


async def integration_effect(function, *args, **kwargs):
    """Keep synchronous Git/generator work cancellable without abandoning cleanup."""
    cancellation = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(function, *args, cancellation_event=cancellation, **kwargs)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        cancellation.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(worker, return_exceptions=True)), timeout=15
            )
        except (TimeoutError, asyncio.CancelledError):
            cancelled.add_note(
                "Integration worker cleanup did not finish within the cancellation wait"
            )
        raise


def _paths(app, item_id):
    root = app._stage_path(item_id, "integration-instruction").parent
    return root / "integration-instruction.json", root / "superseding-delivery-authorization.json"


async def reconcile_integration(app, item_id, instruction_path, amendment_path=None):
    """One bounded reconciliation, with retained effects on interruption."""
    instruction = _read(Path(instruction_path))
    saved, authorization_path = _paths(app, item_id)
    with operation_lock(saved.with_suffix(".lock")):
        context = app.validate_integration_instruction(item_id, instruction)
        if saved.exists():
            require(_read(saved) == instruction, "Integration instruction changed")
        else:
            atomic_json(saved, instruction, exclusive=True)
        amendment_stage = app._stage_path(item_id, "integration-amendment-2")
        amendment = (
            _read(Path(amendment_path))
            if amendment_path
            else (_read(amendment_stage) if amendment_stage.exists() else None)
        )
        if amendment_stage.exists():
            require(_read(amendment_stage) == amendment, "Integration amendment changed")
        if amendment is not None:
            prior_record = _read(app._stage_path(item_id, "integration-candidate"))
            prior_checks = _read(app._stage_path(item_id, "integration-checks"))
            apply_amendment(
                app,
                item_id,
                instruction,
                amendment,
                check_feedback(prior_record, prior_checks),
                context,
            )
        if authorization_path.exists():
            require(
                amendment_path is None or amendment_stage.exists(),
                "Completed integration cannot be amended",
            )
            return load_integration(app, item_id, context["original_candidate"])
        previous_feedback = None
        for attempt in range(1, MAX_INTEGRATION_ATTEMPTS + 1):
            attempt_context = dict(context)
            if attempt == 2:
                require(app.item_quiescent(item_id), "Correction requires quiescent execution")
                attempt_context["workspace"] = Path(
                    str(context["workspace"]) + integration_suffix(attempt)
                )
                if amendment is not None:
                    attempt_context = apply_amendment(
                        app, item_id, instruction, amendment, previous_feedback, attempt_context
                    )
                    if not amendment_stage.exists():
                        require(
                            not app._stage_path(item_id, "integration-attempt-2").exists(),
                            "Correction already started without amendment",
                        )
                        atomic_json(amendment_stage, amendment, exclusive=True)
                retained = {
                    "instruction_digest": digest(instruction),
                    "previous_feedback": previous_feedback,
                    "attempt": 2,
                    **({"amendment_digest": digest(amendment)} if amendment is not None else {}),
                }
                second = app._stage_path(item_id, "integration-attempt-2")
                if second.exists():
                    require(_read(second) == retained, "Correction feedback changed")
                else:
                    app.validate_integration_stage(None, item_id)
                    atomic_json(second, retained, exclusive=True)
            try:
                return await _attempt(
                    app,
                    item_id,
                    instruction,
                    saved,
                    authorization_path,
                    attempt_context,
                    attempt,
                    previous_feedback,
                )
            except CorrectionFeedback as failure:
                previous_feedback = failure.feedback
                require(
                    attempt < MAX_INTEGRATION_ATTEMPTS,
                    "Integration correction exhausted two attempts; review cause and re-estimate remaining work: "
                    + json.dumps(previous_feedback, sort_keys=True),
                )


class CorrectionFeedback(TransitionBlocked):
    """Only validated terminal feedback permits the one bounded correction."""

    def __init__(self, feedback):
        self.feedback = feedback
        super().__init__("Integration resolver/review requires correction")


async def _attempt(
    app, item_id, instruction, saved, authorization_path, context, attempt, previous_feedback
):
    suffix = integration_suffix(attempt)
    record = await integration_effect(
        prepare_candidate,
        app.config.repository,
        context["workspace"],
        saved.parent / ("integration-candidate" + suffix),
        primary=context["primary"],
        candidate=context["original_candidate"],
        base=context["base"],
        candidate_repository=context["candidate_repository"],
        allowed_paths=context["allowed_paths"],
        generated_paths=context["generated_paths"],
        generators=context["generators"],
        allow_resolution=True,
        force_resolution=attempt == 2,
    )
    if record.get("status") == "resolution-required":
        record = await resolve_conflict(
            app,
            item_id,
            record,
            context,
            saved.parent / ("integration-candidate" + suffix),
            attempt=attempt,
            previous_feedback=previous_feedback,
        )
    candidate_path = app._stage_path(item_id, "integration-candidate" + suffix)
    if candidate_path.exists():
        require(_read(candidate_path) == record, "Registered integration candidate changed")
    else:
        atomic_json(candidate_path, record, exclusive=True)
    proof = app.verify_integration_proof(item_id, record, instruction)
    checks_path = app._stage_path(item_id, "integration-checks" + suffix)
    if checks_path.exists():
        checks = _read(checks_path)
    else:
        try:
            checks = app.checks(
                Path(record["workspace"]),
                item_id,
                record["candidate"],
                "integration-checks" + suffix,
            )
        except TransitionBlocked as error:
            if str(error) != "Required checks failed" or not checks_path.exists():
                raise
            checks = _read(checks_path)
    app.validate_check_execution(
        item_id, "integration-checks" + suffix, record["candidate"], checks
    )
    validate_checks(
        checks, record["candidate"], app.item_workflow(item_id)["checks"], allow_failure=True
    )
    if any(row["returncode"] != 0 for row in checks):
        require(app.item_quiescent(item_id), "Failed checks require quiescent execution")
        raise CorrectionFeedback(check_feedback(record, checks))
    prompt = json.dumps(
        {
            "task": "Independently review this exact integration candidate and its delta against "
            "both parents. Return candidate, verdict ACCEPT or REJECT, "
            "unresolved_findings (a list), and evidence (a nonempty list of nonempty strings). "
            "Return one JSON object. Do not modify source.",
            "item_id": item_id,
            "candidate_record": record,
            "original_evidence": context["original_evidence"],
            "checks": checks,
            **({"proof_reuse": proof} if proof.get("disposition") == "review-required" else {}),
        },
        sort_keys=True,
    )
    if proof.get("disposition") == "review-required":
        prompt += (
            "\nProof applicability identity: "
            + proof["context_digest"]
            + "\nAssess the original accepted proof package against the exact merged candidate. "
            "Inspect every changed bound dependency and the full integration delta. Establish "
            "whether the dependency scope is adequate for every original proof obligation, "
            "including newly applicable acceptance obligations. Do not infer equivalence from "
            "filenames or rewrite historical evidence. Do not rerun HOST experiments. Return "
            "proof_applicability containing context_digest, candidate, verdict ACCEPT or "
            "FRESH_PROOF_REQUIRED, scope_assessment, dependency_assessments for every changed "
            "dependency ({path,equivalent:boolean,reason,evidence}), and case_assessments for "
            "every case ({case,retained:boolean,reason,evidence}). Reasons and evidence must "
            "be nonempty strings explaining applicability. Changed assumptions, behavior, "
            "expectations or obligations that invalidate a case require fresh proof for that "
            "case: mark it retained:false and verdict FRESH_PROOF_REQUIRED. No source changes."
        )
    prompt = "Integration review identity: " + digest(record) + "\n" + prompt
    result = await app.invoke(
        item_id,
        "integration-review" + suffix,
        "coordinator",
        prompt,
        purpose="implementation",
        read_only=True,
        review_candidate=record,
    )
    app.validate_integration_stage(result, item_id)
    review = app.verify_integration_review(item_id, record, result)
    proof = validate_proof_applicability(proof, review)
    if review.get("verdict") == "REJECT":
        require(app.item_quiescent(item_id), "Rejected integration reviewer remains active")
        raise CorrectionFeedback(
            {
                "kind": "review-rejected",
                "result_digest": digest(result),
                "candidate_record": record,
                "review": review,
            }
        )
    authorization = authorize_candidate(
        record,
        review,
        checks,
        context["authority"],
        verify_review=lambda evidence: (
            evidence == app.verify_integration_review(item_id, record, result)
        ),
    )
    validate_candidate(
        Path(record["workspace"]),
        record["candidate"],
        record["primary"],
        context["allowed_paths"],
        review["producer_session"],
        review,
        checks,
    )
    authorization.update(
        {
            "item_id": item_id,
            "candidate_record": record,
            "instruction_digest": digest(instruction),
            "review_result_digest": digest(result),
            "original_evidence": context["original_evidence"],
            "proof_reuse": proof,
            "prospective_estimate": context["prospective_estimate"],
            "resolution_evidence": resolution_evidence(app, item_id, attempt),
            "attempt": attempt,
            "previous_feedback": previous_feedback,
        }
    )
    atomic_json(authorization_path, authorization, exclusive=True)
    return authorization


def resolution_evidence(app, item_id, attempt=1):
    suffix = integration_suffix(attempt)
    request_path = app._stage_path(item_id, "integration-resolution-request" + suffix)
    if not request_path.exists():
        return None
    request = _read(request_path)
    result = _read(app._stage_path(item_id, "integration-resolve" + suffix))
    app.validate_integration_stage(result, item_id)
    value = app.result_json(result)
    require(
        value.get("item_id") == item_id
        and value.get("resolution") == "resolved"
        and value.get("resolution_digest") == digest(request),
        "Retained resolver result changed",
    )
    return {"request_digest": digest(request), "result_digest": digest(result)}


async def resolve_conflict(
    app, item_id, record, context, evidence_dir, attempt=1, previous_feedback=None
):
    """One fresh scoped writer attempt; uncertain or failed submissions remain retained."""
    suffix = integration_suffix(attempt)
    require(app.item_quiescent(item_id), "Integration resolution requires quiescent execution")
    request = {
        **record,
        "authority": context["authority"],
        "prospective_estimate": context["prospective_estimate"],
    }
    if attempt == 2:
        request.update(attempt=attempt, previous_feedback=previous_feedback)
    saved = app._stage_path(item_id, "integration-resolution-request" + suffix)
    if saved.exists():
        require(_read(saved) == request, "Integration resolution request changed")
    else:
        atomic_json(saved, request, exclusive=True)
    result = await app.invoke(
        item_id,
        "integration-resolve" + suffix,
        "orchestrator",
        "Resolve only the listed semantic conflicts in this isolated integration workspace. "
        "For correction feedback, make the minimal repair addressing the exact failed checks or findings. "
        "Preserve both parent identities and all unrelated paths. Use the configured claim helper "
        "if current policy requires it. This is bounded integration work under the retained task "
        "authorization, not a new item or canonical owner. Do not commit, change HEAD, mutate "
        "provider state, or run generators: the harness will run declared generators afterwards. "
        "Resolve and stage the listed files; return JSON {item_id,resolution:resolved|blocked,"
        "resolution_digest:<digest of this request>,evidence:<specific result>}. "
        "Do not claim successful resolution when blocked.\n"
        + json.dumps({"request": request, "resolution_digest": digest(request)}, sort_keys=True),
        read_only=False,
        purpose="implementation",
        integration_resolution=request,
    )
    app.validate_integration_stage(result, item_id)
    require(app.item_quiescent(item_id), "Integration resolver remains active")
    value = app.result_json(result)
    require(
        value.get("item_id") == item_id
        and value.get("resolution") in {"resolved", "blocked"}
        and value.get("resolution_digest") == digest(request)
        and isinstance(value.get("evidence"), str)
        and value["evidence"].strip(),
        "Integration resolver did not return a bound successful result; retained attempt requires correction",
    )
    if value["resolution"] == "blocked":
        raise CorrectionFeedback(
            {
                "kind": "resolver-blocked",
                "request_digest": digest(request),
                "result_digest": digest(result),
                "evidence": value["evidence"],
            }
        )
    for name, expected in context.get("amendment_inputs", {}).items():
        path = Path(context["workspace"]) / name
        require(
            path.is_file()
            and not path.is_symlink()
            and sha256(path.read_bytes()).hexdigest() == expected
            and sha256(blob(context["workspace"], "", name)).hexdigest() == expected,
            "Resolver changed bound amendment generator input",
        )
    return await integration_effect(
        finalize_resolution,
        app.config.repository,
        context["workspace"],
        evidence_dir,
        record,
        digest(result),
    )


def load_integration(app, item_id, original_candidate):
    """Revalidate the superseding record before selecting it for protected delivery."""
    saved, path = _paths(app, item_id)
    require(saved.exists() and path.exists(), "Integration authorization missing")
    instruction, authorization = _read(saved), _read(path)
    context = app.validate_integration_instruction(item_id, instruction)
    attempt = authorization.get("attempt", 1)
    suffix = integration_suffix(attempt)
    if attempt == 2:
        require(
            _read(app._stage_path(item_id, "integration-attempt-2"))
            == {
                "attempt": 2,
                "instruction_digest": digest(instruction),
                "previous_feedback": authorization.get("previous_feedback"),
                **(
                    {
                        "amendment_digest": digest(
                            _read(app._stage_path(item_id, "integration-amendment-2"))
                        )
                    }
                    if app._stage_path(item_id, "integration-amendment-2").exists()
                    else {}
                ),
            },
            "Integration correction binding changed",
        )
    if attempt == 2:
        validate_previous_feedback(app, item_id, authorization["previous_feedback"])
        amendment_stage = app._stage_path(item_id, "integration-amendment-2")
        if amendment_stage.exists():
            context = apply_amendment(
                app,
                item_id,
                instruction,
                _read(amendment_stage),
                authorization["previous_feedback"],
                context,
            )
    record = authorization["candidate_record"]
    require(
        authorization.get("resolution_evidence") == resolution_evidence(app, item_id, attempt),
        "Integration resolution evidence changed",
    )
    require(
        authorization["item_id"] == item_id
        and authorization["original_candidate"] == original_candidate
        and context["original_candidate"] == original_candidate
        and authorization["instruction_digest"] == digest(instruction)
        and authorization["original_evidence"] == context["original_evidence"]
        and authorization["prospective_estimate"] == context["prospective_estimate"],
        "Superseding authorization or original evidence changed",
    )
    proof = app.verify_integration_proof(item_id, record, instruction)
    frozen = _read(saved.parent / ("integration-candidate" + suffix) / "candidate.json")
    require(
        record == _read(app._stage_path(item_id, ("integration-candidate" + suffix))),
        "Registered integration candidate changed",
    )
    require(
        record == frozen and record["primary"] == context["primary"],
        "Integration candidate record changed",
    )
    result = _read(app._stage_path(item_id, ("integration-review" + suffix)))
    require(digest(result) == authorization["review_result_digest"], "Integration review changed")
    review = app.verify_integration_review(item_id, record, result)
    require(review == authorization["review"], "Integration review evidence changed")
    require(
        authorization["proof_reuse"] == validate_proof_applicability(proof, review),
        "Integration proof identity changed",
    )
    require(authorization["authority"] == context["authority"], "Integration authority changed")
    checks = authorization["checks"]
    app.validate_check_execution(
        item_id, ("integration-checks" + suffix), record["candidate"], checks
    )
    validate_checks(checks, record["candidate"], app.item_workflow(item_id)["checks"])
    require(
        checks == _read(app._stage_path(item_id, ("integration-checks" + suffix))),
        "Integration checks changed",
    )
    validate_candidate(
        Path(record["workspace"]),
        record["candidate"],
        record["primary"],
        context["allowed_paths"],
        review["producer_session"],
        review,
        checks,
    )
    require(
        git(Path(record["workspace"]), "rev-parse", "HEAD^{tree}") == record["tree"],
        "Integration tree changed",
    )
    return authorization


def validate_previous_feedback(app, item_id, feedback):
    """Recheck the original terminal feedback instead of trusting a copied receipt."""
    if feedback.get("kind") == "checks-failed":
        record = _read(app._stage_path(item_id, "integration-candidate"))
        checks = _read(app._stage_path(item_id, "integration-checks"))
        app.validate_check_execution(item_id, "integration-checks", record["candidate"], checks)
        validate_checks(
            checks, record["candidate"], app.item_workflow(item_id)["checks"], allow_failure=True
        )
        require(
            any(row["returncode"] != 0 for row in checks)
            and feedback == check_feedback(record, checks),
            "Original check feedback changed",
        )
    elif feedback.get("kind") == "resolver-blocked":
        request = _read(app._stage_path(item_id, "integration-resolution-request"))
        result = _read(app._stage_path(item_id, "integration-resolve"))
        app.validate_integration_stage(result, item_id)
        value = app.result_json(result)
        require(
            value.get("item_id") == item_id
            and value.get("resolution") == "blocked"
            and value.get("resolution_digest") == digest(request)
            and feedback
            == {
                "kind": "resolver-blocked",
                "request_digest": digest(request),
                "result_digest": digest(result),
                "evidence": value.get("evidence"),
            },
            "Original resolver feedback changed",
        )
    elif feedback.get("kind") == "review-rejected":
        record = _read(app._stage_path(item_id, "integration-candidate"))
        result = _read(app._stage_path(item_id, "integration-review"))
        app.validate_integration_stage(result, item_id)
        review = app.verify_integration_review(item_id, record, result)
        require(
            review.get("verdict") == "REJECT"
            and feedback
            == {
                "kind": "review-rejected",
                "result_digest": digest(result),
                "candidate_record": record,
                "review": review,
            },
            "Original rejection changed",
        )
    else:
        require(False, "Unknown integration correction feedback")


def check_feedback(record, checks):
    return {
        "kind": "checks-failed",
        "candidate_record": record,
        "checks": checks,
        "checks_digest": digest(checks),
    }


def apply_amendment(app, item_id, instruction, amendment, feedback, context):
    """Add only existing, bound generators for the sole remaining correction attempt."""
    fields = {
        "item_id",
        "instruction_digest",
        "failed_attempt",
        "failed_candidate",
        "primary",
        "next_attempt",
        "purpose",
        "required_check",
        "generators",
        "generated_paths",
        "generator_inputs",
    }
    require(
        isinstance(amendment, dict) and set(amendment) == fields,
        "Integration amendment fields differ",
    )
    require(
        feedback is not None
        and feedback.get("kind") == "checks-failed"
        and amendment["item_id"] == item_id
        and amendment["instruction_digest"] == digest(instruction)
        and type(amendment["failed_attempt"]) is int
        and amendment["failed_attempt"] == 1
        and type(amendment["next_attempt"]) is int
        and amendment["next_attempt"] == 2
        and amendment["failed_candidate"] == feedback["candidate_record"]["candidate"]
        and amendment["primary"] == context["primary"]
        and amendment["purpose"] == "reproduce-failed-required-check"
        and any(
            row["argv"] == amendment["required_check"] and row["returncode"] != 0
            for row in feedback["checks"]
        ),
        "Integration amendment is not bound to the failed required check",
    )
    validate_previous_feedback(app, item_id, feedback)
    require(
        isinstance(amendment["generators"], list)
        and amendment["generators"]
        and isinstance(amendment["generated_paths"], list)
        and amendment["generated_paths"]
        and isinstance(amendment["generator_inputs"], dict),
        "Amendment generator declarations missing",
    )
    # Support the existing Python bytecode flag; a later data argument is not a script.
    check_arguments = amendment["required_check"][1:]
    while check_arguments[:1] == ["-B"]:
        check_arguments = check_arguments[1:]
    require(
        bool(check_arguments)
        and all(
            isinstance(argv, list)
            and len(argv) >= 2
            and argv[0] == amendment["required_check"][0]
            and argv[1] == check_arguments[0]
            for argv in amendment["generators"]
        ),
        "Amendment generator does not reproduce the failed required check",
    )
    merged = dict(instruction)
    for key in ("generators", "generated_paths"):
        merged[key] = list(instruction.get(key, []))
        for value in amendment[key]:
            if value not in merged[key]:
                merged[key].append(value)
    merged["generator_inputs"] = dict(instruction.get("generator_inputs", {}))
    for name, expected in amendment["generator_inputs"].items():
        require(
            name not in merged["generator_inputs"] or merged["generator_inputs"][name] == expected,
            "Amendment changes original generator input",
        )
        merged["generator_inputs"][name] = expected
    validated = app.validate_integration_instruction(item_id, merged)
    return {
        **context,
        "generators": validated["generators"],
        "generated_paths": validated["generated_paths"],
        "amendment_inputs": merged["generator_inputs"],
    }
