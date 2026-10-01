"""Two independent external producers rendezvous before generating their scoped work."""

import importlib.util
import json
import os
import re
import time
from hashlib import sha256
from pathlib import Path


def dispatch(prompt, cwd, native_home, argv, session_id):
    helper = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "legacy_agent.py"
    spec = importlib.util.spec_from_file_location("legacy_multitask", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = Path(os.environ["HARNESS_SYSTEM_REPO"])
    if "Running is now recorded for your exact session" in prompt:
        item = re.findall(r"Work Item ID: (.+)", prompt)[-1]
        barrier = repository.parent / "overlap"
        barrier.mkdir(exist_ok=True)
        mark = {
            "item": item,
            "cwd": str(cwd),
            "session": session_id,
            "pid": os.getpid(),
            "started": time.time_ns(),
        }
        (barrier / (item + ".json")).write_text(json.dumps(mark))
        deadline = time.monotonic() + 20
        while len(list(barrier.glob("item-*.json"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(list(barrier.glob("item-*.json"))) == 2, "Both native work turns did not overlap"
        result = module.dispatch(prompt, cwd, native_home, argv, session_id)
        mark["finished"] = time.time_ns()
        (barrier / (item + ".json")).write_text(json.dumps(mark))
        return result
    result = module.dispatch(prompt, cwd, native_home, argv, session_id)
    if "policy" in result:
        content = (repository / "PROJECT.yaml").read_bytes()
        result["policy"] = {
            "eligible": True,
            "mode": "MULTITASK",
            "primary_branch": "main",
            "evidence": [
                {
                    "path": "PROJECT.yaml",
                    "sha256": sha256(content).hexdigest(),
                    "excerpt": "execution_mode: MULTITASK",
                    "supports": ["mode", "admission"],
                }
            ],
        }
    return result
