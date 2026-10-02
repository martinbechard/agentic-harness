"""Prospective budgets require provider evidence and preserve historical unknowns."""

import json
from dataclasses import asdict, replace

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import digest
from backlog_harness.evidence import atomic_json, component
from backlog_harness.provider import Item, TransitionBlocked, git
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
    validate_transition(replace(item, owner=None), "Ready", authority)
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


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("normalized_owner", [None, "Unowned"])
def test_real_document_without_owner_estimate_to_public_admission(
    config_file, provider, monkeypatch, tmp_path, normalized_owner, automatic
):
    """Canonical dev-methodology bytes caught the absent-Owner integration mismatch."""
    import asyncio
    from pathlib import Path

    from test_provider_coordination import policy_evidence
    from test_telemetry import payload

    from backlog_harness.estimation import record_estimate
    from backlog_harness.telemetry import Sink

    item_id = "reconcile-documentation-semantic-checks-with-authorized-source-revisions"
    old = provider.repository / provider.item("item-one").path
    old.unlink()
    source = provider.repository / "backlog/defect-backlog" / (item_id + ".md")
    source.parent.mkdir()
    source.write_bytes(
        (Path(__file__).parent / "fixtures/unestimated-documentation-defect.md").read_bytes()
    )
    git(provider.repository, "add", "--", "backlog")
    git(provider.repository, "commit", "-m", "Exact unestimated canonical fixture")
    # Snapshot of dev-methodology/backlog/defect-backlog/<item_id>.md, 2026-10-01.
    from hashlib import sha256

    assert (
        sha256(source.read_bytes()).hexdigest()
        == "2cf653fb93436cfd06b5363f98844c79208f213d2864dba5a2ce2c34d39aa174"
    )
    assert "\nOwner:" not in source.read_text()
    item = replace(
        provider.item(item_id), owner=None
    )  # Captured live AgentProvider representation.
    assert item.owner is None and item.original_high is None
    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    if automatic:
        data["workflow"]["preparation"] = {
            "allowed_roots": ["answer.py"],
            "check_commands": data["workflow"]["checks"],
        }
    for name in ("manage-work-items", "manage-work-items-file"):
        skill = Path(data["methodology_root"]) / "skills" / name / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("Fixture management contract")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": policy_evidence(provider.repository),
    }
    atomic_json(
        app.provider.cache_path,
        {
            "observer_digest": app.provider_observer_digest(app.config),
            "capability_digest": app.provider_capability_digest(
                app.config, app.provider_observer_digest(app.config)
            ),
            "source_revision": app.provider.source_revision(),
            "source_manifest": app.provider.source_manifest(),
            "policy": policy,
            "items": [asdict(item)],
            "dependencies": {item_id: []},
        },
    )
    telemetry = app.root / "fixture-telemetry.jsonl"
    sink = Sink(telemetry, {})
    sink.write(payload())

    def response(stage, value):
        binding = asdict(app.config.binding("coordinator"))
        evidence = app.root / "fixture-invocations" / stage
        atomic_json(
            evidence / "intent.json",
            {
                "invocation_id": stage,
                "request_digest": "fixture-request",
                "binding": binding,
                "config_digest": app.config.file_digest,
            },
        )
        return {
            "invocation_id": stage,
            "request_digest": "fixture-request",
            "role": "coordinator",
            "outcome": "returned",
            "session": {
                "session_id": "coordinator",
                "native_session_id": "native",
                "binding": binding,
            },
            "binding": binding,
            "evidence_path": str(evidence),
            "telemetry_path": str(telemetry),
            "telemetry": {
                "span_count": 1,
                "rejected_exports": 0,
                "evidence_sha256": sink.evidence_digest(),
            },
            "events": [{"type": "turn.completed", "usage": {"output_tokens": 10}}],
            "text": json.dumps(value),
        }

    decision = response(
        "estimate",
        {
            "item_id": item_id,
            "provider_revision": item.revision,
            "prospective_high": 180000,
            "historical_original_high": None,
            "historical_usage": "unknown",
            "estimate": {
                "kind": "prospective_pre_execution",
                "dated_at": "2026-10-01T12:44:11Z",
                "generated_tokens": {"low": 90000, "high": 180000},
            },
        },
    )
    if automatic:
        value = json.loads(decision["text"])
        value["workflow"] = {"allowed_paths": ["answer.py"], "checks": data["workflow"]["checks"]}
        value["authority_evidence"] = [
            {
                "path": "PROJECT.yaml",
                "reason": "Existing project workflow",
                "sha256": sha256((provider.repository / "PROJECT.yaml").read_bytes()).hexdigest(),
            }
        ]
        decision["text"] = json.dumps(value)
    decision_path = tmp_path / "prepared-estimate.json"
    atomic_json(decision_path, decision)
    mutations = []

    class AdmissionReached(Exception):
        pass

    preparations = []

    async def model(item_id, stage, role, prompt, **kwargs):
        if stage == "prepare":
            preparations.append(stage)
            assert kwargs["purpose"] == "provider"
            assert (
                source.read_text()
                in json.loads(prompt.split("\nCanonical item: ", 1)[1])["content"]
            )
            return decision
        if stage == "admit":
            assert "Reconcile Documentation Semantic Checks" in prompt
            raise AdmissionReached
        assert stage.startswith("provider-")
        retained = app._stage_path(item_id, stage)
        if retained.exists():
            return json.loads(retained.read_text())
        operation = kwargs["provider_operation"]
        record = json.loads(
            (
                app.root / "provider-agent-operations" / component(operation) / "requested.json"
            ).read_text()
        )
        source.write_text(
            source.read_text() + "\nProspective Execution High: 180000\nHistorical usage: unknown\n"
        )
        if normalized_owner is not None:
            source.write_text(source.read_text() + "\nOwner: Unowned\n")
        git(provider.repository, "add", "--", str(source.relative_to(provider.repository)))
        git(provider.repository, "commit", "-m", "Record prospective estimate")
        mutations.append(operation)
        current = replace(provider.item(item_id), owner=normalized_owner)
        result = response(
            stage,
            {
                "operation_id": record["stage_operation"],
                "before_revision": item.revision,
                "commit": git(provider.repository, "rev-parse", "HEAD"),
                "after": asdict(current),
                "policy": policy,
            },
        )
        atomic_json(app._stage_path(item_id, stage), result)
        return result

    monkeypatch.setattr(app, "invoke", model)
    if not automatic:
        receipt = asyncio.run(record_estimate(app, item_id, decision_path))
        assert receipt["advancement_verified"] is True
        assert (
            asyncio.run(record_estimate(app, item_id, decision_path))["commit"] == receipt["commit"]
        )
    for attempt in range(2):
        with pytest.raises(AdmissionReached):
            if automatic and attempt == 0:
                from backlog_harness.coordination import RunController

                asyncio.run(RunController(app).run("until-terminal"))
            else:
                asyncio.run(app.run_item(item_id))
    assert len(mutations) == 1
    assert len(preparations) == int(automatic)
    frozen = json.loads(app._stage_path(item_id, "assignment").read_text())
    assert frozen["original_high"] == 180000 and frozen["historical_original_high"] is None
    assert app.provider.item(item_id).owner == normalized_owner
    assert ("\nOwner: Unowned" in source.read_text()) == (normalized_owner == "Unowned")
    usage = app.usage_view(item_id, observed_item=app.provider.item(item_id))
    assert usage["accounting_scope"] == "prospective_execution"
    assert usage["historical_usage"] == "unknown"
    assert usage["historical_original_high"] is None
    assert usage["ceiling"] == 360000
    if automatic:
        # Simulate a crash before assignment persisted, then a changed canonical item.
        from backlog_harness.estimation import prepare_item

        app._stage_path(item_id, "assignment").unlink()
        current = app.provider.item(item_id)
        changed = replace(current, revision="changed", content=current.content + "New requirement")
        with pytest.raises(TransitionBlocked, match="stale or unverified"):
            asyncio.run(prepare_item(app, changed))
        assert len(preparations) == 1 and len(mutations) == 1


