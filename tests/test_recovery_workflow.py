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


@pytest.mark.parametrize("no_candidate", [False, True])
def test_incomplete_owner_release_is_corrected_once(
    config_file, provider, monkeypatch, no_candidate
):
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
    decision_checks = []
    if no_candidate:
        authority["recovery"].pop("candidate")
        authority["recovery"]["no_source_changes"] = True

        def validate_original(item, request):
            assert item == original
            assert not request["recovery"].get("repair_effect")
            decision_checks.append(item.revision)

        monkeypatch.setattr(app, "validate_stopped_owner_decision", validate_original)
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


@pytest.mark.parametrize("state", ["Running", "User Action Required"])
@pytest.mark.parametrize("bad_path", [None, "destination", "series", "omitted_series"])
@pytest.mark.parametrize("owner", ["canonical", "another"])
def test_question_handoff_requires_canonical_declined_result(
    config_file, provider, monkeypatch, tmp_path, owner, bad_path, state
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
    item = replace(provider.item("item-one"), state=state, owner="canonical")
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
    monkeypatch.setattr(
        app.provider, "question", lambda _: {**question, "item_id": item.item_id, "answer": None}
    )
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
        assert "do not ask it again" in args[3]
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
            "\n\n## Pending Preparation Question\n\nQuestion ID: q1\n\nAuthorize browser verification?\n"
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


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "wrong_owner",
        "historical_owner",
        "trimmed_section",
        "resumed",
        "candidate",
        "decision_mismatch",
    ],
)
def test_no_candidate_reconciliation_checks_native_stop_and_decision(
    config_file, provider, tmp_path, monkeypatch, case
):
    from hashlib import sha256
    from uuid import uuid4

    from backlog_harness.contracts import digest

    app = Application(config_file[0])
    session, turn = str(uuid4()), str(uuid4())
    item = replace(
        provider.item("item-one"),
        state="Running",
        owner="old-owner",
        content="## Running Acceptance Evidence\n\nOwner: old-owner\nCanonical Conversation: `"
        + session
        + "`\n",
    )
    if case == "historical_owner":
        item = replace(
            item,
            content=item.content.replace(session, str(uuid4()))
            + "\n## History\nPrior session: "
            + session,
        )
    section = item.content.split("## Running Acceptance Evidence\n", 1)[1].split("\n## ", 1)[0]
    owner_binding = {
        "section_sha256": sha256(
            (section.strip() if case == "trimmed_section" else section).encode()
        ).hexdigest()
    }
    rows = [
        {"type": "session_meta", "payload": {"id": session}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
    ]
    if case == "resumed":
        rows.append(
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(uuid4())}}
        )
    path = tmp_path / "runtime.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    runtime = [
        {
            "path": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "native_session_id": session,
            "final_turn_id": turn,
        }
    ]
    decision = {
        "invocation_id": "decision",
        "text": json.dumps(
            {
                "item_id": item.item_id,
                "provider_revision": item.revision,
                "previous_owner": item.owner,
                "ownership_ended": True,
                "no_source_changes": True,
                "operation": "redispatch",
                "runtime_digest": digest(runtime) if case != "decision_mismatch" else "wrong",
                "owner_binding_digest": digest(owner_binding),
            }
        ),
    }
    atomic_json(app._stage_path(item.item_id, "stopped-decision"), decision)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    authority = {
        "role": "coordinator",
        "invocation_id": "decision",
        "observed_result": True,
        "operation": "redispatch",
        "item_id": item.item_id,
        "recovery": {
            "previous_owner": item.owner,
            "ownership_ended": True,
            "no_source_changes": True,
            "packet_digest": "packet",
            "runtime_evidence": runtime,
            "reason": "Stopped without source changes",
            "decision_stage": "stopped-decision",
            "owner_binding": owner_binding,
        },
    }
    if case == "wrong_owner":
        item = replace(item, content="Another canonical execution")
    if case == "candidate":
        authority["recovery"]["candidate"] = "unexpected-candidate"
    if case == "valid":
        validate_transition(item, "Ready", authority)
        app.validate_stopped_owner_decision(item, authority)
    else:
        with pytest.raises(TransitionBlocked):
            app.validate_stopped_owner_decision(item, authority)


