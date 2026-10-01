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


def test_incomplete_owner_release_is_corrected_once(config_file, provider, monkeypatch):
    import asyncio

    import yaml

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    path = provider.repository / provider.item("item-one").path
    path.write_text(
        path.read_text()
        .replace("Status: Ready", "Status: Running")
        .replace("Owner: Unowned", "Owner: old-owner")
    )
    git(provider.repository, "add", "--", str(path))
    git(provider.repository, "commit", "-m", "Existing running owner")
    original = provider.item("item-one")
    app = Application(config)
    decision = {
        "role": "coordinator",
        "invocation_id": "decision",
        "outcome": "returned",
        "session": {"session_id": "coordinator", "native_session_id": "native"},
    }
    atomic_json(app._stage_path(original.item_id, "decision"), decision)
    authority = app.authority(
        decision,
        original.item_id,
        operation="redispatch",
        recovery={
            "previous_owner": original.owner,
            "ownership_ended": True,
            "packet_digest": "hash",
            "candidate": "candidate",
            "runtime_evidence": ["proof"],
            "reason": "Ended",
        },
    )
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    writes = []

    async def invoke(item_id, stage, role, prompt, **kwargs):
        saved = app._stage_path(item_id, stage)
        if saved.exists():
            return json.loads(saved.read_text())
        before = provider.item(item_id)
        text = path.read_text().replace("Status: Running", "Status: Ready")
        if writes:
            assert '"target_owner": "Unowned"' in prompt
            text = text.replace("Owner: old-owner", "Owner: Unowned")
        path.write_text(text)
        git(provider.repository, "add", "--", str(path))
        git(provider.repository, "commit", "-m", "Owner effect " + str(len(writes)))
        writes.append(git(provider.repository, "rev-parse", "HEAD"))
        after = asdict(provider.item(item_id))
        after.pop("content")
        after.pop("revision")
        result = {
            "invocation_id": "effect-" + str(len(writes)),
            "session": decision["session"],
            "evidence_path": "fixture",
            "text": json.dumps(
                {
                    "operation_id": item_id + ":" + stage,
                    "before_revision": before.revision,
                    "commit": writes[-1],
                    "after": after,
                }
            ),
        }
        atomic_json(saved, result)
        return result

    monkeypatch.setattr(app, "invoke", invoke)

    async def effect():
        return await app.invoke_provider_transition(original, "Ready", authority, [original.path])

    receipt = asyncio.run(effect())
    assert receipt["after"]["owner"] == "Unowned"
    assert asyncio.run(effect())["commit"] == receipt["commit"]
    assert len(writes) == 2
    incomplete = list((app.root / "provider-agent-operations").glob("*/incomplete-effect.json"))
    assert len(incomplete) == 1
    assert json.loads(incomplete[0].read_text())["advancement_verified"] is False
    reconciled = app.reconcile_agent_provider(incomplete[0].parent / "requested.json")
    assert reconciled["advancement_verified"] is True
    assert reconciled["correction"]["after"]["owner"] == "Unowned"


