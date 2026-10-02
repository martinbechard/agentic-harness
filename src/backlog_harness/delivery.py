"""Main-branch delivery and recovery are separate from provider closure."""

import json
import os
import stat
import subprocess
from hashlib import file_digest, sha256
from pathlib import Path

from .evidence import atomic_json
from .provider import AgentProvider, TransitionBlocked, git
from .workflow import require, validate_candidate


def preserved_untracked(repository):
    """Snapshot unrelated user files without staging, moving, or claiming them."""
    require(
        not git(repository, "status", "--porcelain", "--untracked-files=no"),
        "Primary tracked files or index are dirty; reconcile without resetting",
    )
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    inventory = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
        env=env,
    )
    if inventory.returncode:
        raise TransitionBlocked(inventory.stderr.decode(errors="replace")[:2000])
    names = inventory.stdout.split(b"\0")
    result = {}
    for raw in filter(None, names):
        name = os.fsdecode(raw)
        path = repository / name
        mode = path.lstat().st_mode
        require(stat.S_ISREG(mode) or stat.S_ISLNK(mode), "Unsupported untracked file type")
        if stat.S_ISLNK(mode):
            content_digest = sha256(os.fsencode(os.readlink(path))).hexdigest()
        else:
            with path.open("rb") as stream:
                content_digest = file_digest(stream, "sha256").hexdigest()
        result[name] = {"mode": mode, "sha256": content_digest}
    return result


def compatible_primary_paths(primary, candidate_repo, candidate, base, head, changed):
    """Permit overlap only when the primary tree already has the exact candidate entries."""
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LITERAL_PATHSPECS": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)

    def raw(repository, *args):
        result = subprocess.run(
            ["git", "-C", str(repository), *args], capture_output=True, check=False, env=env
        )
        if result.returncode:
            raise TransitionBlocked(result.stderr.decode(errors="replace")[:2000])
        return result.stdout

    advanced = raw(primary, "diff", "--name-only", "-z", base, head, "--", *changed)
    return all(
        raw(primary, "ls-tree", "-z", head, "--", os.fsdecode(name))
        == raw(candidate_repo, "ls-tree", "-z", candidate, "--", os.fsdecode(name))
        for name in advanced.split(b"\0")
        if name
    )