@pytest.mark.parametrize(
    "defect",
    [
        "role",
        "path",
        "check",
        "gate",
        "blocked",
        "status",
        "singular_status",
        "unknown_gate",
        "revision",
    ],
)
def test_preparation_rejects_invalid_authority(config_file, monkeypatch, defect):
    from backlog_harness.estimation import configured_workflow, prepared_workflow

    config, data = config_file
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": data["workflow"]["checks"],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    value = {
        "item_id": "one",
        "provider_revision": "revision",
        "workflow": {"allowed_paths": ["answer.py"], "checks": data["workflow"]["checks"]},
    }
    decision = {"role": "coordinator", "text": ""}
    if defect == "role":
        decision["role"] = "orchestrator"
    elif defect == "path":
        value["workflow"]["allowed_paths"] = ["other.py"]
    elif defect == "check":
        value["workflow"]["checks"] = [["sh", "-c", "true"]]
    elif defect == "gate":
        value["workflow"]["completion"] = "skip-review"
    elif defect == "blocked":
        value["blocked"] = "Required authority is missing"
    elif defect == "status":
        value.update(status="blocked", blockers=[{"reason": "Retain prior candidate"}])
    elif defect == "singular_status":
        value.update(status="blocked", blocker={"reason": "Missing verification command"})
    elif defect == "unknown_gate":
        value["workflow"]["required_gates"] = ["New authority"]
    elif defect == "revision":
        value["provider_revision"] = "stale"
    decision["text"] = json.dumps(value)
    decision.update(
        invocation_id="fixture",
        request_digest="request",
        binding={},
        evidence_path=str(app.root / "fixture"),
    )
    atomic_json(
        app.root / "fixture/intent.json",
        {
            "invocation_id": "fixture",
            "request_digest": "request",
            "binding": {},
            "config_digest": app.config.file_digest,
        },
    )
    atomic_json(
        app._stage_path("one", "preparation"),
        {
            "item": {"revision": "revision"},
            "decision": decision,
            "workflow_config_digest": digest(configured_workflow(app.config, "one")),
            "invocation_config_digest": app.config.file_digest,
        },
    )
    with pytest.raises(TransitionBlocked):
        prepared_workflow(app, "one", app.config)