def test_provider_revision_and_content_hash_are_distinct(provider):
    from hashlib import sha256

    item = provider.item("item-one")
    content = (provider.repository / item.path).read_bytes()
    assert item.revision == sha256(item.path.encode() + b"\0" + content).hexdigest()
    assert item.revision != sha256(content).hexdigest()


def test_question_routes_require_exact_series_evidence(provider):
    from backlog_harness.recovery_flow import question_handoff_paths

    source = "backlog/feature-backlog/series/item-one.md"
    series = "backlog/feature-backlog/series/index.md"
    item = replace(provider.item("item-one"), path=source, content="Series: " + series)
    index = provider.repository / series
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("[Item](item-one.md)")
    expected = [source, "backlog/user-action-required/item-one.md", series]
    assert question_handoff_paths(provider.repository, item) == expected
    assert question_handoff_paths(provider.repository, item, expected) == expected
    for invalid in ([source], expected[:2], [source, "elsewhere.md", series]):
        with pytest.raises(TransitionBlocked):
            question_handoff_paths(provider.repository, item, invalid)
    index.write_text("No membership evidence")
    with pytest.raises(TransitionBlocked, match="not established"):
        question_handoff_paths(provider.repository, item)


@pytest.mark.parametrize("retained", [False, True])
def test_normal_question_transition_routes_or_replays(config_file, provider, monkeypatch, retained):
    import asyncio

    app = Application(config_file[0])
    app.provider = AgentProvider(provider.repository, provider.evidence_root)
    item = replace(provider.item("item-one"), state="Running", owner="canonical")
    series = str(__import__("pathlib").Path(item.path).parent / "index.md")
    item = replace(item, content=item.content + "\nSeries: " + series)
    (provider.repository / series).write_text("[Item](item-one.md)")
    # Route reads the configured repository, just as the public runner does.
    monkeypatch.setattr(app, "config", type("Config", (), {"repository": provider.repository})())
    authority = {
        "role": "orchestrator",
        "invocation_id": "question",
        "observed_result": True,
        "item_id": item.item_id,
        "session_id": item.owner,
        "question": {"question_id": "q1", "text": "Approve?"},
    }
    expected = [item.path, "backlog/user-action-required/item-one.md", series]
    if retained:
        expected = [item.path]  # Historical operation remains immutable even after routing changes.
        atomic_json(
            app.root / "provider-agent-operations/old/requested.json",
            {
                "item": asdict(item),
                "target": "User Action Required",
                "authority": authority,
                "paths": expected,
                "source_manifest": {},
            },
        )
        (provider.repository / series).unlink()
    monkeypatch.setattr(app.provider, "item", lambda _: item)
    monkeypatch.setattr(app.provider, "observation", dict)
    monkeypatch.setattr(app.provider, "source_manifest", dict)
    monkeypatch.setattr(app, "validate_management_readiness", lambda _: None)
    atomic_json(app.provider.cache_path, {})

    class Captured(Exception):
        pass

    async def capture(actual_item, target, actor, paths):
        assert paths == expected
        raise Captured

    monkeypatch.setattr(app, "invoke_provider_transition", capture)
    with pytest.raises(Captured):
        asyncio.run(
            app.transition(
                item.item_id,
                item.revision,
                "User Action Required",
                authority,
                validate=validate_transition,
            )
        )


@pytest.mark.parametrize("defect", [None, "actor", "revision", "owner", "evidence"])
def test_ready_question_transition_requires_bound_preparation(provider, defect):
    item = provider.item("item-one")
    authority = {
        "role": "coordinator",
        "observed_result": True,
        "invocation_id": "question-decision",
        "item_id": item.item_id,
        "operation": "await-user",
        "provider_revision": item.revision,
        "preparation_evidence": "preparation",
        "outcome_evidence": "preparation",
        "question": {"question_id": "conflict", "text": "Which requirement governs?"},
    }
    if defect == "actor":
        authority["role"] = "orchestrator"
    elif defect == "revision":
        authority["provider_revision"] = "stale"
    elif defect == "owner":
        item = replace(item, owner="active")
    elif defect == "evidence":
        authority.pop("preparation_evidence")
    if defect:
        with pytest.raises(TransitionBlocked):
            validate_transition(item, "User Action Required", authority)
    else:
        validate_transition(item, "User Action Required", authority)


