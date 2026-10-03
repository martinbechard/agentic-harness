"""Synthetic provider owns decisions, revisions, publication and durable history."""

import hashlib
import json
import os
import time
from pathlib import Path

request = json.loads(Path(os.environ["HARNESS_REQUEST"]).read_text())
if request["action"] == "status":
    root = Path.cwd()
    (root / "ordinary.started").touch()
    while not (root / "ordinary.release").exists():
        time.sleep(0.01)
    Path(os.environ["HARNESS_RESULT"]).write_text(json.dumps({"status": "running"}))
    raise SystemExit(0)
submission = request["submission"]
project = Path(submission["project"])
workspace = Path(submission["workspace"])
observed = submission["observed"]
path = workspace / observed["locator"]
mode_path = project / "behavior"
mode = mode_path.read_text() if mode_path.exists() else "normal"
authority = json.loads((project / "authority.json").read_text())
result = {
    "status": "rejected",
    "decision_id": submission["decision_id"],
    "item_id": submission["item_id"],
    "persisted": False,
    "resolution": "none",
    "state": "",
    "revision": "",
    "workspace": str(workspace),
    "locator": observed["locator"],
    "detail": "stale or mismatched decision",
}


def revision():
    return hashlib.sha256(observed["locator"].encode() + b"\0" + path.read_bytes()).hexdigest()


if mode == "slow":
    time.sleep(0.1)
if authority["workspace"] == str(workspace) and path.exists():
    record = json.loads(path.read_text())
    decision = submission["decision"]
    prior = next(
        (
            d
            for d in record["decisions"]
            if d["submission"]["decision_id"] == submission["decision_id"]
        ),
        None,
    )
    current = (
        record["id"] == submission["item_id"]
        and revision() == observed["revision"]
        and record["question"] == observed["question"]
        and record["candidate"] == observed["candidate"]
        and record["status"] == "User Action Required"
    )
    acceptable = (
        decision["kind"] == "cancel"
        or (decision["kind"] == "allow" and record["kind"] == "approval")
        or (
            decision["kind"] == "answer"
            and record["kind"] != "approval"
            and (record["free_text"] or decision["answer"] in record["options"])
        )
    )
    if prior and prior["submission"] == submission:
        result.update(status="already_applied", persisted=True, resolution=prior["resolution"])
    elif not prior and current and acceptable:
        resolution = {"allow": "approved", "cancel": "cancelled", "answer": "answered"}[
            decision["kind"]
        ]
        record["status"] = "Cancelled" if decision["kind"] == "cancel" else "Ready"
        record["decisions"].append({"submission": submission, "resolution": resolution})
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(path)
        record = json.loads(path.read_text())  # Actual persisted readback.
        result.update(status="applied", persisted=True, resolution=resolution)
    result.update(state=record["status"], revision=revision())
    if result["persisted"]:
        result["detail"] = "Decision verified in persisted item history"
if mode == "lost_result":
    raise SystemExit(9)
if mode == "misbound":
    result["item_id"] = "another-item"
Path(os.environ["HARNESS_RESULT"]).write_text(json.dumps(result))
