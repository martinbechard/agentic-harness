"""Synthetic native design reviews exercise installed workflow boundaries."""

import importlib.util
import json
import os
from hashlib import sha256
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "proof_helpers", Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "proof_reuse_agent.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
helper, native_review, object_after = _module.helper, _module.native_review, _module.object_after


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dispatch(prompt, cwd, native_home, argv, session_id):
    # Design production has a distinct artifact-only invocation purpose.
    if "Prepare only a preimplementation design artifact" in prompt:
        request = object_after(prompt, "Bound design request: ")
        output = Path(object_after(prompt, "Artifact output contract: ")["path"])
        design = output / "design.md"
        fault = os.environ.get("DESIGN_REVIEW_FAULT")
        attempts = Path(os.environ["HARNESS_SYSTEM_REPO"]).parent / "design-attempts.jsonl"
        previous = attempts.read_text().splitlines() if attempts.exists() else []
        reject = fault == "reject-first" and not previous
        design.write_text(
            "Propose answer.txt with unspecified content.\n"
            if reject
            else "Propose answer.txt containing done with focused content validation.\n"
        )
        artifact = {"path": str(design), "sha256": sha256(design.read_bytes()).hexdigest()}
        with attempts.open("a") as stream:
            stream.write(
                json.dumps({"request": request, "argv": argv, "artifact": artifact}) + "\n"
            )
        reviewer = native_review(
            native_home,
            session_id,
            request["candidate"],
            verdict="REJECT" if reject else "ACCEPT",
            unresolved_findings=["Specify exact content"] if reject else [],
            request_digest="wrong" if fault == "wrong-digest" else digest(request),
            design_digest=artifact["sha256"],
            coordination=helper("graph_agent").coordination_block(prompt),
        )
        result = {
            "item_id": request["item_id"],
            "candidate": request["candidate"],
            "status": "evidence-ready",
            "artifacts": [artifact],
            "design": artifact,
            "reviewer_session": session_id if fault == "self" else reviewer,
        }
        if fault == "missing":
            result.pop("design")
        return result
    graph = helper("graph_agent").handle_graph(prompt, cwd, native_home, session_id)
    if graph is not None:
        return graph
    return helper("preparation_agent").dispatch(prompt, cwd, native_home, argv, session_id)
