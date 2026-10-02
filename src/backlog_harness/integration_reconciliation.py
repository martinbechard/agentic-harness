"""Bounded integration effects; callers retain workflow and native review authority."""

import json
import os
import subprocess
from pathlib import Path

from .contracts import digest
from .evidence import atomic_json, operation_lock
from .provider import TransitionBlocked, git
from .workflow import require

MAX_INTEGRATION_ATTEMPTS = 2


def integration_suffix(attempt):
    require(
        type(attempt) is int and 1 <= attempt <= MAX_INTEGRATION_ATTEMPTS,
        "Invalid integration attempt; review or re-estimate the exhausted integration stage",
    )
    return "" if attempt == 1 else f"-{attempt}"


def integration_attempt(stage, base):
    for attempt in range(1, MAX_INTEGRATION_ATTEMPTS + 1):
        if stage == base + integration_suffix(attempt):
            return attempt
    require(False, "Invalid integration stage name")


def _read(path):
    return json.loads(path.read_text())


def prepare_candidate(
    repository,
    workspace,
    evidence_dir,
    *,
    primary,
    candidate,
    base,
    generators=(),
    candidate_repository=None,
    allowed_paths=(),
    generated_paths=(),
    cancellation_event=None,
    allow_resolution=False,
    force_resolution=False,
):
    """Freeze a merged candidate without changing the primary checkout.

    Generator commands are existing project commands, supplied by validated configuration.
    An interrupted generator is not automatically repeated: its effects need reconciliation.
    """
    repository, workspace, evidence_dir = map(Path, (repository, workspace, evidence_dir))
    intent = {
        "primary": primary,
        "candidate": candidate,
        "base": base,
        "candidate_repository": str(Path(candidate_repository or repository).resolve()),
        "allowed_paths": list(allowed_paths),
        "generated_paths": list(generated_paths),
        "workspace": str(workspace.resolve()),
        "generators": [list(x) for x in generators],
        "allow_resolution": allow_resolution,
    }
    if force_resolution:
        intent["force_resolution"] = True
    with operation_lock(evidence_dir / "lock"):
        require(git(repository, "rev-parse", "HEAD") == primary, "Primary advanced")
        for revision in (primary, base):
            require(
                git(repository, "rev-parse", revision + "^{commit}") == revision,
                "Expected exact commit identity",
            )
        git(repository, "merge-base", "--is-ancestor", base, primary)
        intent_path, result_path = evidence_dir / "intent.json", evidence_dir / "candidate.json"
        if intent_path.exists():
            require(_read(intent_path) == intent, "Integration inputs changed")
        else:
            require(not workspace.exists(), "Integration workspace already exists")
            atomic_json(intent_path, intent, exclusive=True)
        if result_path.exists():
            result = _read(result_path)
            require(
                git(workspace, "rev-parse", "HEAD") == result["candidate"],
                "Frozen integration candidate changed",
            )
            require(
                git(workspace, "rev-parse", "HEAD^{tree}") == result["tree"],
                "Frozen integration tree changed",
            )
            require(not git(workspace, "status", "--porcelain"), "Integration workspace dirty")
            return result
        if allow_resolution and (evidence_dir / "resolution-request.json").exists():
            return _read(evidence_dir / "resolution-request.json")
        if workspace.exists():
            raise TransitionBlocked("Integration effect uncertain; reconcile retained workspace")
        git(repository, "clone", "--no-hardlinks", "--no-checkout", str(repository), str(workspace))
        git(workspace, "fetch", "--no-tags", str(candidate_repository or repository), candidate)
        require(
            git(workspace, "rev-parse", candidate + "^{commit}") == candidate,
            "Expected exact candidate identity",
        )
        git(workspace, "merge-base", "--is-ancestor", base, candidate)
        git(workspace, "checkout", "--detach", primary)
        merge = subprocess.run(
            ["git", "-C", str(workspace), "merge", "--no-commit", "--no-ff", candidate],
            capture_output=True,
            text=True,
            check=False,
        )
        conflicts = git(workspace, "diff", "--name-only", "--diff-filter=U").splitlines()
        if merge.returncode:
            require(conflicts, "Merge failed without resolvable conflicts")
            atomic_json(
                evidence_dir / "conflicts.json",
                {"paths": conflicts, "stderr": merge.stderr[-2000:]},
                exclusive=True,
            )
        if allow_resolution and (
            force_resolution
            or (conflicts and not (generators and set(conflicts) <= set(generated_paths)))
        ):
            require(set(conflicts) <= set(allowed_paths), "Conflict paths exceed accepted scope")
            resolution_paths = list(allowed_paths) if force_resolution else conflicts
            record = {
                "status": "resolution-required",
                "workspace": str(workspace.resolve()),
                "primary": primary,
                "original_candidate": candidate,
                "base": base,
                "conflict_paths": conflicts,
                "allowed_paths": sorted(set(resolution_paths) | set(generated_paths)),
                "candidate_allowed_paths": list(allowed_paths),
                "generated_paths": list(generated_paths),
                "generators": [list(a) for a in generators],
                "inputs_digest": digest(intent),
            }
            atomic_json(
                evidence_dir / "resolution-before.json",
                _workspace_snapshot(workspace),
                exclusive=True,
            )
            atomic_json(evidence_dir / "resolution-request.json", record, exclusive=True)
            return record
        if conflicts:
            require(
                generators
                and set(conflicts) <= set(generated_paths)
                and set(generated_paths) <= set(allowed_paths),
                "Integration conflicts require scoped resolution: " + repr(conflicts),
            )
            for path in conflicts:
                git(workspace, "checkout", "--ours", "--", path)
                git(workspace, "add", "--", path)
        return _finish_candidate(
            workspace, evidence_dir, intent, cancellation_event=cancellation_event
        )