@pytest.mark.parametrize("path_form", ["list", "named", "wrong-destination", "extra-field"])
@pytest.mark.parametrize("wrong_question", [False, True])
def test_ready_question_preserves_history_and_replays(
    config_file, provider, monkeypatch, tmp_path, wrong_question, path_form
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
    item = provider.item("item-one")
    monkeypatch.setattr(app.provider, "item", lambda _: item)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    evidence = app.root / "native"
    outcome = {
        "role": "coordinator",
        "invocation_id": "preparation",
        "request_digest": "request",
        "binding": {},
        "evidence_path": str(evidence),
        "text": json.dumps(
            {
                "item_id": item.item_id,
                "provider_revision": item.revision,
                "blocked": "Conflicting requirements",
            }
        ),
    }
    atomic_json(
        evidence / "intent.json",
        {
            "invocation_id": "preparation",
            "request_digest": "request",
            "binding": {},
            "config_digest": app.config.file_digest,
        },
    )
    atomic_json(
        app._stage_path(item.item_id, "preparation"),
        {
            "item": asdict(item),
            "decision": outcome,
            "invocation_config_digest": app.config.file_digest,
        },
    )
    question = {"question_id": "conflict", "text": "Which requirement governs?"}
    paths = [item.path, "backlog/user-action-required/item-one.md"]
    supplied = tmp_path / "question.json"
    atomic_json(supplied, {"question": question, "paths": paths})
    effects = []
    calls = []

    async def invoke(*args, **kwargs):
        assert "without reserving or executing" in args[3]
        calls.append(args[1])
        result = {
            "purpose": "provider",
            "request_digest": "decision-request",
            "binding": {},
            "evidence_path": str(app.root / "decision-native"),
            "role": "coordinator",
            "invocation_id": "decision",
            "outcome": "returned",
            "session": {"session_id": "coordinator", "native_session_id": "native"},
            "text": json.dumps(
                {
                    "operation": "await-user",
                    "item_id": item.item_id,
                    "provider_revision": item.revision,
                    "question": {"question_id": "different", "text": "Different?"}
                    if wrong_question
                    else question,
                    "paths": paths
                    if path_form == "list"
                    else {
                        "source": paths[0],
                        "destination": "wrong.md" if path_form == "wrong-destination" else paths[1],
                        "series_membership": [],
                        **({"unknown": True} if path_form == "extra-field" else {}),
                    },
                    "reason": "Requirements conflict",
                }
            ),
        }
        atomic_json(
            app.root / "decision-native" / "intent.json",
            {
                "invocation_id": "decision",
                "request_digest": "decision-request",
                "binding": {},
                "config_digest": app.config.file_digest,
                "item_id": None,
                "action": args[1],
                "operation_id": item.item_id + ":" + args[1],
            },
        )
        atomic_json(app._stage_path(item.item_id, args[1]), result)
        return result

    async def transition(item_id, revision, target, authority, **kwargs):
        validate_transition(item, target, authority)
        assert authority["preparation_evidence"] == "preparation"
        assert kwargs["declared_paths"] == paths
        effects.append((revision, target, authority))
        return {"state": target}

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "transition", transition)
    if wrong_question or path_form in {"wrong-destination", "extra-field"}:
        with pytest.raises(TransitionBlocked, match="exact question handoff"):
            asyncio.run(defer_item(app, item.item_id, supplied))
        assert not effects
        return
    assert asyncio.run(defer_item(app, item.item_id, supplied))["state"] == "User Action Required"
    # A changed observation after effect must reuse the original immutable request.
    monkeypatch.setattr(app.provider, "item", lambda _: replace(item, state="User Action Required"))
    asyncio.run(defer_item(app, item.item_id, supplied))
    assert effects[0] == effects[1]
    assert len(calls) == 1
    native_intent = app.root / "decision-native" / "intent.json"
    wrong_intent = json.loads(native_intent.read_text())
    wrong_intent["operation_id"] = "other-item:other-request"
    atomic_json(native_intent, wrong_intent)
    with pytest.raises(TransitionBlocked, match="immutable request"):
        asyncio.run(defer_item(app, item.item_id, supplied))
    assert len(effects) == 2 and len(calls) == 1
    assert json.loads(app._stage_path(item.item_id, "defer-input").read_text())["item"] == asdict(
        item
    )


