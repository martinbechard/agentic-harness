"""External fixture: candidate plus concurrent compatible primary change and review."""

import importlib.util
import json
import os
import re
from pathlib import Path

from backlog_harness.provider import git


def helper(name):
    path = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatch(prompt, cwd, native_home, argv, session_id):
    if "Resolve only the listed semantic conflicts" in prompt:
        request = json.JSONDecoder().raw_decode(
            prompt.split("Do not claim successful resolution when blocked.\n", 1)[1]
        )[0]
        if (
            os.environ.get("HARNESS_SYSTEM_SCENARIO") == "integration-conflict-resolver-correction"
            and request["request"].get("attempt", 1) == 1
        ):
            return {
                "item_id": "item-one",
                "resolution": "blocked",
                "resolution_digest": request["resolution_digest"],
                "evidence": "First bounded attempt needs explicit correction feedback",
            }
        (cwd / "answer.txt").write_text(json.dumps({"enabled": True, "flags": ["keep"]}) + "\n")
        if os.environ.get("HARNESS_SYSTEM_SCENARIO") == "integration-conflict-scope-violation":
            (cwd / "unrelated.txt").write_text("outside declared resolution scope\n")
        git(cwd, "add", "answer.txt")
        return {
            "item_id": "item-one",
            "resolution": "resolved",
            "resolution_digest": request["resolution_digest"],
            "evidence": "Preserved enabled feature and primary keep flag",
        }
    if "Integration review identity: " in prompt:
        request = json.JSONDecoder().raw_decode(
            prompt.split("Integration review identity: ", 1)[1].split("\n", 1)[1]
        )[0]
        rejected = os.environ.get(
            "HARNESS_SYSTEM_SCENARIO"
        ) == "integration-conflict-review-correction" and not Path(
            request["candidate_record"]["workspace"]
        ).name.endswith("-2")
        return {
            "candidate": request["candidate_record"]["candidate"],
            "verdict": "REJECT" if rejected else "ACCEPT",
            "unresolved_findings": ["Recheck both parent intents in a fresh correction"]
            if rejected
            else [],
            "evidence": ["Both parent changes retained; checks passed"],
        }
    if "Running is now recorded for your exact session" in prompt:
        path = cwd / "answer.txt"
        conflict = os.environ.get("HARNESS_SYSTEM_SCENARIO", "").startswith("integration-conflict")
        if conflict:
            path.write_text(json.dumps({"enabled": True, "flags": []}) + "\n")
        else:
            lines = path.read_text().splitlines()
            lines[0] = "done"
            path.write_text("\n".join(lines) + "\n")
        git(cwd, "add", "answer.txt")
        git(cwd, "commit", "-m", "Candidate first line")
        candidate = git(cwd, "rev-parse", "HEAD")
        reviewer = helper("graph_agent").native_review(native_home, session_id, candidate)
        # Simulated concurrent source change, separate from the candidate's own workspace.
        repo = Path(os.environ["HARNESS_SYSTEM_REPO"])
        primary = repo / "answer.txt"
        if conflict:
            primary.write_text(json.dumps({"enabled": False, "flags": ["keep"]}) + "\n")
        else:
            lines = primary.read_text().splitlines()
            lines[-1] = "primary advance"
            primary.write_text("\n".join(lines) + "\n")
        git(repo, "add", "answer.txt")
        git(repo, "commit", "-m", "Concurrent primary last line")
        return {
            "item_id": re.findall(r"Work Item ID: (.+)", prompt)[-1],
            "candidate": candidate,
            "reviewer_session": reviewer,
            "request_completion": True,
        }
    return helper("legacy_agent").dispatch(prompt, cwd, native_home, argv, session_id)


def after_terminal_output(prompt):
    """Pause only the completed resolver so the test can crash its supervisor."""
    if os.environ.get("HARNESS_SYSTEM_SCENARIO") != "integration-conflict-resolver-crash":
        return
    if "Resolve only the listed semantic conflicts" not in prompt:
        return
    import time

    marker = Path(os.environ["HARNESS_SYSTEM_REPO"]).parent / "resolver-terminal-pause.json"
    marker.write_text(json.dumps({"pid": os.getpid()}))
    time.sleep(60)