def _workspace_snapshot(workspace):
    """Retain worktree and index identities independently, including unmerged stages."""

    def raw(*args):
        return subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True, check=True
        ).stdout

    index = {}
    for entry in filter(None, raw("ls-files", "--stage", "-z").split(b"\0")):
        identity, name = entry.split(b"\t", 1)
        index.setdefault(os.fsdecode(name), []).append(identity.decode())
    names = set(
        filter(
            None, raw("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        )
    )
    files = {}
    for raw_name in names:
        name = os.fsdecode(raw_name)
        path = workspace / name
        if path.is_symlink():
            files[name] = {"mode": path.lstat().st_mode, "link": os.readlink(path)}
        elif path.is_file():
            from hashlib import sha256

            files[name] = {
                "mode": path.stat().st_mode,
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
        else:
            files[name] = None
    return {"files": files, "index": index}


def finalize_resolution(
    repository, workspace, evidence_dir, record, result_digest, *, cancellation_event=None
):
    """Validate retained resolver effects once; preserve failed/uncertain attempts."""
    repository, workspace, evidence_dir = map(Path, (repository, workspace, evidence_dir))
    with operation_lock(evidence_dir / "lock"):
        require(
            record == _read(evidence_dir / "resolution-request.json"), "Resolution request changed"
        )
        intent = _read(evidence_dir / "intent.json")
        require(digest(intent) == record["inputs_digest"], "Resolution input binding differs")
        require(git(repository, "rev-parse", "HEAD") == record["primary"], "Primary advanced")
        result_path = evidence_dir / "candidate.json"
        if result_path.exists():
            result = _read(result_path)
            require(
                git(workspace, "rev-parse", "HEAD") == result["candidate"]
                and not git(workspace, "status", "--porcelain"),
                "Resolved candidate changed",
            )
            require(
                _read(evidence_dir / "resolution-result.json")["result_digest"] == result_digest,
                "Resolver result changed",
            )
            return result
        require(
            not (evidence_dir / "resolution-result.json").exists(),
            "Resolution finalization uncertain; reconcile retained effects",
        )
        require(
            git(workspace, "rev-parse", "HEAD") == record["primary"]
            and git(workspace, "rev-parse", "MERGE_HEAD") == record["original_candidate"],
            "Resolver changed integration parents",
        )
        require(
            not git(workspace, "diff", "--name-only", "--diff-filter=U"),
            "Resolver left unmerged paths",
        )
        before, after = (
            _read(evidence_dir / "resolution-before.json"),
            _workspace_snapshot(workspace),
        )
        for kind in ("files", "index"):
            changed = {
                name
                for name in before[kind].keys() | after[kind].keys()
                if before[kind].get(name) != after[kind].get(name)
            }
            require(changed <= set(record["allowed_paths"]), "Resolver changed undeclared paths")
        for name in record["conflict_paths"]:
            path = workspace / name
            require(not path.is_symlink(), "Resolved conflict must not be a symlink")
            if path.is_file():
                require(
                    not any(
                        line.startswith((b"<<<<<<< ", b"=======", b">>>>>>> "))
                        for line in path.read_bytes().splitlines()
                    ),
                    "Resolver retained conflict markers",
                )
        require(
            isinstance(result_digest, str) and result_digest, "Verified resolver result required"
        )
        atomic_json(
            evidence_dir / "resolution-result.json",
            {"result_digest": result_digest},
            exclusive=True,
        )
        return _finish_candidate(
            workspace, evidence_dir, intent, cancellation_event=cancellation_event
        )


def _finish_candidate(workspace, evidence_dir, intent, *, cancellation_event=None):
    primary, candidate, base = (intent[key] for key in ("primary", "candidate", "base"))
    generators, generated_paths, allowed_paths = (
        intent[key] for key in ("generators", "generated_paths", "allowed_paths")
    )
    result_path = evidence_dir / "candidate.json"
    before_generators = git(workspace, "write-tree")
    for index, argv in enumerate(generators):
        require(argv and all(isinstance(x, str) for x in argv), "Invalid generator command")
        from .integration_commands import run_generator

        git(workspace, "add", "-A")
        atomic_json(
            evidence_dir / f"generator-{index}-requested.json",
            {"argv": list(argv), "input_tree": git(workspace, "write-tree")},
            exclusive=True,
        )
        run = run_generator(
            argv,
            workspace,
            cancellation_event=cancellation_event,
            record_result=lambda receipt, index=index: atomic_json(
                evidence_dir / f"generator-{index}-result.json", receipt, exclusive=True
            ),
        )
        require(
            run.returncode == 0 and run.failure_reason is None and run.process_group_quiescent,
            "Integration generator failed or termination is uncertain",
        )
    for path in generated_paths:
        output = workspace / path
        require(output.is_file() and not output.is_symlink(), "Generated output missing")
        require(
            not any(
                line.startswith((b"<<<<<<< ", b"=======", b">>>>>>> "))
                for line in output.read_bytes().splitlines()
            ),
            "Generated output contains conflict markers",
        )
    require(
        not git(workspace, "diff", "--name-only", "--diff-filter=U"),
        "Unresolved integration conflicts",
    )
    git(workspace, "add", "-A")
    tree = git(workspace, "write-tree")
    if generators:
        generated_changes = git(
            workspace, "diff", "--name-only", before_generators, tree
        ).splitlines()
        require(
            set(generated_changes) <= set(generated_paths),
            "Integration generator changed undeclared source paths",
        )
    changed = git(workspace, "diff", "--name-only", primary, tree).splitlines()
    require(
        allowed_paths and all(path in allowed_paths for path in changed),
        "Integration changes exceed allowed paths",
    )
    # Stable commit metadata means recovery cannot produce a different identity.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Harness integration",
        "GIT_AUTHOR_EMAIL": "harness@localhost",
        "GIT_COMMITTER_NAME": "Harness integration",
        "GIT_COMMITTER_EMAIL": "harness@localhost",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "commit-tree",
            tree,
            "-p",
            primary,
            "-p",
            candidate,
            "-m",
            "Reconcile integration candidate",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(workspace, "reset", "--hard", commit)
    result = {
        "candidate": commit,
        "tree": tree,
        "primary": primary,
        "original_candidate": candidate,
        "base": base,
        "workspace": str(workspace.resolve()),
    }
    atomic_json(result_path, result, exclusive=True)
    return result


def authorize_candidate(record, review, checks, authority, *, verify_review):
    """Bind existing task authority only after independent native review verification.

    The callback is the existing scoped native-evidence verifier, not an agent verdict.
    The caller persists this separate superseding record without replacing old evidence.
    """
    candidate = record["candidate"]
    require(candidate != record["original_candidate"], "New integration candidate required")
    require(
        review.get("candidate") == candidate and review.get("verdict") == "ACCEPT",
        "Fresh integration review required",
    )
    require(
        review.get("reviewer_session")
        and review.get("producer_session")
        and review["reviewer_session"] != review["producer_session"],
        "Independent reviewer required",
    )
    require(verify_review(review) is True, "Native integration review evidence invalid")
    require(
        checks
        and all(c.get("candidate") == candidate and c.get("returncode") == 0 for c in checks),
        "Merged-tree checks required",
    )
    require(
        authority.get("source_reference") and authority.get("authorization"),
        "Existing task authorization required",
    )
    return {
        "candidate": candidate,
        "original_candidate": record["original_candidate"],
        "primary": record["primary"],
        "review": review,
        "checks": checks,
        "authority": authority,
    }
