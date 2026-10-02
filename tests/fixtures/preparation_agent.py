"""Preparation responses with real native receipts and otherwise normal lifecycle."""

import importlib.util
import json
import os
from hashlib import sha256
from pathlib import Path


def legacy_dispatch(*args):
    path = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "legacy_agent.py"
    spec = importlib.util.spec_from_file_location("legacy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.dispatch(*args)


def dispatch(prompt, cwd, native_home, argv, session_id):
    if "Correct one retained preparation response representation" in prompt:
        from backlog_harness.contracts import digest

        original = json.loads(prompt.split("Immutable original response envelope: ", 1)[1])
        value = json.loads(original["text"])
        workflow = value["workflow"]
        gates = workflow.pop("gates")
        supplied = json.loads(os.environ["PREPARATION_CLASSIFICATIONS"])
        classifications = []
        constraints = list(workflow.get("implementation_constraints", []))
        required = list(workflow.get("required_gates", []))
        for index, (gate, row) in enumerate(zip(gates, supplied, strict=True)):
            destination = row["destination"]
            classifications.append(
                {
                    "index": index,
                    "text": gate,
                    "destination": destination,
                    "runtime_obligation": row.get("runtime_obligation"),
                    "no_additional_proof": destination == "implementation_constraints",
                    "rationale": row["rationale"],
                }
            )
            (constraints if destination == "implementation_constraints" else required).append(gate)
        if constraints:
            workflow["implementation_constraints"] = constraints
        if required:
            workflow["required_gates"] = required
        value["preparation_correction"] = {
            "original_decision_digest": digest(original),
            "schema_error": "Preparation contains unsupported workflow fields: gates",
            "classifications": classifications,
        }
        return value
    if "Prepare this selected canonical Ready item" in prompt:
        item = json.JSONDecoder().raw_decode(prompt.split("Canonical item: ", 1)[1])[0]
        config = json.loads(Path(os.environ["PREPARATION_FIXTURE"]).read_text())
        return {
            "item_id": item["item_id"],
            "provider_revision": item["revision"],
            "workflow": config,
            "authority_evidence": [
                {
                    "path": "PROJECT.yaml",
                    "reason": "Authoritative project policy",
                    "sha256": sha256((cwd / "PROJECT.yaml").read_bytes()).hexdigest(),
                }
            ],
        }
    return legacy_dispatch(prompt, cwd, native_home, argv, session_id)