@pytest.mark.parametrize("bad_path", [None, "destination", "series", "omitted_series"])
@pytest.mark.parametrize("owner", ["canonical", "another"])
def test_question_handoff_requires_canonical_declined_result(
    config_file, provider, monkeypatch, tmp_path, owner, bad_path
):
    import asyncio

    import yaml

    from backlog_harness.recovery_flow import defer_item

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = replace(provider.item("item-one"), state="Running", owner="canonical")
    if bad_path == "omitted_series":
        item = replace(item, content=item.content + "\nSeries: backlog/feature-backlog/index.md\n")
    monkeypatch.setattr(app.provider, "item", lambda _: item)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    result = {
        "invocation_id": "declined",
        "session": {"session_id": owner},
        "text": json.dumps(
            {
                "item_id": item.item_id,
                "request_completion": False,
                "blockers": ["Browser permission denied"],
            }
        ),
    }
    atomic_json(app._stage_path(item.item_id, "produce-review"), result)
    question = {"question_id": "q1", "text": "Authorize local browser verification?"}
    paths = [item.path, "backlog/user-action-required/item-one.md"]
    if bad_path == "destination":
        paths[1] = "backlog/user-action-required/unrelated.md"
    elif bad_path == "series":
        paths.append("backlog/unrelated/index.md")
    supplied = {"question": question, "paths": paths}
    question_file = tmp_path / "question.json"
    question_file.write_text(json.dumps(supplied))
    calls = []

    async def invoke(*args, **kwargs):
        calls.append("decision")
        assert "ALREADY PENDING" in args[3]
        return {
            "role": "coordinator",
            "outcome": "returned",
            "invocation_id": "coordinator-decision",
            "session": {"session_id": "coordinator", "native_session_id": "native"},
            "text": json.dumps(
                {
                    "operation": "await-user",
                    "item_id": item.item_id,
                    "provider_revision": item.revision,
                    "question": question,
                    "paths": paths,
                    "reason": "User owns browser permission",
                }
            ),
        }

    async def transition(item_id, revision, target, authority, **kwargs):
        calls.append("transition")
        validate_transition(item, target, authority)
        assert authority["outcome_evidence"] == "declined"
        assert kwargs["declared_paths"] == paths
        return {"state": target}

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "transition", transition)
    if owner != "canonical":
        with pytest.raises(TransitionBlocked, match="declined completion evidence"):
            asyncio.run(defer_item(app, item.item_id, question_file))
        assert calls == []
    elif bad_path:
        with pytest.raises(TransitionBlocked, match="paths are invalid|series membership"):
            asyncio.run(defer_item(app, item.item_id, question_file))
        assert calls == []
    else:
        assert (
            asyncio.run(defer_item(app, item.item_id, question_file))["state"]
            == "User Action Required"
        )
        assert calls == ["decision", "transition"]


@pytest.mark.parametrize("question_case", ["valid", "missing", "mismatch", "malformed"])
def test_question_move_receipt_and_projection_include_series_link(
    config_file, provider, question_case
):
    import yaml
    from test_provider_coordination import policy_evidence

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    index = provider.repository / "backlog/feature-backlog/index.md"
    index.write_text("[One](item-one.md)\n")
    git(provider.repository, "add", "--", str(index))
    git(provider.repository, "commit", "-m", "Series")
    head = git(provider.repository, "rev-parse", "HEAD")
    manifest = app.provider.source_manifest()
    destination = "backlog/user-action-required/item-one.md"
    paths = [item.path, destination, str(index.relative_to(provider.repository))]
    policy = {
        "eligible": False,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": policy_evidence(provider.repository),
    }
    before = {
        "items": [asdict(item)],
        "dependencies": {item.item_id: []},
        "policy": policy,
        "non_items": [{"path": paths[2], "kind": "index", "reason": "Series index"}],
    }
    question = {"question_id": "q1", "text": "Authorize browser verification?"}
    target = provider.repository / destination
    target.parent.mkdir()
    target.write_text(
        (provider.repository / item.path)
        .read_text()
        .replace("Status: Ready", "Status: User Action Required")
        + (
            "\nq1\nAuthorize browser verification?\n"
            if question_case == "valid"
            else "\nOther question\n"
        )
    )
    (provider.repository / item.path).unlink()
    index.write_text("[One](../user-action-required/item-one.md)\n")
    git(provider.repository, "add", "--", *paths)
    git(provider.repository, "commit", "-m", "Question queue")
    after = provider.item(item.item_id)
    record = {
        "item": asdict(item),
        "target": "User Action Required",
        "authority": {"question": question if question_case != "malformed" else {}},
        "head": head,
        "paths": paths,
        "expected_path": destination,
        "stage_operation": "question-op",
    }
    value = {
        "operation_id": "question-op",
        "before_revision": item.revision,
        "commit": git(provider.repository, "rev-parse", "HEAD"),
        "after": asdict(after),
    }
    if question_case != "missing":
        value["question"] = question if question_case != "malformed" else {}
    if question_case != "valid":
        with pytest.raises(TransitionBlocked, match="Committed provider question"):
            app.verify_provider_receipt(record, value)
        return
    receipt = app.verify_provider_receipt(record, value)
    app.advance_provider_projection(before, manifest, receipt, paths)
    observed = app.provider.observation()
    assert observed["items"][0]["path"] == destination
    assert observed["questions"][item.item_id]["question_id"] == "q1"
    assert observed["policy"]["eligible"] is False