@pytest.mark.parametrize("metadata_shape", [None, "supported", "dashboard", "portrait"])
def test_preparation_config_binding_ignores_other_item(config_file, monkeypatch, metadata_shape):
    from backlog_harness.contracts import load_config
    from backlog_harness.estimation import configured_workflow, prepared_workflow

    config, data = config_file
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": [["git", "diff", "--check"]],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    decision = {
        "role": "coordinator",
        "text": json.dumps(
            {
                "item_id": "one",
                "provider_revision": "revision",
                "workflow": {
                    "persistence": "file",
                    "completion": "main-branch",
                    "canonical_primary_branch": "main",
                    "allowed_paths": ["answer.py"],
                    "checks": [["git", "diff", "--check"]],
                },
            }
        ),
    }
    decision.update(
        invocation_id="fixture",
        request_digest="request",
        binding={},
        evidence_path=str(app.root / "fixture"),
    )
    atomic_json(
        app.root / "fixture/intent.json",
        {
            "invocation_id": "fixture",
            "request_digest": "request",
            "binding": {},
            "config_digest": app.config.file_digest,
        },
    )
    atomic_json(
        app._stage_path("one", "preparation"),
        {
            "item": {"revision": "revision"},
            "decision": decision,
            "workflow_config_digest": digest(configured_workflow(app.config, "one")),
            "invocation_config_digest": app.config.file_digest,
        },
    )
    if metadata_shape:
        from pathlib import Path

        path = app._stage_path("one", "preparation")
        saved = json.loads(path.read_text())
        value = json.loads(saved["decision"]["text"])
        if metadata_shape == "supported":
            metadata = {
                "checks_executed": False,
                "scope_conditions": ["Preserve existing behavior"],
                "verification_limit": "No acceptance checks have run",
            }
        else:
            actual = json.loads(
                (
                    Path(__file__).parent / "fixtures/preparation" / (metadata_shape + ".json")
                ).read_text()
            )
            metadata = {
                key: value
                for key, value in actual.items()
                if key
                not in {
                    "allowed_paths",
                    "checks",
                    "persistence",
                    "completion",
                    "canonical_primary_branch",
                }
            }
        value["workflow"].update(metadata)
        saved["decision"]["text"] = json.dumps(value)
        atomic_json(path, saved)
        before = path.read_bytes()
        if metadata_shape != "supported":
            for _ in range(2):
                with pytest.raises(
                    TransitionBlocked, match="unsupported acceptance gates before dispatch"
                ) as error:
                    prepared_workflow(app, "one", app.config)
                assert json.dumps(metadata["required_gates"]) in str(error.value)
            assert path.read_bytes() == before
            return
    expected = prepared_workflow(app, "one", app.config)
    if metadata_shape:
        assert expected["preparation_evidence"]["metadata"] == metadata
        assert expected["preparation_evidence"]["decision_digest"] == digest(saved["decision"])
        assert path.read_bytes() == before
    assert data["workflow"]["checks"][0] in expected["checks"]
    data["workflow"]["items"] = {"other": {"allowed_paths": ["other.py"]}}
    config.write_text(yaml.safe_dump(data))
    assert prepared_workflow(app, "one", load_config(config)) == expected
    intent_path = app.root / "fixture/intent.json"
    original_intent = json.loads(intent_path.read_text())
    atomic_json(intent_path, {**original_intent, "invocation_id": "changed"})
    with pytest.raises(TransitionBlocked, match="invocation configuration differs"):
        prepared_workflow(app, "one", load_config(config))
    atomic_json(intent_path, original_intent)
    data["workflow"]["items"]["one"] = {"allowed_paths": ["changed.py"]}
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(TransitionBlocked, match="configuration changed"):
        prepared_workflow(app, "one", load_config(config))


