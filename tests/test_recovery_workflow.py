"""Evidence gates for the small external-owner recovery route."""

import json
from dataclasses import asdict, replace

import pytest

from backlog_harness.application import Application
from backlog_harness.evidence import atomic_json
from backlog_harness.provider import AgentProvider, TransitionBlocked, git
from backlog_harness.workflow import validate_candidate, validate_transition


def test_recovery_requires_owner_and_evidence(provider):
    item = replace(provider.item("item-one"), state="Running", owner="desktop-owner")
    authority = {
        "role": "coordinator",
        "invocation_id": "observed",
        "observed_result": True,
        "item_id": item.item_id,
        "operation": "redispatch",
        "recovery": {
            "previous_owner": item.owner,
            "ownership_ended": True,
            "packet_digest": "hash",
            "candidate": "sha",
            "runtime_evidence": ["proof"],
            "reason": "Owner ended; candidate preserved",
        },
    }
    validate_transition(item, "Ready", authority)
    for field in [
        "previous_owner",
        "ownership_ended",
        "packet_digest",
        "candidate",
        "runtime_evidence",
        "reason",
    ]:
        bad = {**authority, "recovery": {**authority["recovery"], field: None}}
        with pytest.raises(TransitionBlocked):
            validate_transition(item, "Ready", bad)
    with pytest.raises(TransitionBlocked):
        validate_transition(item, "Ready", {**authority, "role": "orchestrator"})


def test_recovery_authority_does_not_open_other_admission(config_file, provider, monkeypatch):
    app = Application(config_file[0])
    app.provider = AgentProvider(provider.repository, provider.evidence_root)
    policy = {"eligible": False}
    monkeypatch.setattr(app.provider, "observation", lambda: {"policy": policy})
    monkeypatch.setattr(app.provider, "validate_policy", lambda _: None)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    atomic_json(
        app._stage_path("item-one", "decision"),
        {
            "invocation_id": "observed",
            "text": json.dumps(
                {"item_id": "item-one", "continuation_authorized": True, "operation": "redispatch"}
            ),
        },
    )
    atomic_json(
        app._stage_path("item-one", "recovery"),
        {"decision_stage": "decision", "decision_invocation": "observed", "policy_sources": {}},
    )
    assert app.execution_policy("item-one") is policy
    with pytest.raises(TransitionBlocked, match="item-specific"):
        app.execution_policy("other")
    assert policy["eligible"] is False


def test_preserved_candidate_checks_entire_ancestry(provider):
    repo = provider.repository
    base = git(repo, "rev-parse", "HEAD")
    for path in ["page.html", "check.py"]:
        (repo / path).write_text(path)
        git(repo, "add", "--", path)
        git(repo, "commit", "-m", path)
    head = git(repo, "rev-parse", "HEAD")
    review = {
        "candidate": head,
        "verdict": "ACCEPT",
        "reviewer_session": "reviewer",
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": "hash",
        "unresolved_findings": [],
    }
    checks = [{"candidate": head, "returncode": 0, "argv": ["check"], "evidence_sha256": "hash"}]
    assert set(
        validate_candidate(
            repo, head, base, ["page.html", "check.py"], "producer", review, checks, preserved=True
        )
    ) == {"page.html", "check.py"}
    with pytest.raises(TransitionBlocked, match="scope"):
        validate_candidate(
            repo, head, base, ["check.py"], "producer", review, checks, preserved=True
        )


def test_projection_rejects_unrelated_provider_changes(config_file, provider):
    app = Application(config_file[0])
    app.provider = AgentProvider(provider.repository, provider.evidence_root)
    item = provider.item("item-one")
    manifest = app.provider.source_manifest()
    (provider.repository / "PROJECT.yaml").write_text("changed")
    with pytest.raises(TransitionBlocked, match="Unrelated"):
        app.advance_provider_projection(
            {"items": [asdict(item)]}, manifest, {"after": asdict(item)}, [item.path]
        )


def test_recovery_budget_keeps_history_unknown(config_file, provider, monkeypatch):
    app = Application(config_file[0])
    monkeypatch.setattr(app.provider, "item", provider.item)
    atomic_json(app._stage_path("item-one", "assignment"), {"original_high": 200})
    atomic_json(app._stage_path("item-one", "recovery"), {"historical_original_high": None})
    view = app.usage_view("item-one")
    assert view["historical_usage"] == "unknown"
    assert view["accounting_scope"] == "recovery_remaining_work"
    assert view["generated_tokens"] == 0 and view["ceiling"] == 400
    atomic_json(
        app._stage_path("item-one", "produce"),
        {"role": "orchestrator", "events": [], "telemetry": {}, "outcome": "unresolved"},
    )
    view = app.usage_view("item-one")
    assert view["status"] == "unknown" and view["may_generate"] is False


