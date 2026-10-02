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