@pytest.mark.parametrize(
    "racing", [None, "config_digest", "invocation_id", "request_digest", "binding"]
)
def test_preparation_uses_fresh_configuration(config_file, monkeypatch, racing):
    import asyncio

    from backlog_harness.estimation import prepare_item

    config, data = config_file
    app = Application(config)
    data["workflow"]["preparation"] = {
        "allowed_roots": ["fresh.py"],
        "check_commands": [["git", "diff", "--check"]],
    }
    config.write_text(yaml.safe_dump(data))
    item = Item("one", "one.md", "revision", "Ready", None, None, "Full canonical requirements")
    calls = []
    captured = app.config

    async def invoke(*args, **kwargs):
        calls.append(args)
        assert "fresh.py" in args[3]
        result = {
            "role": "coordinator",
            "config_digest": app.config.file_digest,
            "text": json.dumps(
                {
                    "item_id": "one",
                    "provider_revision": "revision",
                    "blocked": "No source authority established",
                }
            ),
        }
        result.update(
            invocation_id="prepare",
            request_digest="request",
            binding={"role": "coordinator"},
            evidence_path=str(app.root / "native"),
        )
        intent = {
            "invocation_id": result["invocation_id"],
            "request_digest": result["request_digest"],
            "binding": result["binding"],
            "config_digest": app.config.file_digest,
        }
        if racing:
            intent[racing] = "different"
        atomic_json(app.root / "native/intent.json", intent)
        result.pop("config_digest")
        # Simulate another run-loop iteration replacing the shared config object.
        app.config = captured
        return result

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    if racing:
        with pytest.raises(TransitionBlocked, match="invocation configuration differs"):
            asyncio.run(prepare_item(app, item))
        assert not app._stage_path("one", "preparation").exists()
    else:
        # Reached the fresh configured invocation; invalid result cannot advance.
        with pytest.raises(TransitionBlocked, match="Preparation blocked"):
            asyncio.run(prepare_item(app, item))
    assert len(calls) == 1
    if not racing:
        from backlog_harness.contracts import load_config
        from backlog_harness.estimation import configured_workflow

        saved = json.loads(app._stage_path("one", "preparation").read_text())
        assert saved["workflow_config_digest"] == digest(
            configured_workflow(load_config(config), "one")
        )
    assert not app._stage_path("one", "estimate-input").exists()


@pytest.mark.parametrize(
    "mismatch", [None, "invocation_id", "request_digest", "config_digest", "binding"]
)
def test_retained_native_preparation_envelopes(tmp_path, mismatch):
    from pathlib import Path
    from types import SimpleNamespace

    from backlog_harness.estimation import validate_preparation_invocation

    envelopes = json.loads(
        (Path(__file__).parent / "fixtures/native-preparation-envelopes.json").read_text()
    )
    result, intent = envelopes["result"], envelopes["intent"]
    assert "config_digest" not in result
    config = SimpleNamespace(file_digest=intent["config_digest"])
    result["evidence_path"] = str(tmp_path)
    if mismatch:
        intent[mismatch] = "different"
    atomic_json(tmp_path / "intent.json", intent)
    if mismatch:
        with pytest.raises(TransitionBlocked, match="configuration differs"):
            validate_preparation_invocation(result, config.file_digest, tmp_path)
    else:
        validate_preparation_invocation(result, config.file_digest, tmp_path)


def test_preparation_intent_cannot_escape_operational_root(tmp_path):
    from backlog_harness.estimation import validate_preparation_invocation

    external = tmp_path / "outside"
    atomic_json(external / "intent.json", {"config_digest": "config"})
    with pytest.raises(TransitionBlocked, match="escapes evidence root"):
        validate_preparation_invocation(
            {"evidence_path": str(external)}, "config", tmp_path / "operations"
        )


@pytest.mark.parametrize("defect", [None, "unbound", "changed", "canonical"])
def test_preparation_accepts_only_bound_external_stopped_runtime(tmp_path, defect):
    from dataclasses import replace
    from types import SimpleNamespace

    from test_recovery_evidence import _recovery_case

    from backlog_harness.estimation import validate_preparation_sources
    from backlog_harness.evidence import atomic_json

    packet, item, repository, runtime = _recovery_case(tmp_path)
    record = packet["runtime_records"][0]
    item = replace(item, content=record["path"] + "\n" + record["sha256"])
    saved = tmp_path / "stopped-owner-input.json"
    atomic_json(saved, {"supplied": {"runtime_records": packet["runtime_records"]}})
    app = SimpleNamespace(_stage_path=lambda *_: saved)
    evidence = {"path": str(runtime), "sha256": record["sha256"], "reason": "Verified recovery"}
    if defect == "unbound":
        saved.unlink()
    elif defect == "changed":
        runtime.write_text(runtime.read_text() + "{}\n")
    elif defect == "canonical":
        item = replace(item, content="Another recovery")
    if defect:
        with pytest.raises(TransitionBlocked):
            validate_preparation_sources(
                app, SimpleNamespace(repository=repository), item, [evidence]
            )
    else:
        validate_preparation_sources(app, SimpleNamespace(repository=repository), item, [evidence])


@pytest.mark.parametrize("name", ["dashboard", "portrait"])
def test_retained_preparation_metadata_preserves_every_obligation(name):
    from pathlib import Path

    from backlog_harness.estimation import normalize_preparation_workflow

    parameters = json.loads(
        (Path(__file__).parent / "fixtures/preparation" / (name + ".json")).read_text()
    )
    echoes = {
        "persistence": "file",
        "completion": "main-branch",
        "canonical_primary_branch": "main",
    }
    executable, evidence = normalize_preparation_workflow(parameters, echoes)
    assert set(executable) == {"allowed_paths", "checks"}
    assert executable | evidence | echoes == parameters
    assert evidence["required_gates"] == parameters["required_gates"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("checks_executed", True),
        ("checks_executed", 0),
        ("required_gates", "ACCEPT"),
        ("required_gates", [{"gate": "browser"}]),
        ("implementation_constraints", [False]),
        ("verification_limit", ""),
        ("extra_permission", True),
        ("completion", "skip-review"),
    ],
)
def test_preparation_metadata_invalid_fields_block(field, value):
    from backlog_harness.estimation import normalize_preparation_workflow

    with pytest.raises(TransitionBlocked, match=field):
        normalize_preparation_workflow(
            {"allowed_paths": ["answer.py"], "checks": [["git", "diff", "--check"]], field: value},
            {"completion": "main-branch"},
        )