def test_recovery_replays_committed_handoff_without_second_decision(
    config_file, tmp_path, monkeypatch
):
    import asyncio
    import shutil

    import yaml
    from test_recovery_evidence import _recovery_case

    from backlog_harness.evidence import component
    from backlog_harness.recovery_flow import recover_item

    case_root = tmp_path / "case"
    case_root.mkdir()
    packet, original, repository, _ = _recovery_case(case_root)
    config, data = config_file
    data["provider_interaction"] = "agent"
    data["candidate_root"] = str(tmp_path / "imports")
    Path = type(config)
    Path(data["candidate_root"]).mkdir()
    destination = Path(data["candidate_root"]) / component(original.item_id)
    shutil.copytree(repository, destination)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    current = [original]
    calls = {"decision": 0, "mutation": 0, "transition": 0, "run": 0}
    observation = {"policy": {"evidence": []}, "dependencies": {}}
    monkeypatch.setattr(app.provider, "item", lambda _: current[0])
    monkeypatch.setattr(app.provider, "observation", lambda: observation)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)

    async def refresh():
        return None

    async def invoke(*args, **kwargs):
        calls["decision"] += 1
        decision = {
            "role": "coordinator",
            "invocation_id": "decision",
            "outcome": "returned",
            "session": {"session_id": "portable", "native_session_id": "native"},
            "text": json.dumps(
                {
                    "operation": "redispatch",
                    "item_id": original.item_id,
                    "provider_revision": original.revision,
                    "previous_owner": original.owner,
                    "ownership_ended": True,
                    "continuation_authorized": True,
                    "reason": "Ended execution",
                    "remaining_high": 25,
                    "scope": packet["scope"],
                    "dependencies": [],
                }
            ),
        }
        atomic_json(app._stage_path(args[0], args[1]), decision)
        return decision

    async def transition(*args, **kwargs):
        calls["transition"] += 1
        if current[0].state == "Running":
            calls["mutation"] += 1
            current[0] = replace(
                original, state="Ready", owner="Unowned", revision="ready-revision"
            )
            raise RuntimeError("crash after committed effect")
        return {"state": "Ready"}

    async def run(item_id):
        calls["run"] += 1
        assignment = json.loads(app._stage_path(item_id, "assignment").read_text())
        assert assignment["provider_revision"] == "ready-revision"
        assert assignment["original_high"] == 25
        assert assignment["historical_original_high"] == original.original_high
        assert git(destination, "rev-parse", "HEAD") == packet["candidate"]["head"]
        return {"state": "Running"}

    monkeypatch.setattr(app, "refresh_provider", refresh)
    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "transition", transition)
    monkeypatch.setattr(app, "run_item", run)
    evidence = tmp_path / "packet.json"
    evidence.write_text(
        json.dumps({"runtime_records": packet["runtime_records"], "candidate": packet["candidate"]})
    )
    with pytest.raises(RuntimeError, match="crash after"):
        asyncio.run(recover_item(app, original.item_id, evidence))
    assert asyncio.run(recover_item(app, original.item_id, evidence)) == {"state": "Running"}
    assert calls == {"decision": 1, "mutation": 1, "transition": 2, "run": 1}


def test_recovered_ready_item_uses_continuation_reservation(config_file, provider, monkeypatch):
    import asyncio

    import yaml

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    recovery = {
        "workflow": data["workflow"],
        "config_workflow": data["workflow"],
        "decision_invocation": "recovery-coordinator",
        "historical_original_high": 100,
        "reason": "Ownership ended; same reserved item",
        "packet": {},
    }
    atomic_json(app._stage_path(item.item_id, "recovery"), recovery)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)

    async def invoke(item_id, stage, role, prompt, **kwargs):
        if stage == "accept":
            assert app.provider.item(item_id).state == "Starting"
            raise RuntimeError("reservation verified; stop before native acceptance")
        assert stage == "admit"
        assert "SAME recovered item" in prompt
        assert "recovery-coordinator" in prompt
        assert "global admission remains closed" in prompt
        result = {
            "role": role,
            "invocation_id": "reservation",
            "outcome": "returned",
            "session": {"session_id": "new-coordinator", "native_session_id": "new-native"},
            "text": json.dumps(
                {
                    "operation": "new",
                    "item_id": item_id,
                    "provider_revision": item.revision,
                    "reason": "Recovery reservation",
                }
            ),
        }
        atomic_json(app._stage_path(item_id, stage), result)
        return result

    monkeypatch.setattr(app, "invoke", invoke)
    with pytest.raises(RuntimeError, match="reservation verified"):
        asyncio.run(app._run_item(item.item_id))
    assert app.provider.item(item.item_id).state == "Starting"
