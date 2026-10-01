"""Prospective budgets require provider evidence and preserve historical unknowns."""

import json
from dataclasses import asdict, replace

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import TransitionBlocked, git
from backlog_harness.workflow import validate_transition


def test_prospective_estimate_requires_coordinator_and_unknown_baseline(provider):
    item = replace(provider.item("item-one"), original_high=None)
    authority = {
        "role": "coordinator",
        "invocation_id": "estimate",
        "observed_result": True,
        "item_id": item.item_id,
        "operation": "record-estimate",
        "prospective_high": 90000,
        "estimate": {"kind": "prospective_pre_execution", "dated_at": "2026-10-01"},
    }
    validate_transition(item, "Ready", authority)
    for changes in (
        {"role": "orchestrator"},
        {"prospective_high": None},
        {"prospective_high": True},
        {"prospective_high": -1},
        {"estimate": {}},
    ):
        with pytest.raises(TransitionBlocked):
            validate_transition(item, "Ready", {**authority, **changes})
    for changes in ({"original_high": 100}, {"owner": "active"}, {"state": "Running"}):
        with pytest.raises(TransitionBlocked):
            validate_transition(replace(item, **changes), "Ready", authority)


def test_assignment_requires_committed_prospective_evidence(config_file, provider, monkeypatch):
    config, data = config_file
    data.update(repository=str(provider.repository), operational_root=str(provider.evidence_root))
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    initial = provider.item("item-one")
    path = provider.repository / initial.path
    path.write_text(
        path.read_text().replace(
            "Original High Generated Tokens: 100", "Original estimate: unknown"
        )
    )
    git(provider.repository, "add", "--", initial.path)
    git(provider.repository, "commit", "-m", "Preserve unknown original estimate")
    item = provider.item("item-one")
    assert item.original_high is None
    with pytest.raises(TransitionBlocked, match="missing"):
        app.assignment_estimate(item)
    record = {
        "item": asdict(item),
        "head": git(provider.repository, "rev-parse", "HEAD"),
        "target": "Ready",
        "paths": [item.path],
        "stage_operation": "estimate-operation",
        "authority": {
            "operation": "record-estimate",
            "prospective_high": 90000,
            "role": "coordinator",
            "invocation_id": "estimate",
            "observed_result": True,
            "item_id": item.item_id,
            "estimate": {"kind": "prospective_pre_execution", "dated_at": "2026-10-01"},
        },
    }
    path.write_text(path.read_text() + "\nProspective Execution High: 90000\n")
    git(provider.repository, "add", "--", item.path)
    git(provider.repository, "commit", "-m", "Record prospective estimate")
    current = provider.item(item.item_id)
    commit = git(provider.repository, "rev-parse", "HEAD")
    receipt = app.verify_provider_receipt(
        record,
        {
            "operation_id": record["stage_operation"],
            "before_revision": item.revision,
            "commit": commit,
            "after": asdict(current),
        },
    )
    receipt["advancement_verified"] = True
    atomic_json(
        app.root / "provider-agent-operations" / component(digest(record)) / "requested.json",
        record,
    )
    estimate = {
        "provider_revision": current.revision,
        "prospective_high": 90000,
        "receipt": receipt,
    }
    evidence = app._stage_path(item.item_id, "prospective-estimate")
    atomic_json(evidence, estimate)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    atomic_json(
        app._stage_path(item.item_id, "estimate-decision"),
        {
            "role": "coordinator",
            "invocation_id": "estimate",
            "text": json.dumps(
                {
                    "item_id": item.item_id,
                    "provider_revision": item.revision,
                    "estimate": record["authority"]["estimate"],
                    "prospective_high": 90000,
                }
            ),
        },
    )
    assert app.assignment_estimate(current) == 90000
    assert current.original_high is None
    atomic_json(
        app._stage_path(item.item_id, "assignment"),
        {
            "original_high": 90000,
            "historical_original_high": None,
            "prospective_estimate_operation": receipt["operation"],
        },
    )
    usage = app.usage_view(item.item_id, observed_item=current)
    assert usage["accounting_scope"] == "prospective_execution"
    assert usage["historical_usage"] == "unknown"
    assert usage["historical_original_high"] is None
    assert usage["ceiling"] == 180000
    for bad in (
        {**estimate, "provider_revision": "stale"},
        {**estimate, "prospective_high": 180000},
        {**estimate, "receipt": {**receipt, "advancement_verified": False}},
    ):
        atomic_json(evidence, bad)
        with pytest.raises(TransitionBlocked):
            app.assignment_estimate(current)

    for invalid_high in (-1, True):
        invalid_record = {
            **record,
            "authority": {**record["authority"], "prospective_high": invalid_high},
        }
        operation = digest(invalid_record)
        atomic_json(
            app.root / "provider-agent-operations" / component(operation) / "requested.json",
            invalid_record,
        )
        atomic_json(
            evidence,
            {
                **estimate,
                "prospective_high": invalid_high,
                "receipt": {**receipt, "operation": operation},
            },
        )
        with pytest.raises(TransitionBlocked):
            app.assignment_estimate(current)
    atomic_json(evidence, estimate)
    corrupt = {**record, "stage_operation": "altered"}
    atomic_json(
        app.root / "provider-agent-operations" / component(digest(record)) / "requested.json",
        corrupt,
    )
    with pytest.raises(TransitionBlocked, match="operation differs"):
        app.assignment_estimate(current)
    atomic_json(app._stage_path(item.item_id, "assignment"), {"original_high": 100})
    assert "accounting_scope" not in app.usage_view(item.item_id, observed_item=current)