@pytest.mark.parametrize(
    "fault,match",
    [
        (None, None),
        ("missing", "classify every original gates entry"),
        ("duplicate", "classify every original gates entry"),
        ("text", "classification differs from original gates"),
        ("obligation", "unsupported runtime obligation"),
        ("attestation", "no additional proof"),
        ("demote", "existing required_gates changed"),
        ("path", "changed preparation authority"),
        ("estimate", "changed preparation authority"),
    ],
)
def test_preparation_correction_is_representation_only(fault, match):
    from copy import deepcopy

    from backlog_harness.estimation import validate_preparation_correction

    gates = [
        "Reserve and accept the item before implementation.",
        "Obtain fresh browser proof before delivery.",
    ]
    original = {
        "item_id": "one",
        "provider_revision": "revision",
        "workflow": {
            "allowed_paths": ["answer.py"],
            "checks": [["git", "diff", "--check"]],
            "gates": gates,
            "required_gates": ["Preserve an existing explicit proof gate."],
        },
        "historical_original_high": None,
        "historical_usage": "unknown",
        "prospective_high": 1000,
        "estimate": {"kind": "prospective_pre_execution", "generated_tokens": {"high": 1000}},
        "authority_evidence": [{"path": "PROJECT.yaml", "sha256": "a" * 64, "reason": "Policy"}],
    }
    error = "Preparation contains unsupported workflow fields: gates"
    corrected = deepcopy(original)
    corrected["workflow"].pop("gates")
    corrected["workflow"]["implementation_constraints"] = [gates[0]]
    corrected["workflow"]["required_gates"] = [
        "Preserve an existing explicit proof gate.",
        gates[1],
    ]
    corrected["preparation_correction"] = {
        "original_decision_digest": "original-digest",
        "schema_error": error,
        "classifications": [
            {
                "index": 0,
                "text": gates[0],
                "destination": "implementation_constraints",
                "runtime_obligation": "reservation_acceptance",
                "no_additional_proof": True,
                "rationale": "The normal admission transitions already enforce this obligation.",
            },
            {
                "index": 1,
                "text": gates[1],
                "destination": "required_gates",
                "runtime_obligation": None,
                "no_additional_proof": False,
                "rationale": "The configured workflow has no browser-proof selection.",
            },
        ],
    }
    if fault == "missing":
        corrected["preparation_correction"]["classifications"].pop()
    elif fault == "duplicate":
        corrected["preparation_correction"]["classifications"][1]["index"] = 0
    elif fault == "text":
        corrected["preparation_correction"]["classifications"][0]["text"] = "Changed"
    elif fault == "obligation":
        corrected["preparation_correction"]["classifications"][0]["runtime_obligation"] = (
            "browser_proof"
        )
    elif fault == "attestation":
        corrected["preparation_correction"]["classifications"][0]["no_additional_proof"] = False
    elif fault == "demote":
        corrected["workflow"]["required_gates"].pop(0)
    elif fault == "path":
        corrected["workflow"]["allowed_paths"] = ["other.py"]
    elif fault == "estimate":
        corrected["prospective_high"] = 2000
    if fault:
        with pytest.raises(TransitionBlocked, match=match):
            validate_preparation_correction(
                original,
                corrected,
                "original-digest",
                error,
                {"reservation_acceptance"},
            )
    else:
        validate_preparation_correction(
            original,
            corrected,
            "original-digest",
            error,
            {"reservation_acceptance"},
        )


