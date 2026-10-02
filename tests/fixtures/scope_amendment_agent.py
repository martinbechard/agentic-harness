"""Installed scope-amendment scenario with a retained partial candidate."""

import importlib.util
import json
import os
from hashlib import sha256
from pathlib import Path

from backlog_harness.provider import git

QUESTION = {
    "question_id": "reconcile-verifier-validation-blocker",
    "text": "Should the original scope be explicitly revised?",
}


def _legacy():
    path = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "legacy_agent.py"
    spec = importlib.util.spec_from_file_location("scope_legacy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(prompt):
    return json.loads(prompt[prompt.rfind("\n{") + 1 :])


def dispatch(prompt, cwd, native_home, argv, session_id):
    repo = Path(os.environ["HARNESS_SYSTEM_REPO"])
    if "Prepare this selected canonical Ready item" in prompt:
        item = json.JSONDecoder().raw_decode(prompt.split("Canonical item: ", 1)[1])[0]
        return {
            "item_id": item["item_id"],
            "provider_revision": item["revision"],
            "workflow": {
                "allowed_paths": ["answer.txt"],
                "checks": [json.loads(os.environ["HARNESS_SCOPE_OLD_CHECK"])],
            },
            "authority_evidence": [
                {
                    "path": "PROJECT.yaml",
                    "reason": "Authoritative project policy",
                    "sha256": sha256((repo / "PROJECT.yaml").read_bytes()).hexdigest(),
                }
            ],
        }
    if "Decide one exact same-execution content amendment" in prompt:
        payload = _payload(prompt)
        request = payload["request"]
        answer = payload["resolved_scope_answer"]
        return {
            "operation": "amend-content",
            "authorized": True,
            "item_id": request["item_id"],
            "expected_revision": request["expected_revision"],
            "operator_request_digest": sha256(
                json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "previous_admission_digest": request["previous_admission_digest"],
            "candidate": request["candidate"],
            "scope": request["scope"],
            "amended_content_sha256": sha256(request["amended_content"].encode()).hexdigest(),
            "remaining_high": 100,
            "reason": "The exact answer and authority cover this retained execution.",
            "coverage_complete": True,
            "unsupported_new_requirements": [],
            "requirements_coverage": [
                {
                    "excerpt": "Run the expanded check.",
                    "kind": "check",
                    "value": request["scope"]["checks"][-1],
                }
            ],
            "question_digest": answer["question_digest"],
            "answer_digest": answer["answer_digest"],
            "question_disposition": "approve",
        }
    if "Perform only this authorized provider content amendment" in prompt:
        record = _payload(prompt)
        path = repo / record["item"]["path"]
        path.write_text(record["amended_content"])
        git(repo, "add", "--", record["item"]["path"])
        git(repo, "commit", "-m", "Fixture scope amendment")
        return {
            "operation_id": record["operation_id"],
            "before_revision": record["item"]["revision"],
            "commit": git(repo, "rev-parse", "HEAD"),
            "after": {
                "item_id": record["item"]["item_id"],
                "path": record["item"]["path"],
                "state": record["item"]["state"],
                "owner": record["item"]["owner"],
                "original_high": record["item"]["original_high"],
            },
        }
    if "Running is now recorded for your exact session" in prompt:
        if "Scope admission:" not in prompt:
            (cwd / "answer.txt").write_text("candidate\n")
            git(cwd, "add", "--", "answer.txt")
            git(cwd, "commit", "-m", "Retained partial candidate")
            return {"item_id": "item-one", "question": QUESTION}
        marker = repo.parent / "scope-continuation.json"
        marker.write_text(
            json.dumps(
                {
                    "cwd": str(cwd.resolve()),
                    "session_id": session_id,
                    "resumed": "resume" in argv,
                },
                sort_keys=True,
            )
        )
        (cwd / "expanded.txt").write_text("expanded\n")
        git(cwd, "add", "--", "expanded.txt")
        git(cwd, "commit", "-m", "Complete expanded scope")
        candidate = git(cwd, "rev-parse", "HEAD")
        helper = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "graph_agent.py"
        spec = importlib.util.spec_from_file_location("scope_review", helper)
        review = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(review)
        reviewer = review.native_review(
            native_home,
            session_id,
            candidate,
            review.source_review_request(prompt),
            None,
            review.coordination_block(prompt),
        )
        return {
            "item_id": "item-one",
            "candidate": candidate,
            "reviewer_session": reviewer,
            "request_completion": True,
        }
    return _legacy().dispatch(prompt, cwd, native_home, argv, session_id)


def transport_usage(prompt):
    return {"output_tokens": 1}