@pytest.mark.parametrize("date", [True, [], "yesterday", "2026-02-30", "", "2026-1-1"])
def test_invalid_estimate_dates(provider, date):
    item = replace(provider.item("item-one"), original_high=None)
    with pytest.raises(TransitionBlocked):
        validate_transition(
            item,
            "Ready",
            {
                "role": "coordinator",
                "invocation_id": "estimate",
                "observed_result": True,
                "item_id": item.item_id,
                "operation": "record-estimate",
                "prospective_high": 90000,
                "estimate": {"kind": "prospective_pre_execution", "dated_at": date},
            },
        )


def test_record_estimate_retains_decision_and_replays_same_operation(
    config_file, provider, monkeypatch, tmp_path
):
    import asyncio

    from backlog_harness.estimation import record_estimate
    from backlog_harness.provider import AgentProvider

    app = Application(config_file[0])
    app.provider = AgentProvider(provider.repository, provider.evidence_root)
    item = replace(provider.item("item-one"), original_high=None)
    monkeypatch.setattr(app.provider, "item", lambda _: item)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    value = {
        "item_id": item.item_id,
        "provider_revision": item.revision,
        "historical_original_high": None,
        "historical_usage": "unknown",
        "prospective_high": 90000,
        "estimate": {
            "kind": "prospective_pre_execution",
            "dated_at": "2026-10-01",
            "generated_tokens": {"low": 45000, "high": 90000},
        },
    }
    decision = {
        "role": "coordinator",
        "outcome": "returned",
        "invocation_id": "estimate",
        "session": {"session_id": "coordinator", "native_session_id": "native"},
        "text": json.dumps(value),
    }
    source = tmp_path / "decision.json"
    atomic_json(source, decision)
    operations = []

    async def transition(item_id, revision, target, authority, **kwargs):
        kwargs["validate"](item, target, authority)
        assert authority["estimate"] == value["estimate"]
        assert authority["prospective_high"] == 90000
        operations.append(digest([item_id, revision, target, authority]))
        return {"after": {**asdict(item), "revision": "after"}, "advancement_verified": True}

    monkeypatch.setattr(app, "transition", transition)
    first = asyncio.run(record_estimate(app, item.item_id, source))
    # Provider revision changed after the effect; replay must use retained original input.
    monkeypatch.setattr(app.provider, "item", lambda _: replace(item, revision="after"))
    assert asyncio.run(record_estimate(app, item.item_id, source)) == first
    assert len(set(operations)) == 1
    assert json.loads(app._stage_path(item.item_id, "estimate-decision").read_text()) == decision
