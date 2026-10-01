"""External transport fixture: pause only the canonical work turn for stop testing."""

import importlib.util
import json
import os
import time
from pathlib import Path


def dispatch(prompt, cwd, native_home, argv, session_id):
    if "Running is now recorded for your exact session" in prompt:
        ready = Path(os.environ["HARNESS_SYSTEM_REPO"]).parent / "slow-agent-ready.json"
        ready.write_text(
            json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(), "session": session_id})
        )
        time.sleep(60)
        raise RuntimeError("Fixture cancellation did not arrive")
    path = Path(os.environ["HARNESS_SYSTEM_HELPERS"]) / "legacy_agent.py"
    spec = importlib.util.spec_from_file_location("runtime_legacy_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.dispatch(prompt, cwd, native_home, argv, session_id)