@pytest.mark.parametrize("defect", [None, "history", "question", "link", "extra"])
def test_ready_question_committed_receipt_preserves_content_and_link(provider, defect):
    from hashlib import sha256
    from types import SimpleNamespace

    repo = provider.repository
    item = provider.item("item-one")
    source = repo / item.path
    source.write_text(item.content + "\nSeries: backlog/feature-backlog/index.md\n")
    index = repo / "backlog/feature-backlog/index.md"
    index.write_text("[One](item-one.md)\n")
    git(repo, "add", "--", "backlog")
    git(repo, "commit", "-m", "Existing series")
    item = provider.item(item.item_id)
    before = git(repo, "rev-parse", "HEAD")
    destination = "backlog/user-action-required/item-one.md"
    question = {"question_id": "conflict", "text": "Which requirement governs?"}
    content = item.content.replace("Status: Ready", "Status: User Action Required")
    if defect == "history":
        content = content.replace("One item.", "Rewritten acceptance.")
    if defect != "question":
        content += "\n\n## Pending Preparation Question\n\nQuestion ID: conflict\n\nWhich requirement governs?\n"
    if defect == "extra":
        content += "\nNew Requirement: skip review.\n"
    target = repo / destination
    target.parent.mkdir()
    target.write_text(content)
    source.unlink()
    index.write_text(
        "[One](item-one.md)\nChanged\n"
        if defect == "link"
        else "[One](../user-action-required/item-one.md)\n"
    )
    git(repo, "add", "--", "backlog")
    git(repo, "commit", "-m", "Record exact preparation question")
    after = replace(
        item,
        path=destination,
        state="User Action Required",
        content=content,
        revision=sha256(destination.encode() + b"\0" + content.encode()).hexdigest(),
    )
    record = {
        "stage_operation": "question",
        "expected_path": destination,
        "item": asdict(item),
        "target": "User Action Required",
        "head": before,
        "paths": [item.path, destination, "backlog/feature-backlog/index.md"],
        "authority": {"operation": "await-user", "question": question},
    }
    value = {
        "operation_id": "question",
        "before_revision": item.revision,
        "commit": git(repo, "rev-parse", "HEAD"),
        "after": asdict(after),
        "question": question,
    }
    app = object.__new__(Application)
    app.config = SimpleNamespace(repository=repo)
    if defect:
        with pytest.raises(TransitionBlocked):
            app.verify_provider_receipt(record, value)
    else:
        receipt = app.verify_provider_receipt(record, value)
        assert receipt["question"] == question
        assert app.verify_provider_receipt(record, value) == receipt