@pytest.mark.parametrize("correction_fault", [None, "unsupported-obligation"])
@pytest.mark.parametrize("coordinator_permission", ["read", "workspace-write"])
def test_prepare_item_retains_and_replays_one_linked_contract_correction(
    config_file, monkeypatch, correction_fault, coordinator_permission
):
    import asyncio
    from copy import deepcopy
    from hashlib import sha256

    from backlog_harness.estimation import prepare_item

    config, data = config_file
    repository = config.parent / "project"
    (repository / "PROJECT.yaml").write_text("workflow_selection: {}\n")
    (repository / "answer.py").write_text("answer = 1\n")
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": data["workflow"]["checks"],
    }
    data["profiles"]["control"]["permissions"] = [coordinator_permission]
    if coordinator_permission == "workspace-write":
        data["provider_interaction"] = "agent"
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = Item("one", "one.md", "revision", "Ready", None, 100, "Canonical requirements")
    error = "Preparation contains unsupported workflow fields: gates"
    gate = "Reserve and accept the item before implementation."
    original_value = {
        "item_id": item.item_id,
        "provider_revision": item.revision,
        "workflow": {
            "allowed_paths": ["answer.py"],
            "checks": data["workflow"]["checks"],
            "gates": [gate],
        },
        "historical_original_high": None,
        "historical_usage": "unknown",
        "prospective_high": 1000,
        "estimate": {"kind": "prospective_pre_execution", "generated_tokens": {"high": 1000}},
        "authority_evidence": [
            {
                "path": "PROJECT.yaml",
                "sha256": sha256((repository / "PROJECT.yaml").read_bytes()).hexdigest(),
                "reason": "Project policy",
            }
        ],
    }
    calls = []

    def result(stage, value):
        path = app.root / (stage + "-native")
        envelope = {
            "role": "coordinator",
            "invocation_id": stage,
            "request_digest": stage + "-request",
            "binding": asdict(app._provider_invocation_snapshot(app.config).binding("coordinator")),
            "evidence_path": str(path),
            "outcome": "returned",
            "text": json.dumps(value),
        }
        atomic_json(
            path / "intent.json",
            {
                "invocation_id": envelope["invocation_id"],
                "request_digest": envelope["request_digest"],
                "binding": envelope["binding"],
                "config_digest": app.config.file_digest,
            },
        )
        return envelope

    original = result("prepare", original_value)
    corrected_value = deepcopy(original_value)
    corrected_value["workflow"].pop("gates")
    corrected_value["workflow"]["implementation_constraints"] = [gate]
    corrected_value["preparation_correction"] = {
        "original_decision_digest": digest(original),
        "schema_error": error,
        "classifications": [
            {
                "index": 0,
                "text": gate,
                "destination": "implementation_constraints",
                "runtime_obligation": "reservation_acceptance",
                "no_additional_proof": True,
                "rationale": "The normal admission transitions already enforce this obligation.",
            }
        ],
    }
    if correction_fault == "unsupported-obligation":
        corrected_value["preparation_correction"]["classifications"][0]["runtime_obligation"] = (
            "browser_proof"
        )
    corrected = result("preparation-contract-correction", corrected_value)

    async def invoke(item_id, stage, role, prompt, **kwargs):
        assert item_id == item.item_id
        assert role == "coordinator"
        assert kwargs["purpose"] == "provider"
        if stage == "prepare":
            calls.append(stage)
            return original
        assert stage == "preparation-contract-correction"
        assert kwargs["read_only"] is True
        assert error in prompt
        assert item.content in prompt
        assert json.dumps(original, sort_keys=True) in prompt
        retained = app._stage_path(item_id, stage)
        if retained.exists():
            return json.loads(retained.read_text())
        calls.append(stage)
        atomic_json(retained, corrected, exclusive=True)
        return corrected

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    if correction_fault:
        for _ in range(2):
            with pytest.raises(TransitionBlocked, match="unsupported runtime obligation"):
                asyncio.run(prepare_item(app, item))
        assert calls == ["prepare", "preparation-contract-correction"]
        assert not app._stage_path(item.item_id, "preparation-contract-resolution").exists()
        assert not app._stage_path(item.item_id, "estimate-input").exists()
        return
    assert asyncio.run(prepare_item(app, item)) == item
    preparation = app._stage_path(item.item_id, "preparation")
    original_bytes = preparation.read_bytes()
    resolution = json.loads(
        app._stage_path(item.item_id, "preparation-contract-resolution").read_text()
    )
    assert resolution["original_decision_digest"] == digest(original)
    assert resolution["corrected_decision_digest"] == digest(corrected)
    workflow = app.item_workflow(item.item_id)
    assert workflow["preparation_evidence"]["metadata"]["implementation_constraints"] == [gate]
    assert workflow["preparation_evidence"]["original_decision_digest"] == digest(original)
    assert asyncio.run(prepare_item(app, item)) == item
    assert calls == ["prepare", "preparation-contract-correction"]
    assert preparation.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "refusal",
        "path",
        "check",
        "missing-authority",
        "stale-authority",
        "tampered-authority",
        "config",
        "binding",
    ],
)
def test_preparation_authority_failures_do_not_invoke_contract_correction(
    config_file, monkeypatch, fault
):
    import asyncio
    from hashlib import sha256

    from backlog_harness.estimation import configured_workflow, prepare_item

    config, data = config_file
    repository = config.parent / "project"
    (repository / "PROJECT.yaml").write_text("workflow_selection: {}\n")
    (repository / "answer.py").write_text("answer = 1\n")
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": data["workflow"]["checks"],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = Item("one", "one.md", "revision", "Ready", None, 100, "Canonical requirements")
    value = {
        "item_id": "other" if fault == "identity" else item.item_id,
        "provider_revision": item.revision,
        "workflow": {
            "allowed_paths": ["other.py"] if fault == "path" else ["answer.py"],
            "checks": [["false"]] if fault == "check" else data["workflow"]["checks"],
            "gates": ["Reserve and accept the item before implementation."],
        },
        "authority_evidence": [
            {
                "path": "PROJECT.yaml",
                "sha256": sha256((repository / "PROJECT.yaml").read_bytes()).hexdigest(),
                "reason": "Project policy",
            }
        ],
    }
    if fault == "refusal":
        value["blocked"] = "Canonical authority is unresolved"
    elif fault == "missing-authority":
        value.pop("authority_evidence")
    elif fault == "stale-authority":
        value["authority_evidence"][0]["sha256"] = "0" * 64
    elif fault == "tampered-authority":
        value["authority_evidence"][0]["path"] = "missing-policy.md"
    binding = asdict(app._provider_invocation_snapshot(app.config).binding("coordinator"))
    if fault == "binding":
        binding = {**binding, "permission_digest": "changed"}
    decision = {
        "role": "coordinator",
        "invocation_id": "prepare",
        "request_digest": "prepare-request",
        "binding": binding,
        "evidence_path": str(app.root / "prepare-native"),
        "outcome": "returned",
        "text": json.dumps(value),
    }
    atomic_json(
        app.root / "prepare-native/intent.json",
        {
            "invocation_id": decision["invocation_id"],
            "request_digest": decision["request_digest"],
            "binding": decision["binding"],
            "config_digest": app.config.file_digest,
        },
    )
    if fault == "config":
        data["poll_seconds"] = 2
        config.write_text(yaml.safe_dump(data))
    atomic_json(
        app._stage_path(item.item_id, "preparation"),
        {
            "item": asdict(item),
            "decision": decision,
            "workflow_config_digest": digest(configured_workflow(app.config, item.item_id)),
            "invocation_config_digest": app.config.file_digest,
        },
    )

    async def no_invocation(*_args, **_kwargs):
        raise AssertionError("authority failures must not invoke correction")

    monkeypatch.setattr(app, "invoke", no_invocation)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    with pytest.raises(TransitionBlocked):
        asyncio.run(prepare_item(app, item))
    assert not app._stage_path(item.item_id, "preparation-contract-correction").exists()
    assert not app._stage_path(item.item_id, "estimate-input").exists()


