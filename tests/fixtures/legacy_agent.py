"""Deterministic management decisions, with real committed file-provider effects."""

import json
import os
import re
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from backlog_harness.provider import FileProvider, git


def policy(repo):
    return {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": "PROJECT.yaml",
                "sha256": sha256((repo / "PROJECT.yaml").read_bytes()).hexdigest(),
                "excerpt": "execution_mode: SOLO",
                "supports": ["mode", "admission"],
            }
        ],
    }


def work_item_id(prompt):
    return re.findall(r"^Work Item ID: ([^\n]+)$", prompt, re.MULTILINE)[-1]


def dispatch(prompt, cwd, native_home, argv, session_id):
    repo = Path(os.environ["HARNESS_SYSTEM_REPO"])
    provider = FileProvider(repo, repo / ".agent-ops/fixture-provider")
    if "Observe the authoritative file provider" in prompt:
        items = provider.snapshot()
        dependencies = {
            i.item_id: re.findall(r"^Depends On: (.+)$", i.content, re.MULTILINE) for i in items
        }
        unresolved = any(
            name not in dependencies for names in dependencies.values() for name in names
        )
        return {
            "items": [asdict(i) for i in items],
            "policy": policy(repo),
            "questions": {
                i.item_id: provider.question(i) for i in items if i.state == "User Action Required"
            },
            "archive_debt": [],
            "non_items": [],
            "dependencies": dependencies,
            **({"dependency_omissions": "unknown"} if unresolved else {}),
            "transition_paths": {
                i.item_id: {"Completed": [i.path, "backlog/archive/" + Path(i.path).name]}
                for i in items
            },
        }
    if "Decide whether to admit" in prompt:
        return {
            "operation": "new",
            "item_id": work_item_id(prompt),
            "provider_revision": re.search(r"Provider revision: (.+)", prompt)[1],
            "reason": "Authorized fixture",
        }
    if "Provider reservation is Starting" in prompt:
        return {"item_id": re.search(r'"item_id":\s*"([^"]+)"', prompt)[1], "accepted": True}
    if "Classify the exact operator answer" in prompt:
        request = json.loads(prompt[prompt.index("\n{") + 1 :])
        return {
            "question_id": request["question"]["question_id"],
            "answer_digest": request["answer"]["digest"],
            "disposition": "approve",
            "reason": "Explicit answer",
        }
    if "Perform only this authorized provider transition" in prompt:
        record = json.loads(prompt[prompt.index("\n{") + 1 :])
        item = record["item"]
        target = record["target"]
        authority = record["authority"]
        source = repo / item["path"]
        content = source.read_text()
        content = re.sub(r"^Status: .+$", "Status: " + target, content, flags=re.MULTILINE)
        owner = record["target_owner"]
        if owner:
            content = re.sub(r"^Owner: .+$", "Owner: " + owner, content, flags=re.MULTILINE)
        destination = (
            next((p for p in record["paths"] if p != item["path"]), item["path"])
            if target in ("Completed", "User Action Required")
            else item["path"]
        )
        out = repo / destination
        out.parent.mkdir(parents=True, exist_ok=True)
        if out != source:
            source.unlink()
        if authority.get("question"):
            if record.get("ready_question_content"):
                content = record["ready_question_content"]
            else:
                content += (
                    "\n## Harness Transition Evidence\n```json\n"
                    + json.dumps({"authority": authority})
                    + "\n```\n"
                )
        out.write_text(content)
        git(repo, "add", "--", *record["paths"])
        git(repo, "commit", "-m", "Fixture " + target)
        if (
            os.environ.get("HARNESS_SYSTEM_SCENARIO") == "legacy-crash-after-commit"
            and target == "Starting"
        ):
            os._exit(7)
        return {
            "operation_id": record["operation_id"],
            "before_revision": item["revision"],
            "commit": git(repo, "rev-parse", "HEAD"),
            "after": {
                "item_id": item["item_id"],
                "path": destination,
                "state": target,
                "owner": owner,
                "original_high": item["original_high"],
            },
            "policy": policy(repo),
            **({"question": authority["question"]} if authority.get("question") else {}),
        }
    if "Running is now recorded for your exact session" in prompt:
        if (
            os.environ.get("HARNESS_SYSTEM_SCENARIO") == "legacy-question"
            and "Persisted canonical approval:" not in prompt
        ):
            return {
                "item_id": work_item_id(prompt),
                "question": {"question_id": "language", "text": "Which language?"},
            }
        import importlib.util

        helper = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "graph_agent.py"
        spec = importlib.util.spec_from_file_location("review_helper", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        allowed = json.loads(re.search(r"Allowed implementation paths: (\[.*?\])", prompt)[1])
        scenario = os.environ.get("HARNESS_SYSTEM_SCENARIO")
        for name in allowed:
            (cwd / name).write_text("wrong\n" if scenario == "legacy-correct" else "done\n")
        git(cwd, "add", "--", *allowed)
        git(cwd, "commit", "-m", "Fixture implementation")
        candidate = git(cwd, "rev-parse", "HEAD")
        reviewer = module.native_review(
            native_home,
            session_id,
            candidate,
            module.source_review_request(prompt),
            os.environ.get("SOURCE_REVIEW_FAULT"),
            module.coordination_block(prompt),
        )
        if scenario in ("legacy-correct", "legacy-reject"):
            child = next(native_home.glob("sessions/*/*/*/*" + reviewer + ".jsonl"))
            child.write_text(child.read_text().replace("ACCEPT", "REJECT"))
        if scenario == "legacy-correct":
            for name in allowed:
                (cwd / name).write_text("done\n")
            git(cwd, "add", "--", *allowed)
            git(cwd, "commit", "--amend", "--no-edit")
            candidate = git(cwd, "rev-parse", "HEAD")
            reviewer = module.native_review(
                native_home,
                session_id,
                candidate,
                module.source_review_request(prompt),
                os.environ.get("SOURCE_REVIEW_FAULT"),
                module.coordination_block(prompt),
            )
        if scenario == "legacy-stale-review":
            git(cwd, "commit", "--amend", "-m", "Different candidate after review")
            candidate = git(cwd, "rev-parse", "HEAD")
        return {
            "item_id": work_item_id(prompt),
            "candidate": candidate,
            "reviewer_session": reviewer,
            "request_completion": True,
        }
    raise RuntimeError("Unsupported external scenario prompt: " + prompt[:160])


def after_terminal_output(prompt):
    """External process pause exposes a deterministic supervisor-crash boundary."""
    if os.environ.get("HARNESS_SYSTEM_SCENARIO") != "legacy-crash-after-terminal":
        return
    if "Perform only this authorized provider transition" not in prompt:
        return
    record = json.loads(prompt[prompt.index("\n{") + 1 :])
    if record["target"] != "Starting":
        return
    import time

    marker = Path(os.environ["HARNESS_SYSTEM_REPO"]).parent / "terminal-pause.json"
    marker.write_text(json.dumps({"pid": os.getpid(), "operation": record["operation_id"]}))
    time.sleep(60)
