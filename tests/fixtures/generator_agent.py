"""External producer for generated projection integration scenarios."""

import importlib.util
import os
import re
import subprocess
from pathlib import Path

from backlog_harness.provider import git


def helper(name):
    path = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatch(prompt, cwd, native_home, argv, session_id):
    if "Running is now recorded for your exact session" in prompt:
        source = cwd / "source.txt"
        lines = source.read_text().splitlines()
        lines[0] = "candidate"
        source.write_text("\n".join(lines) + "\n")
        subprocess.run(
            [os.environ["HARNESS_GENERATOR_PYTHON"], "generate.py"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        git(cwd, "add", "source.txt", "generated.txt")
        git(cwd, "commit", "-m", "Candidate source and projection")
        candidate = git(cwd, "rev-parse", "HEAD")
        reviewer = helper("graph_agent").native_review(native_home, session_id, candidate)
        primary = Path(os.environ["HARNESS_SYSTEM_REPO"])
        lines = (primary / "source.txt").read_text().splitlines()
        lines[-1] = "primary"
        (primary / "source.txt").write_text("\n".join(lines) + "\n")
        subprocess.run(
            [os.environ["HARNESS_GENERATOR_PYTHON"], "generate.py"],
            cwd=primary,
            check=True,
            capture_output=True,
        )
        git(primary, "add", "source.txt", "generated.txt")
        git(primary, "commit", "-m", "Concurrent source and projection")
        return {
            "item_id": re.findall(r"Work Item ID: (.+)", prompt)[-1],
            "candidate": candidate,
            "reviewer_session": reviewer,
            "request_completion": True,
        }
    return helper("integration_agent").dispatch(prompt, cwd, native_home, argv, session_id)
