"""External agent boundary for installed scoped-proof reconciliation exercises."""

import importlib.util
import json
import os
from hashlib import sha256
from pathlib import Path


def helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def object_after(prompt, marker):
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


def native_review(home, producer, candidate, coordination=None, **extra):
    reviewer = helper("graph_agent").native_review(
        home, producer, candidate, coordination=coordination
    )
    path = next(home.glob("sessions/*/*/*/*" + reviewer + ".jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    value = json.loads(rows[-1]["payload"]["last_agent_message"])
    value.update(extra)
    rows[-1]["payload"]["last_agent_message"] = json.dumps(value)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return reviewer


def dispatch(prompt, cwd, native_home, argv, session_id):
    repo = Path(os.environ["HARNESS_SYSTEM_REPO"])
    root = repo.parent
    if "Reconcile this one preserved item" in prompt:
        request = object_after(prompt, "candidate changes or attempt resets.\n")
        supplied = request["evidence"]
        return {
            "operation": "redispatch",
            "item_id": request["item"]["item_id"],
            "provider_revision": request["item"]["revision"],
            "previous_owner": supplied["preserved_execution"]["execution_id"],
            "ownership_ended": True,
            "continuation_authorized": True,
            "reason": "Preserved completed external execution",
            "remaining_high": 1000,
            "scope": json.loads((root / "scope.json").read_text()),
            "dependencies": [],
            "preserved_execution": supplied["preserved_execution"],
        }
    if "Perform only the missing proof for this retained candidate" in prompt:
        request = object_after(prompt, "Bound request: ")
        output = Path(object_after(prompt, "Artifact output contract: ")["path"])
        return write_proof(output, request, cwd, native_home, session_id)
    if "Integration review identity: " in prompt:
        request = json.JSONDecoder().raw_decode(
            prompt.split("Integration review identity: ", 1)[1].split("\n", 1)[1]
        )[0]
        result = {
            "candidate": request["candidate_record"]["candidate"],
            "verdict": "ACCEPT",
            "unresolved_findings": [],
            "evidence": ["Scoped dependencies retain identical bytes and mode"],
        }
        # The production request carries the exact retained proof context for this review.
        (root / "integration-review-request.json").write_text(json.dumps(request))
        context = request.get("proof_reuse", {})
        if context:
            result["proof_applicability"] = {
                "context_digest": context.get("context_digest"),
                "candidate": result["candidate"],
                "verdict": "FRESH_PROOF_REQUIRED"
                if "expectations.txt" in context["context"]["changed_dependencies"]
                else "ACCEPT",
                "scope_assessment": "Reviewed complete original package against the merged candidate",
                "dependency_assessments": [
                    {
                        "path": p,
                        "equivalent": p == "guidance.txt",
                        "reason": "Guidance wording does not change the answer contract"
                        if p == "guidance.txt"
                        else "Required expectation changed",
                        "evidence": "Compared original and merged Git dependency bytes",
                    }
                    for p in context["context"]["changed_dependencies"]
                ],
                "case_assessments": [
                    {
                        "case": c,
                        "retained": "expectations.txt"
                        not in context["context"]["changed_dependencies"],
                        "reason": "Retained semantic coverage assessed",
                        "evidence": "Scoped source comparison",
                    }
                    for c in context["context"]["cases"]
                ],
            }
        return result
    if "Preserved recovery evidence: " in prompt:
        packet = object_after(prompt, "Preserved recovery evidence: ")
        candidate = packet["candidate"]["head"]
        if "Consume this validated auxiliary proof" in prompt:
            handoff = object_after(
                prompt, "This handoff grants no new permissions or delivery authority. "
            )
            reviewer = native_review(
                native_home,
                session_id,
                candidate,
                coordination=helper("graph_agent").coordination_block(prompt),
                proof_result_digest=handoff["request"]["proof_result_digest"],
            )
            return {
                "item_id": "item-one",
                "candidate": candidate,
                "reviewer_session": reviewer,
                "proof_reviewer_session": reviewer,
                "status": "awaiting_exact_candidate_approval",
                "question": {
                    "question_id": "approve-proof",
                    "candidate": candidate,
                    "text": "Approve reviewed candidate " + candidate + "?",
                },
            }
        return {
            "item_id": "item-one",
            "candidate": candidate,
            "request_completion": False,
            "status": "blocked",
            "blockers": ["Need scoped semantic proof"],
        }
    return helper("legacy_agent").dispatch(prompt, cwd, native_home, argv, session_id)


def write_proof(output, request, cwd, home, producer):
    output.mkdir(parents=True, exist_ok=True)
    candidate = request["candidate"]

    def write(name, value):
        path = output / name
        path.write_text(json.dumps(value, sort_keys=True))
        return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}

    binding = {
        "candidate": candidate,
        "scope": "answer-only semantic proof",
        "checks": [
            {"path": p, "candidate_sha256": sha256((cwd / p).read_bytes()).hexdigest()}
            for p in ("answer.txt", "expectations.txt", "guidance.txt")
        ],
    }
    files = [write("candidate-binding.json", binding)]
    files.append(
        write(
            "committed-input-binding.json",
            {
                "candidate": candidate,
                "all_match": True,
                "checks": [
                    {
                        "path": c["path"],
                        "committed_sha256": c["candidate_sha256"],
                        "matches_bound_candidate_bytes": True,
                    }
                    for c in binding["checks"]
                ],
            },
        )
    )
    files.append(
        write("proof-input-inventory.json", {"candidate": candidate, "artifacts": files.copy()})
    )
    reviewer = native_review(home, producer, candidate)
    review = write(
        "independent-result-review.json",
        {
            "candidate": candidate,
            "verdict": "ACCEPT",
            "sha256_by_relative_path": {Path(f["path"]).name: f["sha256"] for f in files},
            "blockers": [],
            "independent_judges": [
                {"case": "answer reports done", "identity": reviewer, "verdict": "ACCEPT"}
            ],
        },
    )
    manifest = write(
        "proof-result.json",
        {
            "item_id": request["item_id"],
            "candidate": candidate,
            "status": "evidence-ready",
            "blockers": [],
            "artifacts": files + [review],
            "independent_judges": [
                {"case": "answer reports done", "identity": reviewer, "verdict": "ACCEPT"}
            ],
            "independent_result_review": {**review, "identity": reviewer, "verdict": "ACCEPT"},
        },
    )
    return {
        "item_id": request["item_id"],
        "candidate": candidate,
        "status": "evidence-ready",
        "artifacts": [manifest, review],
        "blockers": [],
    }
