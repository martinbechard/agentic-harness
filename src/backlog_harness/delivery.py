"""Main-branch delivery and recovery are separate from provider closure."""

import json

from .evidence import atomic_json
from .provider import git
from .workflow import require, validate_candidate


def integrate(app, item_id, candidate_repo, candidate, base, review, checks):
    path = app._stage_path(item_id, "delivery")
    primary = app.config.repository
    with app.provider.transaction():
        app.provider.policy()
        require(
            not git(primary, "status", "--porcelain"),
            "Primary checkout is dirty; reconcile without resetting",
        )
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
        )
        prior = json.loads(path.read_text()) if path.exists() else None
        if prior:
            require(
                prior.get("candidate") == candidate and prior.get("base", base) == base,
                "Delivery is bound to another candidate",
            )
        else:
            require(
                not git(primary, "diff", "--name-only", base, "HEAD", "--", *changed),
                "Primary source paths advanced after candidate base",
            )
            prior = {
                "candidate": candidate,
                "base": base,
                "main_before": git(primary, "rev-parse", "HEAD"),
                "disposition": "REQUESTED",
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
            # The first-parent history proves the merge has not occurred. Recheck all
            # original gates before executing this deterministic Git effect once.
            require(
                not (primary / ".git/MERGE_HEAD").exists(),
                "An incomplete merge needs manual resolution",
            )
            require(
                not git(primary, "diff", "--name-only", base, head, "--", *changed),
                "Primary source changed",
            )
            git(primary, "fetch", "--no-tags", str(candidate_repo), candidate)
            git(primary, "merge", "--no-ff", "--no-edit", candidate)
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
        require(not git(primary, "status", "--porcelain"), "Integrated verification left changes")
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
        }
        atomic_json(path, result)
        return result