def integrate(
    app,
    item_id,
    candidate_repo,
    candidate,
    base,
    review,
    checks,
    *,
    expected_owner=None,
    expected_revision=None,
):
    path = app._stage_path(item_id, "delivery")
    primary = app.config.repository
    policy_check = lambda: app.execution_policy(item_id)
    transaction = (
        app.provider.transaction(policy_check=policy_check)
        if isinstance(app.provider, AgentProvider)
        else app.provider.transaction()
    )
    with transaction:
        policy_check() if isinstance(app.provider, AgentProvider) else app.provider.policy()
        if not path.exists() and expected_owner is not None:
            current = app.provider.item(item_id)
            require(
                current.state == "Running"
                and current.owner == expected_owner
                and current.revision == expected_revision,
                "Canonical delivery ownership or revision changed",
            )
        if hasattr(app, "item_workflow") and app.item_workflow(item_id).get("design_review"):
            from .recovery_flow import verify_design_acceptance

            verify_design_acceptance(app, item_id, base)
        superseding = app._stage_path(item_id, "superseding-delivery-authorization")
        if superseding.exists():
            from .integration_flow import load_integration

            integrated_authority = load_integration(app, item_id, candidate)
            record = integrated_authority["candidate_record"]
            require(
                path.exists() or git(primary, "rev-parse", "HEAD") == record["primary"],
                "Primary advanced after integration review",
            )
            candidate_repo, candidate, base = (
                Path(record["workspace"]),
                record["candidate"],
                record["primary"],
            )
            review, checks = integrated_authority["review"], integrated_authority["checks"]
        if hasattr(app, "item_workflow"):
            from .recovery_flow import verify_candidate_approval, verify_configured_proof

            verify_configured_proof(app, item_id, candidate)
            verify_candidate_approval(app, item_id, candidate, include_recovery=False)
        untracked = preserved_untracked(primary)
        changed = validate_candidate(
            candidate_repo,
            candidate,
            base,
            (
                app.item_workflow(item_id)
                if hasattr(app, "item_workflow")
                else app.config.data["workflow"]
            )["allowed_paths"],
            review["producer_session"],
            review,
            checks,
            preserved=bool(app.recovery_record(item_id))
            if hasattr(app, "recovery_record")
            else False,
        )
        require(
            not any(
                loose == changed_path
                or loose.startswith(changed_path + "/")
                or changed_path.startswith(loose + "/")
                for loose in untracked
                for changed_path in changed
            ),
            "Untracked paths overlap reviewed source paths",
        )
        prior = json.loads(path.read_text()) if path.exists() else None
        if prior:
            require(
                prior.get("preserved_untracked", {}) == untracked,
                "Preserved untracked files changed during delivery",
            )
            require(
                prior.get("candidate") == candidate and prior.get("base", base) == base,
                "Delivery is bound to another candidate",
            )
        else:
            require(
                compatible_primary_paths(primary, candidate_repo, candidate, base, "HEAD", changed),
                "Primary source paths advanced after candidate base",
            )
            prior = {
                "candidate": candidate,
                "base": base,
                "main_before": git(primary, "rev-parse", "HEAD"),
                "disposition": "REQUESTED",
                "preserved_untracked": untracked,
            }
            atomic_json(path, prior, exclusive=True)
        if prior["disposition"] == "READY":
            require(prior.get("verified") is True, "Delivery receipt is unverified")
            git(primary, "merge-base", "--is-ancestor", prior["main_commit"], "HEAD")
            require(
                not git(primary, "diff", "--name-only", candidate, "HEAD", "--", *changed),
                "Reviewed paths advanced before provider closure",
            )
            return prior
        require(
            prior.get("main_before"), "Historical uncertain delivery lacks its pre-effect identity"
        )
        before = prior["main_before"]
        head = git(primary, "rev-parse", "HEAD")
        if head == before:
            if expected_owner is not None:
                current = app.provider.item(item_id)
                require(
                    current.state == "Running"
                    and current.owner == expected_owner
                    and current.revision == expected_revision,
                    "Canonical delivery ownership or revision changed",
                )
            # The first-parent history proves the merge has not occurred. Recheck all
            # original gates before executing this deterministic Git effect once.
            require(
                not (primary / ".git/MERGE_HEAD").exists(),
                "An incomplete merge needs manual resolution",
            )
            require(
                compatible_primary_paths(primary, candidate_repo, candidate, base, head, changed),
                "Primary source changed",
            )
            git(primary, "fetch", "--no-tags", str(candidate_repo), candidate)
            git(primary, "merge", "--no-ff", "--no-edit", "--no-overwrite-ignore", candidate)
            head = git(primary, "rev-parse", "HEAD")
        matches = []
        for commit in git(primary, "rev-list", "--first-parent", before + ".." + head).splitlines():
            if git(primary, "rev-list", "--parents", "-n", "1", commit).split() == [
                commit,
                before,
                candidate,
            ]:
                matches.append(commit)
        require(len(matches) == 1, "Delivery lacks a unique merge proof; no merge repeated")
        integrated = matches[0]
        require(
            not git(primary, "diff", "--name-only", candidate, head, "--", *changed),
            "Integrated source differs from the reviewed candidate",
        )
        checks_path = app._stage_path(item_id, "integrated-checks")
        integrated_checks = json.loads(checks_path.read_text()) if checks_path.exists() else []
        if not integrated_checks or any(
            c.get("candidate") != integrated or c.get("returncode") != 0 for c in integrated_checks
        ):
            require(head == integrated, "Cannot rerun integrated checks after main advanced")
            integrated_checks = app.checks(primary, item_id, integrated, "integrated-checks")
        require(
            preserved_untracked(primary) == untracked,
            "Integrated verification changed preserved untracked files",
        )
        git(primary, "merge-base", "--is-ancestor", candidate, "HEAD")
        result = {
            "disposition": "READY",
            "verified": True,
            "candidate": candidate,
            "base": base,
            "main_commit": integrated,
            "review": review,
            "checks": checks,
            "integrated_checks": integrated_checks,
            "changed_paths": changed,
            "requested_lifecycle": "Completed",
            "item_id": item_id,
            "completion_provider": "main-branch",
            "publication_required": False,
            "preserved_untracked": untracked,
        }
        atomic_json(path, result)
        return result
