"""Synthetic provider owns decisions, revisions, publication and durable history."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

request = json.loads(Path(os.environ["HARNESS_REQUEST"]).read_text())


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def publish(root, locator):
    if (root / ".git").exists():
        if git(root, "status", "--porcelain", "--", locator):
            git(root, "add", "--", locator)
            git(root, "commit", "--only", "-m", "Publish exact human decision", "--", locator)
        assert json.loads(git(root, "show", f"HEAD:{locator}")) == json.loads(
            (root / locator).read_text()
        )


if request["role"] == "development":
    record = json.loads(Path("backlog/item.json").read_text())
    assert record["status"] == "Ready" and record["decisions"]
    Path("received-answer.json").write_text(json.dumps(record["decisions"][-1]))
    record["status"] = "Running"
    record["history"].append("Running: retry received comment")
    Path("backlog/item.json").write_text(json.dumps(record))
    if Path("still-blocked").exists():
        record["status"] = "Blocked"
        record["history"].append("Blocked: blocker rechecked and remains")
        Path("backlog/item.json").write_text(json.dumps(record))
        Path(os.environ["HARNESS_RESULT"]).write_text(
            json.dumps({"status": "user_action_required"})
        )
        raise SystemExit(0)
    Path(os.environ["HARNESS_RESULT"]).write_text(json.dumps({"status": "success"}))
    raise SystemExit(0)

if request["action"] == "ready":
    root = Path.cwd()
    authority = json.loads((root / "authority.json").read_text())
    workspace = Path(authority["delivery_workspace"])
    locator = "backlog/item.json"
    source = json.loads(git(root, "show", f"HEAD:{locator}"))
    local = json.loads((workspace / locator).read_text())
    items = []
    # Refuse conflicts and preserve local delivery history.
    if (
        source["status"] == "Ready"
        and source["decisions"]
        and local["question"] == source["question"]
        and local["candidate"] == source["candidate"]
    ):
        local.update(status=source["status"], decisions=source["decisions"])
        (workspace / locator).write_text(json.dumps(local, indent=2))
        publish(workspace, locator)
        verified = json.loads((workspace / locator).read_text())
        assert verified["decisions"] == source["decisions"]
        items = [{"id": local["id"], "worktree": str(workspace), "branch": "item/one"}]
    Path(os.environ["HARNESS_RESULT"]).write_text(json.dumps({"items": items}))
    raise SystemExit(0)

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
    snapshots = json.loads((project / "observations.json").read_text())
    original = snapshots.get(observed["revision"])
    relevant = (
        "id",
        "status",
        "question",
        "candidate",
        "kind",
        "options",
        "free_text",
        "conditions",
    )
    unchanged = original is not None and all(original.get(k) == record.get(k) for k in relevant)
    expected_revision = revision()
    current = (
        record["id"] == submission["item_id"]
        and (expected_revision == observed["revision"] or unchanged)
        and record["question"] == observed["question"]
        and record["candidate"] == observed["candidate"]
        and record["status"]
        == ("Blocked" if decision["kind"] == "retry_blocked" else "User Action Required")
    )
    acceptable = (
        decision["kind"] in ("cancel", "retry_blocked")
        or (decision["kind"] == "allow" and record["kind"] == "approval")
        or (
            decision["kind"] == "answer"
            and record["kind"] != "approval"
            and (record["free_text"] or decision["answer"] in record["options"])
        )
    )
    historical = record.get("historical_resolution")
    if record["id"] == submission["item_id"] and prior and prior["submission"] == submission:
        result.update(status="already_applied", persisted=True, resolution=prior["resolution"])
    elif (
        historical
        and decision["kind"] != "retry_blocked"
        and (decision["kind"] != "cancel" or historical["resolution"] == "cancelled")
        and record["id"] == submission["item_id"]
        and historical["question"] == observed["question"]
        and historical["candidate"] == observed["candidate"]
    ):
        result.update(
            status="already_resolved", persisted=True, resolution=historical["resolution"]
        )
    elif not prior and current and acceptable and authority.get("native_transactions", True):
        if mode == "concurrent_change":
            changed = {**record, "question": "Approve a different scope?"}
            path.write_text(json.dumps(changed))
        if revision() != expected_revision:
            result["detail"] = "Native conditional update refused a concurrent change"
            Path(os.environ["HARNESS_RESULT"]).write_text(json.dumps(result))
            raise SystemExit(0)
        resolution = {
            "allow": "approved",
            "cancel": "cancelled",
            "answer": "answered",
            "retry_blocked": "retry_requested",
        }[decision["kind"]]
        record["status"] = "Cancelled" if decision["kind"] == "cancel" else "Ready"
        record["decisions"].append({"submission": submission, "resolution": resolution})
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(path)
        record = json.loads(path.read_text())  # Actual persisted readback.
        result.update(status="applied", persisted=True, resolution=resolution)
    result.update(state=record["status"], revision=revision())
    if result["persisted"] and result["status"] != "already_resolved":
        publish(workspace, observed["locator"])
    if result["persisted"]:
        result["detail"] = (
            "Historical approval from earlier human action; stale UAR requires provider state reconciliation"
            if result["status"] == "already_resolved"
            else "Decision verified in persisted item history"
        )
if mode == "sleep_after_write":
    time.sleep(60)
if mode == "lost_result":
    raise SystemExit(9)
if mode == "misbound":
    result["item_id"] = "another-item"
Path(os.environ["HARNESS_RESULT"]).write_text(json.dumps(result))