@pytest.mark.parametrize("fault", [None, "revision", "decision", "old_checks", "old_scope"])
@pytest.mark.parametrize("selection", ["proof", "design", "both"])
def test_explicit_selection_preserves_frozen_preparation(
    config_file, monkeypatch, fault, selection
):
    from backlog_harness.contracts import load_config
    from backlog_harness.estimation import configured_workflow, prepared_workflow

    config, data = config_file
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": [["git", "diff", "--check"]],
    }
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(
        "backlog_harness.estimation.validate_preparation_invocation", lambda *a: None
    )
    decision = {
        "role": "coordinator",
        "text": json.dumps(
            {
                "item_id": "one",
                "provider_revision": "revision",
                "workflow": {
                    "allowed_paths": ["answer.py"],
                    "checks": [["git", "diff", "--check"]],
                    "required_gates": ["Independently review the selected acceptance evidence"],
                },
            }
        ),
    }
    path = app._stage_path("one", "preparation")
    atomic_json(
        path,
        {
            "item": {"revision": "revision"},
            "decision": decision,
            "workflow_config_digest": digest(configured_workflow(app.config, "one")),
            "invocation_config_digest": app.config.file_digest,
        },
    )
    original = path.read_bytes()
    contract = {
        "provider_revision": "revision",
        "preparation_digest": digest(decision),
        "requirements": [
            {
                "id": "browser",
                "canonical_reference": "item-one#acceptance",
                "evidence_kind": "browser",
                "acceptance_text": "Readable candidate",
            }
        ],
    }
    design = {
        "provider_revision": "revision",
        "preparation_digest": digest(decision),
        "canonical_reference": "item-one#design",
        "acceptance_text": "Review proposed treatment before source production",
    }
    selected = {"allowed_paths": data["workflow"]["allowed_paths"]}
    if selection in {"proof", "both"}:
        selected["proof_requirements"] = contract
    if selection in {"design", "both"}:
        selected["design_review"] = design
    fault_contract = design if selection == "design" else contract
    if fault == "revision":
        fault_contract["provider_revision"] = "other"
    elif fault == "decision":
        fault_contract["preparation_digest"] = "0" * 64
    elif fault == "old_checks":
        selected["checks"] = [["git", "status"]]
    elif fault == "old_scope":
        selected["allowed_paths"] = ["unrelated.py"]
    data["workflow"]["items"] = {"one": selected}
    config.write_text(yaml.safe_dump(data))
    current = load_config(config)
    if fault:
        with pytest.raises(TransitionBlocked):
            prepared_workflow(app, "one", current)
    else:
        first = prepared_workflow(app, "one", current)
        assert prepared_workflow(app, "one", current) == first
        if selection in {"proof", "both"}:
            assert first["proof_requirements"] == contract
        if selection in {"design", "both"}:
            assert first["design_review"] == design
        assert first["preparation_evidence"]["decision_digest"] == digest(decision)
        assert first["preparation_evidence"]["metadata"]["required_gates"] == [
            "Independently review the selected acceptance evidence"
        ]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "fault", [None, "revision", "effective", "original", "resolution", "gate", "uncovered"]
)
def test_source_review_selection_binds_corrected_preparation_lineage(
    config_file, monkeypatch, fault
):
    from copy import deepcopy

    from backlog_harness.contracts import load_config
    from backlog_harness.estimation import configured_workflow, prepared_workflow

    config, data = config_file
    data["workflow"]["preparation"] = {
        "allowed_roots": ["answer.py"],
        "check_commands": [["git", "diff", "--check"]],
    }
    data["workflow"]["checks"] = [["git", "diff", "--check"]]
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(
        "backlog_harness.estimation.validate_preparation_invocation", lambda *a: None
    )
    gate = "Verify role-suite agreement without weakening evaluation"
    original_value = {
        "item_id": "one",
        "provider_revision": "revision",
        "workflow": {
            "allowed_paths": ["answer.py"],
            "checks": [["git", "diff", "--check"]],
            "gates": [gate],
        },
    }
    original_decision = {
        "role": "coordinator",
        "invocation_id": "original",
        "binding": {},
        "text": json.dumps(original_value),
    }
    corrected_value = deepcopy(original_value)
    corrected_value["workflow"].pop("gates")
    corrected_value["workflow"]["required_gates"] = [gate]
    corrected_value["preparation_correction"] = {
        "original_decision_digest": digest(original_decision),
        "schema_error": "Preparation contains unsupported workflow fields: gates",
        "classifications": [
            {
                "index": 0,
                "text": gate,
                "destination": "required_gates",
                "runtime_obligation": None,
                "no_additional_proof": False,
                "rationale": "Passing commands alone does not prove preserved evaluation strength.",
            }
        ],
    }
    corrected_decision = {
        "role": "coordinator",
        "invocation_id": "corrected",
        "binding": {},
        "text": json.dumps(corrected_value),
    }
    atomic_json(
        app._stage_path("one", "preparation"),
        {
            "item": {"revision": "revision"},
            "decision": original_decision,
            "workflow_config_digest": digest(configured_workflow(app.config, "one")),
            "invocation_config_digest": app.config.file_digest,
        },
    )
    atomic_json(app._stage_path("one", "preparation-contract-correction"), corrected_decision)
    resolution = {
        "original_decision_digest": digest(original_decision),
        "corrected_decision_digest": digest(corrected_decision),
        "original_invocation_id": "original",
        "corrected_invocation_id": "corrected",
        "schema_error": "Preparation contains unsupported workflow fields: gates",
    }
    atomic_json(app._stage_path("one", "preparation-contract-resolution"), resolution)
    contract = {
        "provider_revision": "revision",
        "preparation_digest": digest(corrected_decision),
        "original_preparation_digest": digest(original_decision),
        "correction_resolution_digest": digest(resolution),
        "requirements": [
            {
                "id": "role-suite",
                "canonical_reference": "item-one#acceptance",
                "acceptance_text": "Role, suite, and generated adapters agree without weakening",
                "required_gate": gate,
            }
        ],
    }
    if fault == "revision":
        contract["provider_revision"] = "other"
    elif fault == "effective":
        contract["preparation_digest"] = "0" * 64
    elif fault == "original":
        contract["original_preparation_digest"] = "0" * 64
    elif fault == "resolution":
        contract["correction_resolution_digest"] = "0" * 64
    elif fault == "gate":
        contract["requirements"][0]["required_gate"] = "Different obligation"
    elif fault == "uncovered":
        corrected_value["workflow"]["required_gates"].append("Unknown extra proof")
        corrected_decision["text"] = json.dumps(corrected_value)
        atomic_json(app._stage_path("one", "preparation-contract-correction"), corrected_decision)
        resolution["corrected_decision_digest"] = digest(corrected_decision)
        atomic_json(app._stage_path("one", "preparation-contract-resolution"), resolution)
        contract["preparation_digest"] = digest(corrected_decision)
        contract["correction_resolution_digest"] = digest(resolution)
    data["workflow"]["items"] = {
        "one": {"allowed_paths": ["answer.py"], "review_requirements": contract}
    }
    config.write_text(yaml.safe_dump(data))
    current = load_config(config)
    if fault:
        with pytest.raises(TransitionBlocked):
            prepared_workflow(app, "one", current)
    else:
        workflow = prepared_workflow(app, "one", current)
        assert workflow["review_requirements"] == contract
        assert workflow["preparation_evidence"] == {
            "item_id": "one",
            "provider_revision": "revision",
            "decision_digest": digest(corrected_decision),
            "original_decision_digest": digest(original_decision),
            "correction_resolution_digest": digest(resolution),
            "metadata": {"required_gates": [gate]},
        }
