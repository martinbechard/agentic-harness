"""Deterministic invalid observation and valid policy-only correction."""

import json
import os
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from backlog_harness.provider import FileProvider


def policy(repo, excerpt):
    project = repo / "PROJECT.yaml"
    return {
        "eligible": excerpt == "execution_mode: SOLO",
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": "PROJECT.yaml",
                "sha256": sha256(project.read_bytes()).hexdigest(),
                "excerpt": excerpt,
                "supports": ["mode", "admission"],
            }
        ],
    }


def dispatch(prompt, cwd, native_home, argv, session_id):
    repo = Path(os.environ["HARNESS_SYSTEM_REPO"])
    if "Observe the authoritative file provider" in prompt:
        provider = FileProvider(repo, repo / ".agent-ops/fixture-provider")
        items = []
        for item in provider.snapshot():
            row = asdict(item)
            row.pop("content")
            row.pop("revision")
            row["state"] = item.state.upper()
            items.append(row)
        return {
            "items": items,
            "dependencies": {item["item_id"]: [] for item in items},
            "policy": policy(
                repo,
                "execution_mode: SOLO\ncanonical_primary_branch: main",
            ),
            "questions": {},
            "archive_debt": [],
            "non_items": [],
            "transition_paths": {
                item["item_id"]: {
                    "Completed": [
                        item["path"],
                        "backlog/archive/" + Path(item["path"]).name,
                    ]
                }
                for item in items
            },
        }
    if "Reassess global admission from current source authority" in prompt:
        request = json.loads(prompt[prompt.rfind("\n{") + 1 :])
        return {
            "source_revision": request["source_revision"],
            "reason": "Canonical project authority permits independent Ready work",
            "policy": policy(repo, "execution_mode: SOLO"),
        }
    raise RuntimeError("Unsupported policy fixture prompt: " + prompt[:160])
