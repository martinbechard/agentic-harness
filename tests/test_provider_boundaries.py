"""Provider inventory rejects ambiguity and unsafe paths before any mutation."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from backlog_harness.provider import AgentProvider, TransitionBlocked, parse_item


@pytest.mark.parametrize("fault", ["duplicate_header", "identity", "state"])
def test_file_item_header_is_unambiguous(provider, fault):
    item = provider.item("item-one")
    content = item.content
    if fault == "duplicate_header":
        content = content.replace("Status: Ready", "Status: Ready\nStatus: Ready")
    elif fault == "identity":
        content = content.replace("Work Item ID: item-one", "Work Item ID: other")
    else:
        content = content.replace("Status: Ready", "Status: invented")
    with pytest.raises(TransitionBlocked):
        parse_item(item.path, content.encode())


@pytest.mark.parametrize("fault", ["missing", "symlink", "duplicate", "changing", "absent_item"])
def test_inventory_requires_stable_unique_local_items(provider, monkeypatch, fault):
    repo = provider.repository
    path = repo / "backlog/feature-backlog/item-one.md"
    if fault == "missing":
        (repo / "backlog").rename(repo / "saved-backlog")
    elif fault == "symlink":
        (path.parent / "linked.md").symlink_to(path)
    elif fault == "duplicate":
        other = repo / "backlog/other/item-one.md"
        other.parent.mkdir()
        other.write_bytes(path.read_bytes())
    elif fault == "changing":
        original = Path.read_bytes
        reads = 0

        def changing(selected):
            nonlocal reads
            data = original(selected)
            if selected == path:
                reads += 1
                return data + (b"\n" if reads > 1 else b"")
            return data

        monkeypatch.setattr(Path, "read_bytes", changing)
    with pytest.raises(TransitionBlocked):
        provider.item("absent" if fault == "absent_item" else "item-one")


def test_inventory_ignores_future_ideas_and_documentation(provider):
    root = provider.repository / "backlog"
    (root / "README.md").write_text("Backlog documentation")
    (root / "future-ideas").mkdir()
    (root / "future-ideas/idea.md").write_text("Not yet an item")
    assert [item.item_id for item in provider.snapshot()] == ["item-one"]
    agent = AgentProvider(provider.repository, provider.evidence_root)
    assert "backlog/future-ideas/idea.md" not in agent.source_manifest()


@pytest.mark.parametrize("fault", ["mode", "concurrency", "route", "branch"])
def test_file_policy_rejects_unsupported_execution_before_mutation(provider, fault):
    path = provider.repository / "PROJECT.yaml"
    value = yaml.safe_load(path.read_text())
    if fault == "mode":
        value["execution_mode"] = "invented"
    elif fault == "concurrency":
        value["project_setup"] = {"concurrent_tasking": True}
    elif fault == "route":
        value["workflow_selection"]["persistence"]["default"] = "other"
    else:
        value["workflow_selection"]["canonical_primary_branch"] = "other"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(TransitionBlocked):
        provider.policy()


def test_waiting_item_needs_actual_question_evidence(provider):
    item = replace(provider.item("item-one"), state="User Action Required")
    with pytest.raises(TransitionBlocked, match="question evidence is missing"):
        provider.question(item)


def test_agent_observation_rejects_symlinked_sources(provider):
    (provider.repository / "backlog/linked.md").symlink_to(provider.repository / "PROJECT.yaml")
    agent = AgentProvider(provider.repository, provider.evidence_root)
    with pytest.raises(TransitionBlocked, match="Unsafe provider observation path"):
        agent.source_manifest()


def test_agent_requires_observation_before_inventory(provider):
    agent = AgentProvider(provider.repository, provider.evidence_root)
    with pytest.raises(TransitionBlocked, match="observation is required"):
        agent.observation()


@pytest.mark.parametrize("crash", [None, "before_commit", "after_request"])
def test_archive_completion_recovers_once_and_preserves_content(provider, monkeypatch, crash):
    from test_provider_coordination import authority

    from backlog_harness import provider as module
    from backlog_harness.provider import git
    from backlog_harness.workflow import validate_transition

    source = provider.repository / "backlog/feature-backlog/item-one.md"
    source.write_text(
        source.read_text()
        .replace("Status: Ready", "Status: Running")
        .replace("Owner: Unowned", "Owner: canonical")
    )
    git(provider.repository, "add", "--", str(source))
    git(provider.repository, "commit", "-m", "Running fixture")
    item = provider.item("item-one")
    before = git(provider.repository, "rev-parse", "HEAD")
    actor = authority(
        "orchestrator",
        session_id="canonical",
        delivery={
            "disposition": "READY",
            "verified": True,
            "candidate": "candidate",
            "review": "review",
            "checks": "checks",
            "integrated_checks": "checks",
            "main_commit": before,
        },
    )
    original_git = module.git
    original_json = module.atomic_json

    def interrupt_request(path, value, **kwargs):
        original_json(path, value, **kwargs)
        if path.name == "requested.json":
            raise RuntimeError("supervisor lost after request")

    def interrupt(repo, *args):
        if args[0] == "commit":
            raise RuntimeError("supervisor lost before commit")
        return original_git(repo, *args)

    if crash:
        if crash == "before_commit":
            monkeypatch.setattr(module, "git", interrupt)
        else:
            monkeypatch.setattr(module, "atomic_json", interrupt_request)
        with pytest.raises(RuntimeError, match="supervisor lost"):
            provider.transition(
                item.item_id, item.revision, "Completed", actor, validate=validate_transition
            )
        monkeypatch.setattr(module, "git", original_git)
        monkeypatch.setattr(module, "atomic_json", original_json)
        record = next(
            (provider.evidence_root / "provider-operations").glob("*/requested.json")
        ).parent
        receipt = provider.recover_prepared(record, validate=validate_transition)
        assert provider.recover_prepared(record, validate=validate_transition) == receipt
    else:
        receipt = provider.transition(
            item.item_id, item.revision, "Completed", actor, validate=validate_transition
        )
    assert not source.exists()
    archived = provider.item(item.item_id)
    assert archived.state == "Completed"
    assert archived.path == "backlog/completed-backlog/features/item-one.md"
    assert item.content.split("## Objective", 1)[1] in archived.content
    assert git(provider.repository, "rev-parse", "HEAD^") == before
    assert (
        provider.transition(
            item.item_id, item.revision, "Completed", actor, validate=validate_transition
        )
        == receipt
    )
    assert git(provider.repository, "rev-parse", "HEAD") == receipt["provider_commit"]
