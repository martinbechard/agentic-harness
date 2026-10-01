"""Narrow file/main-branch provider transactions with agent-owned resource claims.

The revision, exact-path Git transaction and immutable blob checks adapt the
existing ProjectBridge._transition operation. Its LangGraph/claims controller is
not reused. Agents apply their configured resource-claim workflow independently.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import yaml

from .contracts import digest, utcnow
from .evidence import atomic_json, component, fsync_directory

STATES = {
    "Ready",
    "Starting",
    "Running",
    "Stalled",
    "Blocked",
    "User Action Required",
    "Holding",
    "Awaiting Review",
    "Completed",
    "Failed",
    "Abandoned",
    "Superseded",
}
TERMINAL = {"Completed", "Failed", "Abandoned", "Superseded"}


class TransitionBlocked(ValueError):
    pass


def git(repo, *args):
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, env=env
    )
    if result.returncode:
        raise TransitionBlocked(result.stderr.decode(errors="replace")[:2000])
    return result.stdout.decode().strip()


def blob(repo, commit, path):
    # Preserve bytes; whitespace belongs to the immutable provider identity.
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    return subprocess.check_output(
        ["git", "-C", str(repo), "show", commit + ":" + path],
        env=env,
    )


@dataclass(frozen=True)
class Item:
    item_id: str
    path: str
    revision: str
    state: str
    owner: str
    original_high: int | None
    content: str


def parse_item(path, content):
    text = content.decode("utf-8")
    header = text.split("\n## ", 1)[0]
    fields = re.findall(r"^([A-Za-z][A-Za-z ]+):\s*(.*?)\s*$", header, re.MULTILINE)
    if len(dict(fields)) != len(fields):
        raise TransitionBlocked("Duplicate provider header field")
    values = dict(fields)
    identity = values.get("Work Item ID")
    if not identity or Path(path).stem != identity or values.get("Provider") != "file":
        raise TransitionBlocked("Invalid file-provider identity")
    if values.get("Status") not in STATES:
        raise TransitionBlocked("Unknown lifecycle state")
    estimate = values.get("Original High Generated Tokens", "")
    return Item(
        identity,
        path,
        sha256(path.encode() + b"\0" + content).hexdigest(),
        values["Status"],
        values.get("Owner", "Unowned"),
        int(estimate) if estimate.isdigit() and int(estimate) > 0 else None,
        text,
    )


class FileProvider:
    def __init__(self, repository: Path, evidence_root: Path):
        self.repository, self.evidence_root = repository.resolve(), evidence_root.resolve()
        self.receipt_cache = {}

    def snapshot(self):
        root = self.repository / "backlog"
        if not root.is_dir():
            raise TransitionBlocked("Provider scope is missing")

        def read():
            rows = {}
            for path in sorted(root.rglob("*.md")):
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise TransitionBlocked("Unsafe provider path")
                data = path.read_bytes()
                if path.name in {"README.md", "index.md"} and b"Work Item ID:" not in data:
                    continue
                if "future-ideas" in path.parts:
                    continue
                rows[path.relative_to(self.repository).as_posix()] = data
            return rows

        before, after = read(), read()
        if before != after:
            raise TransitionBlocked("Provider inventory changed during observation")
        items = [parse_item(path, data) for path, data in before.items()]
        if len({i.item_id for i in items}) != len(items):
            raise TransitionBlocked("Duplicate provider identity")
        return items

    def item(self, item_id):
        items = [i for i in self.snapshot() if i.item_id == item_id]
        if len(items) != 1:
            raise TransitionBlocked("Item is absent or ambiguous")
        return items[0]

    def question(self, item):
        if item.state != "User Action Required":
            return None
        events = re.findall(
            r"## Harness Transition Evidence\s+```json\n(.*?)\n```", item.content, re.DOTALL
        )
        for event in reversed(events):
            actor = json.loads(event)["authority"]
            if actor.get("question"):
                return {
                    **actor["question"],
                    "item_id": item.item_id,
                    "item_revision": item.revision,
                    "owner": item.owner,
                    "answer": actor.get("answer"),
                    "disposition": actor.get("disposition"),
                }
        raise TransitionBlocked("Provider question evidence is missing")

    def policy(self):
        project = yaml.safe_load((self.repository / "PROJECT.yaml").read_text())
        route = project.get("workflow_selection", {})
        if project.get("execution_mode") not in {"SOLO", "MULTITASK"}:
            raise TransitionBlocked("This route requires an explicit execution mode")
        concurrent = project.get("project_setup", {}).get("concurrent_tasking", False)
        if type(concurrent) is not bool or concurrent != (project["execution_mode"] == "MULTITASK"):
            raise TransitionBlocked(
                "Execution mode conflicts with project_setup.concurrent_tasking"
            )
        for key, expected in [("persistence", "file"), ("commit", "main-branch")]:
            if route.get(key, {}).get("default") != expected or route[key].get("folder_overrides"):
                raise TransitionBlocked("Unsupported selected provider/completion workflow")
        branch = route.get("canonical_primary_branch")
        if (
            branch not in {"main", "master"}
            or git(self.repository, "symbolic-ref", "--short", "HEAD") != branch
        ):
            raise TransitionBlocked("Configured primary branch differs from attached branch")
        gitdir = Path(git(self.repository, "rev-parse", "--absolute-git-dir")).resolve()
        common = Path(git(self.repository, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = self.repository / common
        if gitdir != common.resolve():
            raise TransitionBlocked("Provider mutation requires the primary worktree")
        return branch

    @contextmanager
    def transaction(self):
        self.policy()
        with (self.repository / ".git/agentic-provider.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.policy()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def transition(self, item_id, expected_revision, target, authority, *, validate):
        """Execute exactly one gate-authorized transition, retaining uncertain receipts."""
        with self.transaction():
            operation = digest([item_id, expected_revision, target, authority])
            record = self.evidence_root / "provider-operations" / component(operation)
            if (record / "requested.json").exists():
                return self.reconcile_operation(record)
            item = self.item(item_id)
            if item.revision != expected_revision:
                raise TransitionBlocked("Stale provider revision")
            validate(item, target, authority)
            if git(self.repository, "status", "--porcelain"):
                raise TransitionBlocked("Provider transaction requires a clean primary checkout")
            head = git(self.repository, "rev-parse", "HEAD")
            source = self.repository / item.path
            target_path = item.path
            if target == "Completed":
                groups = {
                    "feature-backlog": "features",
                    "defect-backlog": "defects",
                    "analysis-backlog": "analyses",
                    "investigation-backlog": "investigations",
                }
                group = groups.get(Path(item.path).parts[1])
                if not group:
                    raise TransitionBlocked("Unsupported archive source")
                target_path = f"backlog/completed-backlog/{group}/{Path(item.path).name}"
            destination = self.repository / target_path
            if destination != source and destination.exists():
                raise TransitionBlocked("Archive target already exists")
            text, separator, body = item.content.partition("\n## ")
            text = re.sub(r"(?m)^Status: .*$", "Status: " + target, text)
            owner = (
                item.owner
                if target in {"Holding", "User Action Required"}
                or item.state in {"Holding", "User Action Required"}
                else authority.get("session_id", item.owner)
            )
            text = re.sub(r"(?m)^Owner: .*$", "Owner: " + owner, text)
            event = {
                "transition": f"{item.state} -> {target}",
                "recorded_at": utcnow(),
                "authority": authority,
                "original_high": item.original_high,
            }
            updated = (
                text
                + separator
                + body
                + "\n\n## Harness Transition Evidence\n\n```json\n"
                + json.dumps(event, sort_keys=True)
                + "\n```\n"
            ).encode()
            paths = list(dict.fromkeys([item.path, target_path]))
            atomic_json(
                record / "requested.json",
                {
                    "version": 1,
                    "head": head,
                    "paths": paths,
                    "before_revision": item.revision,
                    "intended_sha256": sha256(updated).hexdigest(),
                    "authority": authority,
                    "item_id": item_id,
                    "target": target,
                    "target_path": target_path,
                    "intended_text": updated.decode(),
                    "operation": operation,
                },
                exclusive=True,
            )
            # No uncertain mutation is rolled back or retried automatically.
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                if destination != source:
                    os.link(tmp, destination)
                    source.unlink()
                else:
                    os.replace(tmp, destination)
                fsync_directory(destination.parent)
                fsync_directory(source.parent)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            git(self.repository, "add", "--", *paths)
            git(self.repository, "commit", "--only", "-m", f"{item_id}: {target}", "--", *paths)
            commit = git(self.repository, "rev-parse", "HEAD")
            if (
                git(self.repository, "rev-parse", commit + "^") != head
                or set(
                    git(
                        self.repository, "diff", "--no-renames", "--name-only", head, commit
                    ).splitlines()
                )
                != set(paths)
                or blob(self.repository, commit, target_path) != updated
            ):
                raise TransitionBlocked("Immutable provider commit proof failed")
            receipt = {
                "state": target,
                "provider_commit": commit,
                "path": target_path,
                "revision": parse_item(target_path, updated).revision,
                "operation": operation,
            }
            atomic_json(record / "receipt.json", receipt, exclusive=True)
            return receipt

    def recover_prepared(self, record, *, validate):
        """Explicitly finish a proven uncommitted operation; never replay a committed one."""
        with self.transaction():
            record = Path(record)
            request = json.loads((record / "requested.json").read_text())
            if git(self.repository, "rev-parse", "HEAD") != request["head"]:
                return self.reconcile_operation(record)
            source_path, target_path = request["paths"][0], request["target_path"]
            original = blob(self.repository, request["head"], source_path)
            item = parse_item(source_path, original)
            updated = request["intended_text"].encode()
            intended = parse_item(target_path, updated)
            if (
                item.revision != request["before_revision"]
                or item.item_id != request["item_id"]
                or intended.item_id != item.item_id
                or intended.state != request["target"]
                or sha256(updated).hexdigest() != request["intended_sha256"]
                or digest([item.item_id, item.revision, request["target"], request["authority"]])
                != request["operation"]
            ):
                raise TransitionBlocked("Prepared provider identity or bytes changed")
            validate(item, request["target"], request["authority"])
            if (self.repository / ".git/MERGE_HEAD").exists():
                raise TransitionBlocked("An unresolved merge prevents provider recovery")
            changed = set(git(self.repository, "diff", "HEAD", "--name-only").splitlines())
            changed.update(
                git(self.repository, "ls-files", "--others", "--exclude-standard").splitlines()
            )
            if changed - set(request["paths"]):
                raise TransitionBlocked("Unrelated checkout changes prevent provider recovery")
            for relative in request["paths"]:
                path = self.repository / relative
                if path.is_symlink() or not path.resolve().is_relative_to(self.repository):
                    raise TransitionBlocked("Unsafe prepared provider path")
                allowed = (
                    {original, updated}
                    if source_path == target_path
                    else ({original, None} if relative == source_path else {updated, None})
                )
                if (path.read_bytes() if path.exists() else None) not in allowed:
                    raise TransitionBlocked("Prepared checkout bytes conflict with recorded intent")
                staged = subprocess.run(
                    ["git", "-C", str(self.repository), "show", ":" + relative],
                    capture_output=True,
                    check=False,
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
                    },
                )
                if staged.returncode == 0 and staged.stdout not in allowed:
                    raise TransitionBlocked("Prepared index bytes conflict with recorded intent")
            source, destination = self.repository / source_path, self.repository / target_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, destination)
                if source != destination and source.exists():
                    source.unlink()
                fsync_directory(destination.parent)
                fsync_directory(source.parent)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            git(self.repository, "add", "--", *request["paths"])
            git(
                self.repository,
                "commit",
                "--only",
                "-m",
                f"{item.item_id}: {request['target']}",
                "--",
                *request["paths"],
            )
            return self.reconcile_operation(record)

    def reconcile_operation(self, record):
        """Recover an already committed effect by its parent, exact paths and bytes."""
        record = Path(record)
        request = json.loads((record / "requested.json").read_text())
        receipts = record / "receipt.json"
        head = git(self.repository, "rev-parse", "HEAD")
        existing = json.loads(receipts.read_text()) if receipts.exists() else None
        cache_key = digest([head, request, existing])
        if self.receipt_cache.get(record) == cache_key:
            return existing
        commits = git(
            self.repository, "rev-list", "--first-parent", request["head"] + ".." + head
        ).splitlines()
        matches = []
        target_path = request.get("target_path", request["paths"][-1])
        for commit in commits:
            if git(self.repository, "rev-parse", commit + "^") != request["head"]:
                continue
            changed = git(
                self.repository, "diff", "--no-renames", "--name-only", request["head"], commit
            ).splitlines()
            if set(changed) != set(request["paths"]):
                continue
            data = blob(self.repository, commit, target_path)
            if sha256(data).hexdigest() == request["intended_sha256"]:
                matches.append((commit, data))
        if len(matches) != 1:
            raise TransitionBlocked(
                "Provider effect has no unique immutable commit proof; no mutation repeated"
            )
        commit, data = matches[0]
        item = parse_item(target_path, data)
        receipt = {
            "state": item.state,
            "provider_commit": commit,
            "path": target_path,
            "revision": item.revision,
            "operation": request.get("operation", record.name),
        }
        if receipts.exists():
            if any(
                existing[k] != receipt[k] for k in ("state", "provider_commit", "path", "revision")
            ):
                raise TransitionBlocked("Provider receipt conflicts with immutable proof")
            self.receipt_cache[record] = cache_key
            return existing
        atomic_json(receipts, receipt, exclusive=True)
        self.receipt_cache[record] = digest([head, request, receipt])
        return receipt


class AgentProvider:
    """Read-only projection of provider observations produced by the responsible agent."""

    def __init__(self, repository: Path, evidence_root: Path):
        self.repository = repository.resolve()
        self.evidence_root = evidence_root.resolve()
        self.cache_path = self.evidence_root / "provider-observation.json"

    def source_manifest(self):
        # Detect committed and pending provider changes without interpreting document headers.
        files = git(
            self.repository,
            "ls-files",
            "-co",
            "--exclude-standard",
            "--",
            "PROJECT.yaml",
            "backlog",
        ).splitlines()
        values = []
        for name in sorted(set(files)):
            if "future-ideas" in Path(name).parts:
                continue
            path = self.repository / name
            if path.is_symlink() or not path.resolve().is_relative_to(self.repository):
                raise TransitionBlocked("Unsafe provider observation path")
            values.append([name, sha256(path.read_bytes()).hexdigest() if path.is_file() else None])
        return dict(values)

    def source_revision(self):
        return digest([git(self.repository, "rev-parse", "HEAD"), self.source_manifest()])

    def validate_inventory(self, value):
        """Require complete classification while leaving document interpretation to the agent."""
        from .workflow import require

        expected = {
            name
            for name, content in self.source_manifest().items()
            if name.startswith("backlog/") and name.endswith(".md") and content is not None
        }
        rows = value["items"]
        classified = value.get("non_items", [])
        paths = [row["path"] for row in rows]
        for row in classified:
            require(
                row.get("kind") in {"group", "index", "archive_debt", "supporting_document"}
                and isinstance(row.get("reason"), str)
                and row["reason"].strip(),
                "Non-item classification evidence is missing",
            )
            paths.append(row["path"])
        require(
            len(paths) == len(set(paths)) and set(paths) == expected,
            "Provider inventory classification is incomplete or duplicated",
        )
        identities = {row["item_id"] for row in rows}
        dependencies = value.get("dependencies", {})
        require(
            set(dependencies) <= identities
            and (set(dependencies) == identities or value.get("dependency_omissions") == "unknown"),
            "Dependency observations are incomplete",
        )
        require(
            all(
                isinstance(values, list) and all(isinstance(v, str) and v for v in values)
                for values in dependencies.values()
            ),
            "Invalid dependency observations",
        )
        require(
            all(set(values) <= identities for values in dependencies.values())
            or value.get("dependency_omissions") == "unknown",
            "Provider dependency names an unobserved item",
        )
        require(
            set(value.get("questions", {})) <= identities
            and set(value.get("transition_paths", {})) <= identities,
            "Provider metadata names an unobserved item",
        )

    def observation(self):
        if not self.cache_path.exists():
            raise TransitionBlocked("Provider agent observation is required")
        value = json.loads(self.cache_path.read_text())
        if value["source_revision"] != self.source_revision():
            raise TransitionBlocked("Provider observation is stale; refresh through the agent")
        return value

    def snapshot(self):
        return [Item(**value) for value in self.observation()["items"]]

    def item(self, item_id):
        matches = [item for item in self.snapshot() if item.item_id == item_id]
        if len(matches) != 1:
            raise TransitionBlocked("Item is absent or ambiguous")
        return matches[0]

    def policy(self):
        policy = self.observation()["policy"]
        self.validate_policy(policy)
        if policy["eligible"] is not True:
            raise TransitionBlocked("Provider agent did not establish eligible execution policy")
        return policy

    def validate_policy(self, policy):
        """Validate cited source bytes and deterministic route invariants, not agent interpretation."""
        from .workflow import require

        require(
            type(policy.get("eligible")) is bool and policy.get("mode") in {"SOLO", "MULTITASK"},
            "Provider eligibility or execution mode is invalid",
        )
        evidence = policy.get("evidence")
        require(isinstance(evidence, list) and evidence, "Structured policy evidence is required")
        supported = set()
        for reference in evidence:
            name = reference.get("path", "")
            path = self.repository / name
            require(
                name
                and not Path(name).is_absolute()
                and ".." not in Path(name).parts
                and not path.is_symlink()
                and path.resolve().is_relative_to(self.repository),
                "Unsafe policy evidence path",
            )
            content = path.read_bytes()
            require(sha256(content).hexdigest() == reference.get("sha256"), "Stale policy evidence")
            excerpt = reference.get("excerpt")
            require(
                isinstance(excerpt, str) and excerpt and excerpt.encode() in content,
                "Policy evidence excerpt is absent",
            )
            facts = reference.get("supports")
            require(
                isinstance(facts, list) and set(facts) <= {"mode", "admission"},
                "Policy evidence facts are invalid",
            )
            supported.update(facts)
        require(supported == {"mode", "admission"}, "Policy mode or admission authority is missing")
        project = yaml.safe_load((self.repository / "PROJECT.yaml").read_text())
        route = project.get("workflow_selection", {})
        for key, expected in [("persistence", "file"), ("commit", "main-branch")]:
            require(
                route.get(key, {}).get("default") == expected
                and not route[key].get("folder_overrides"),
                "Unsupported selected provider route",
            )
        require(
            route.get("canonical_primary_branch") == policy.get("primary_branch"),
            "Observed primary branch differs from project",
        )
        if "execution_mode" in project:
            require(project["execution_mode"] == policy["mode"], "Observed execution mode differs")
        if policy["mode"] == "MULTITASK":
            require(
                project.get("project_setup", {}).get("concurrent_tasking") is True,
                "MULTITASK has no project concurrency authority",
            )
        if git(self.repository, "symbolic-ref", "--short", "HEAD") != policy["primary_branch"]:
            raise TransitionBlocked("Provider primary branch differs")
        gitdir = Path(git(self.repository, "rev-parse", "--absolute-git-dir")).resolve()
        common = Path(git(self.repository, "rev-parse", "--git-common-dir"))
        require(
            gitdir == (self.repository / common).resolve(), "Provider requires primary checkout"
        )

    def question(self, item):
        return self.observation().get("questions", {}).get(item.item_id)

    def transition(self, *args, **kwargs):
        raise TransitionBlocked("Live provider mutation requires the responsible agent")

    @contextmanager
    def transaction(self):
        """Serialize delivery with agent-managed provider operations in the same checkout."""
        with (self.repository / ".git/agentic-provider.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.policy()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
