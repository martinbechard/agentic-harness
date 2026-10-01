"""External native double for failed-check repair; the harness owns generation."""

import importlib.util
import json
import os
from pathlib import Path

from backlog_harness.provider import git


def helper(name):
    path = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatch(prompt, cwd, native_home, argv, session_id):
    if "Do not claim successful resolution when blocked.\n" in prompt:
        request = json.JSONDecoder().raw_decode(
            prompt.split("Do not claim successful resolution when blocked.\n", 1)[1]
        )[0]
        feedback = request["request"]["previous_feedback"]
        assert feedback["kind"] == "checks-failed"
        assert request["request"]["attempt"] == 2
        assert sum(row["returncode"] != 0 for row in feedback["checks"]) == 2
        lines = (cwd / "answer.txt").read_text().splitlines()
        if os.environ["HARNESS_SYSTEM_SCENARIO"] != "check-correction-exhausted":
            lines[1] = lines[-1]
        (cwd / "answer.txt").write_text("\n".join(lines) + "\n")
        git(cwd, "add", "answer.txt")
        return {
            "item_id": "item-one",
            "resolution": "resolved",
            "resolution_digest": request["resolution_digest"],
            "evidence": "Updated obsolete premise for retained primary advance",
        }
    if "Running is now recorded for your exact session" in prompt:
        lines = (cwd / "answer.txt").read_text().splitlines()
        lines[0] = "done"
        text = "\n".join(lines) + "\n"
        (cwd / "answer.txt").write_text(text)
        (cwd / "generated.txt").write_text(json.dumps({"source": text, "runs": 0}))
        git(cwd, "add", "answer.txt", "generated.txt")
        git(cwd, "commit", "-m", "Candidate source and generated output")
        candidate = git(cwd, "rev-parse", "HEAD")
        reviewer = helper("graph_agent").native_review(native_home, session_id, candidate)
        repo = Path(os.environ["HARNESS_SYSTEM_REPO"])
        lines = (repo / "answer.txt").read_text().splitlines()
        lines[-1] = "primary advance"
        (repo / "answer.txt").write_text("\n".join(lines) + "\n")
        git(repo, "add", "answer.txt")
        git(repo, "commit", "-m", "Concurrent primary advance")
        return {
            "item_id": "item-one",
            "candidate": candidate,
            "reviewer_session": reviewer,
            "request_completion": True,
        }
    return helper("integration_agent").dispatch(prompt, cwd, native_home, argv, session_id)
