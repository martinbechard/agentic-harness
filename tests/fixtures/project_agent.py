"""Deterministic agents for an isolated Git test project, never a real backlog."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def load(root, identity):
    matches = list((root / "backlog").glob(f"*/{identity}.json"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one item {identity} in {root}, got {matches}")
    return matches[0], json.loads(matches[0].read_text())


def save(root, item, status=None):
    identity = item["id"]
    if status:
        item["status"] = status
    destination = root / "backlog" / item["status"] / f"{identity}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for old in (root / "backlog").glob(f"*/{identity}.json"):
        if old != destination:
            old.unlink()
    destination.write_text(json.dumps(item, indent=2) + "\n")


def commit(root, message):
    git(root, "add", "--all")
    if git(root, "status", "--porcelain"):
        git(root, "commit", "-m", message)


def committed_items(root):
    revision = git(root, "rev-parse", "HEAD")
    paths = git(root, "ls-tree", "-r", "--name-only", revision, "backlog").splitlines()
    return [
        json.loads(git(root, "show", f"{revision}:{path}"))
        for path in paths
        if path.endswith(".json")
    ]


def access(request, root, main, control):
    action = request["action"]
    if action == "ready":
        known = {i["id"]: Path(i["worktree"]) for i in request["workspaces"]}
        selected = []
        committed = committed_items(main)
        completed = {i["id"] for i in committed if i["status"] == "completed"}
        for item in sorted(committed, key=lambda i: i["id"]):
            recovering = item.get("interrupted_candidate", False) and item["status"] == "running"
            if item["status"] != "ready" and not recovering:
                continue
            if item["id"] in request["excluded"]:
                continue
            if request["epic"]:
                in_epic = item.get("epic") == request["epic"]
                if in_epic == request["outside"]:
                    continue
            if any(dep not in completed for dep in item.get("dependencies", [])):
                continue
            worktree = known.get(item["id"], main.parent / "worktrees" / item["id"])
            branch = "item/" + item["id"]
            if worktree.exists():
                _, local = load(worktree, item["id"])
                if recovering and local["status"] == "running":
                    # Fixture provider reconciles an explicitly recorded stopped attempt.
                    save(worktree, local, "ready")
                elif local["status"] != "ready":
                    continue
            else:
                worktree.parent.mkdir(exist_ok=True)
                git(main, "worktree", "add", "-b", branch, str(worktree), "main")
            selected.append({"id": item["id"], "worktree": str(worktree), "branch": branch})
            if len(selected) == request["limit"]:
                break
        return {"items": selected}
    if action == "epic_complete":
        items = committed_items(main)
        return {
            "complete": all(
                i["status"] == "completed" for i in items if i.get("epic") == request["epic"]
            )
        }
    _, item = load(root, request["item"]["id"])
    if action == "status":
        return {"status": item["status"]}
    if action == "failure":
        item.setdefault("failures", []).append(request["reason"])
        save(root, item)
        return {"transient": item.get("retryable", True)}
    if action == "hold":
        save(root, item, "holding")
        commit(root, "Hold failed item")
        return {"updated": True}
    raise ValueError(action)


def development(request, root, main, control):
    identity = request["item"]["id"]
    assert git(root, "branch", "--show-current") == request["item"]["branch"]
    _, item = load(root, identity)
    count_path = control / f"{identity}.attempts"
    attempt = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(attempt))
    save(root, item, "running")
    mode = item.get("mode", "success")
    print(f"Delivering {identity}, attempt {attempt}", flush=True)
    if mode in {"interrupted", "restart"} and attempt == 1:
        (root / "partial.txt").write_text("cannot resume" if mode == "restart" else "continue me")
        (root / "unrelated.txt").write_text("preserve me")
        sys.exit(3)
    if mode in {"interrupted", "restart"}:
        assert "previously started but interrupted" in request["instruction"]
        assert (root / "unrelated.txt").read_text() == "preserve me"
        if mode == "restart":
            assert "preserve unrelated" in request["instruction"]
            (root / "partial.txt").unlink()
        else:
            assert (root / "partial.txt").read_text() == "continue me"
            (root / "partial.txt").write_text("resumed")
    if mode == "timeout":
        time.sleep(30)
    if mode == "controlled":
        (control / f"{identity}.started").write_text(str(os.getpid()))
        release = control / f"{identity}.release"
        while not release.exists():
            time.sleep(0.02)
    if mode == "failure" or (mode == "transient" and attempt == 1):
        return {"status": "failed", "transient": mode == "transient", "detail": "fixture failure"}
    if mode == "user_action":
        item["question"] = "Human user: supply the acceptance example."
        save(root, item, "user-action-required")
        commit(root, "Record question for human user")
        return {"status": "user_action_required"}
    products = root / "products"
    products.mkdir(exist_ok=True)
    (products / f"{identity}.txt").write_text("bad" if mode == "bad_merge" else "verified")
    if mode in {"blocking_defect", "unrelated_defect"}:
        defect = {
            "id": identity + "-defect",
            "status": "ready",
            "evidence": "Discovered by test agent",
        }
        save(root, defect)
        if mode == "blocking_defect":
            (products / f"{defect['id']}.txt").write_text("verified")
            save(root, defect, "completed")
    save(root, item, "awaiting-merge")
    time.sleep(item.get("commit_delay", 0))
    # Stage only owned paths; unrelated work remains local.
    git(root, "add", "backlog", "products")
    if mode == "interrupted":
        git(root, "add", "partial.txt")
    git(root, "commit", "-m", f"Deliver {identity}")
    return {"status": "success"}


def merge(request, root, main, control):
    lock = control / "merge.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise AssertionError("Overlapping merge agents") from None
    try:
        for line in git(main, "worktree", "list", "--porcelain").splitlines():
            if not line.startswith("worktree "):
                continue
            worktree = Path(line.removeprefix("worktree "))
            if worktree == main:
                continue
            for path in sorted((worktree / "backlog" / "awaiting-merge").glob("*.json")):
                branch = git(worktree, "branch", "--show-current")
                candidate = git(main, "rev-parse", branch)
                relative = path.relative_to(worktree).as_posix()
                try:
                    item = json.loads(git(main, "show", f"{candidate}:{relative}"))
                except subprocess.CalledProcessError:
                    continue  # The working-tree status has not been committed yet.
                if not item.get("approved", True):
                    continue
                if (main / "backlog" / "completed" / path.name).exists():
                    continue
                before = git(main, "rev-parse", "HEAD")
                git(main, "merge", "--no-ff", "--no-commit", candidate)
                test = subprocess.run(
                    [sys.executable, str(main / "check.py")], cwd=main, check=False
                )
                if test.returncode:
                    git(main, "merge", "--abort")
                    assert git(main, "rev-parse", "HEAD") == before
                    (control / "merge-rejected").write_text(item["id"])
                    return {"status": "failed", "detail": "Combined tests failed"}
                save(main, item, "completed")
                commit(main, f"Integrate {item['id']} after combined tests")
        return {"status": "success"}
    finally:
        lock.rmdir()


def main():
    request = json.loads(Path(os.environ["HARNESS_REQUEST"]).read_text())
    root = Path.cwd()
    settings = json.loads((root / "fixture.json").read_text())
    main, control = Path(settings["main"]), Path(settings["control"])
    result = {"access": access, "development": development, "merge": merge}[request["role"]](
        request, root, main, control
    )
    destination = Path(os.environ["HARNESS_RESULT"])
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(destination)


if __name__ == "__main__":
    main()