def test_ready_question_public_route_commits_and_replays(
    config_file, provider, monkeypatch, tmp_path
):
    import asyncio
    from pathlib import Path

    import yaml
    from test_provider_coordination import policy_evidence
    from test_telemetry import payload

    from backlog_harness.contracts import digest
    from backlog_harness.recovery_flow import defer_item, question_handoff_paths
    from backlog_harness.telemetry import Sink

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    for name in ("manage-work-items", "manage-work-items-file"):
        p = Path(data["methodology_root"]) / "skills" / name / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("Fixture management skill")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    index_path = "backlog/feature-backlog/index.md"
    (provider.repository / item.path).write_text(item.content + f"\nSeries: {index_path}\n")
    (provider.repository / index_path).write_text("[One](item-one.md)\n")
    git(provider.repository, "add", "--", "backlog")
    git(provider.repository, "commit", "-m", "Existing series")
    item = provider.item(item.item_id)
    atomic_json(
        app.provider.cache_path,
        {
            "items": [asdict(item)],
            "dependencies": {item.item_id: []},
            "non_items": [{"path": index_path, "kind": "index", "reason": "Series index"}],
            "questions": {},
            "transition_paths": {},
            "archive_debt": [],
            "source_revision": app.provider.source_revision(),
            "source_manifest": app.provider.source_manifest(),
            "observer_digest": app.provider_observer_digest(app.config),
            "policy": {
                "eligible": True,
                "mode": "SOLO",
                "primary_branch": "main",
                "evidence": policy_evidence(provider.repository),
            },
        },
    )
    calls = []
    mutations = []

    def envelope(stage, value):
        native = app.root / "test-native" / stage
        sink = Sink(native / "telemetry.jsonl", {})
        sink.write(payload())
        result = {
            "invocation_id": stage,
            "request_digest": digest(value),
            "binding": asdict(app.config.binding("coordinator")),
            "role": "coordinator",
            "purpose": "provider",
            "outcome": "returned",
            "session": {"session_id": stage, "native_session_id": "native-" + stage},
            "evidence_path": str(native),
            "text": json.dumps(value),
            "events": [{"type": "turn.completed", "usage": {"output_tokens": 1}}],
            "telemetry_path": str(native / "telemetry.jsonl"),
            "telemetry": {
                "span_count": len(sink.seen),
                "rejected_exports": 0,
                "evidence_sha256": sink.evidence_digest(),
            },
        }
        atomic_json(
            native / "intent.json",
            {
                "invocation_id": stage,
                "request_digest": result["request_digest"],
                "binding": result["binding"],
                "config_digest": app.config.file_digest,
                "item_id": None,
                "action": stage,
                "operation_id": item.item_id + ":" + stage,
            },
        )
        atomic_json(app._stage_path(item.item_id, stage), result)
        return result

    preparation = envelope(
        "prepare",
        {
            "item_id": item.item_id,
            "provider_revision": item.revision,
            "blocked": "Conflicting requirements",
        },
    )
    atomic_json(
        app._stage_path(item.item_id, "preparation"),
        {
            "item": asdict(item),
            "decision": preparation,
            "invocation_config_digest": app.config.file_digest,
        },
    )
    question = {"question_id": "conflict", "text": "Which requirement governs?"}
    paths = question_handoff_paths(provider.repository, item)
    supplied = tmp_path / "question.json"
    atomic_json(supplied, {"question": question, "paths": paths})

    async def native_agent(item_id, stage, role, prompt, **kwargs):
        saved = app._stage_path(item_id, stage)
        if saved.exists():
            return json.loads(saved.read_text())
        calls.append(stage)
        if stage.startswith("defer-decision-"):
            return envelope(
                stage,
                {
                    "operation": "await-user",
                    "item_id": item_id,
                    "provider_revision": item.revision,
                    "question": question,
                    "paths": {
                        "source": paths[0],
                        "destination": paths[1],
                        "series_membership": paths[2:],
                    },
                    "reason": "Conflicting requirements require a user answer",
                },
            )
        assert kwargs["provider_operation"] and kwargs["read_only"] is False
        request = json.loads(prompt.split("\n", 1)[1])
        destination = provider.repository / paths[1]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(request["ready_question_content"])
        (provider.repository / paths[0]).unlink()
        (provider.repository / index_path).write_text(
            "[One](../user-action-required/item-one.md)\n"
        )
        git(provider.repository, "add", "--", *paths)
        git(provider.repository, "commit", "-m", "Record question", "--", *paths)
        mutations.append(git(provider.repository, "rev-parse", "HEAD"))
        return envelope(
            stage,
            {
                "operation_id": request["operation_id"],
                "before_revision": item.revision,
                "commit": mutations[-1],
                "after": {
                    "item_id": item_id,
                    "path": paths[1],
                    "state": "User Action Required",
                    "owner": item.owner,
                    "original_high": item.original_high,
                },
                "question": question,
            },
        )

    monkeypatch.setattr(app, "invoke", native_agent)
    first = asyncio.run(defer_item(app, item.item_id, supplied))
    second = asyncio.run(defer_item(app, item.item_id, supplied))
    assert first == second
    assert len(calls) == 2 and len(mutations) == 1
    assert app.provider.item(item.item_id).state == "User Action Required"
    assert app.provider.question(app.provider.item(item.item_id))["text"] == question["text"]
    assert not (provider.repository / paths[0]).exists()
    assert question["text"] in (provider.repository / paths[1]).read_text()
    assert "../user-action-required/item-one.md" in (provider.repository / index_path).read_text()
