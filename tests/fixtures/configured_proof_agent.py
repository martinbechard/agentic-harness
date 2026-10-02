"""Simulated native proof collection/review; production validators remain unchanged."""

import importlib.util
import os
from hashlib import sha256
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "proof_helpers", Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "proof_reuse_agent.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
helper, native_review, object_after = _module.helper, _module.native_review, _module.object_after


def dispatch(prompt, cwd, native_home, argv, session_id):
    fault = os.environ.get("CONFIGURED_PROOF_FAULT")
    if "Collect only the explicitly required browser/print evidence" in prompt:
        request = object_after(prompt, "Bound request: ")
        output = Path(object_after(prompt, "Artifact output contract: ")["path"])
        capture = output / "fixture-capture.txt"
        capture.write_text("Synthetic capture: all columns readable within Letter margins")
        artifact = {"path": str(capture), "sha256": sha256(capture.read_bytes()).hexdigest()}
        result = {
            "item_id": request["item_id"],
            "candidate": request["candidate"],
            "status": "evidence-ready",
            "artifacts": [artifact],
            "blockers": [],
            "requirement_results": [
                {"id": row["id"], "outcome": "PASS", "artifacts": [artifact]}
                for row in request["proof_requirements"]["requirements"]
            ],
        }
        if fault == "missing":
            result.pop("requirement_results")
        if fault == "invalid":
            artifact["sha256"] = "0" * 64
        return result
    if "Arrange one fresh native read-only child review" in prompt:
        request = object_after(prompt, "Bound request: ")
        proof = object_after(prompt, "Proof result: ")
        extra = (
            {}
            if fault == "generic"
            else {
                "proof_result_digest": prompt.split("Proof result digest: ", 1)[1].splitlines()[0],
                "requirements_digest": request["requirements_digest"],
                "requirement_assessments": [
                    {
                        "id": row["id"],
                        "verdict": "ACCEPT",
                        "assessment": "Inspected synthetic capture: readable columns within margins",
                        "artifacts": row["artifacts"],
                    }
                    for row in proof["requirement_results"]
                ],
            }
        )
        reviewer = native_review(native_home, session_id, request["candidate"], **extra)
        return {
            "item_id": request["item_id"],
            "candidate": request["candidate"],
            "proof_reviewer_session": reviewer,
        }
    return helper("preparation_agent").dispatch(prompt, cwd, native_home, argv, session_id)
