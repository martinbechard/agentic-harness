import pytest

from backlog_harness.provider import TransitionBlocked, git
from backlog_harness.workflow import validate_candidate, validate_transition


def authority(role, **values):
    return {
        "role": role,
        "invocation_id": "observed-invocation",
        "observed_result": True,
        "item_id": "item-one",
        **values,
    }


@pytest.mark.parametrize("coordination", ["none", "resource-claim"])
def test_valid_admission_and_reject_missing_actor_stale_revision(provider, coordination):
    project = provider.repository / "PROJECT.yaml"
    project.write_text(project.read_text().replace("selected: none", f"selected: {coordination}"))
    git(provider.repository, "add", "--", "PROJECT.yaml")
    if coordination != "none":
        git(provider.repository, "commit", "-m", "Select agent resource coordination")
    item = provider.item("item-one")
    head = git(provider.repository, "rev-parse", "HEAD")
    with pytest.raises(TransitionBlocked):
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("orchestrator", operation="new"),
            validate=validate_transition,
        )
    assert git(provider.repository, "rev-parse", "HEAD") == head
    receipt = provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    assert receipt["state"] == "Starting"
    assert (
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("coordinator", operation="new"),
            validate=validate_transition,
        )
        == receipt
    )
    with pytest.raises(TransitionBlocked, match="Stale"):
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("coordinator", operation="new", invocation_id="another-attempt"),
            validate=validate_transition,
        )
    current = provider.item(item.item_id)
    with pytest.raises(TransitionBlocked, match="acceptance"):
        provider.transition(
            item.item_id,
            current.revision,
            "Running",
            authority("orchestrator"),
            validate=validate_transition,
        )
    provider.transition(
        item.item_id,
        current.revision,
        "Running",
        authority(
            "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
        ),
        validate=validate_transition,
    )
    assert provider.item(item.item_id).owner == "canonical"
    current = provider.item(item.item_id)
    with pytest.raises(TransitionBlocked, match="Commit READY"):
        provider.transition(
            item.item_id,
            current.revision,
            "Completed",
            authority("orchestrator", session_id="canonical"),
            validate=validate_transition,
        )
    assert provider.item(item.item_id).state == "Running"


def test_agent_owned_claim_policy_is_preserved(provider):
    p = provider.repository / "PROJECT.yaml"
    p.write_text(p.read_text().replace("selected: none", "selected: resource-claim"))
    before = p.read_bytes()
    assert provider.policy() == "main"
    assert p.read_bytes() == before
    p.write_text(p.read_text().replace("execution_mode: SOLO", "execution_mode: invalid"))
    with pytest.raises(TransitionBlocked, match="explicit execution mode"):
        provider.policy()


def test_candidate_bound_independent_review(provider):
    repo = provider.repository
    base = git(repo, "rev-parse", "HEAD")
    (repo / "answer.txt").write_text("42\n")
    git(repo, "add", "--", "answer.txt")
    git(repo, "commit", "-m", "Candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    review = {
        "candidate": candidate,
        "verdict": "ACCEPT",
        "reviewer_session": "reviewer",
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": "review-hash",
        "unresolved_findings": [],
    }
    checks = [
        {"candidate": candidate, "argv": ["test"], "returncode": 0, "evidence_sha256": "check-hash"}
    ]

    def check(r=review, c=checks):
        return validate_candidate(repo, candidate, base, ["answer.txt"], "producer", r, c)

    assert check() == ["answer.txt"]
    for bad in [
        None,
        {**review, "candidate": base},
        {**review, "reviewer_session": "producer"},
        {**review, "fresh_context": False},
        {**review, "verdict": "REJECT"},
        {**review, "unresolved_findings": ["bug"]},
    ]:
        with pytest.raises(TransitionBlocked):
            check(bad)
    with pytest.raises(TransitionBlocked):
        check(c=[{**checks[0], "candidate": base}])
    (repo / "answer.txt").write_text("unreviewed\n")
    with pytest.raises(TransitionBlocked, match="dirty"):
        check()


def test_hold_preserves_canonical_owner_and_unknown_cannot_release(provider):
    item = provider.item("item-one")
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    provider.transition(
        item.item_id,
        item.revision,
        "Running",
        authority(
            "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
        ),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    provider.transition(
        item.item_id,
        item.revision,
        "Holding",
        authority("coordinator", session_id="coordinator", incident="usage_limit"),
        validate=validate_transition,
    )
    held = provider.item(item.item_id)
    assert held.owner == "canonical"
    actor = authority(
        "coordinator",
        operation="resume",
        session_id="coordinator",
        retained_owner="canonical",
        usage={"status": "unknown", "may_generate": False},
    )
    with pytest.raises(TransitionBlocked, match="Unknown"):
        provider.transition(
            held.item_id, held.revision, "Running", actor, validate=validate_transition
        )
    actor["usage"] = {
        "status": "below",
        "generated_tokens": 201,
        "ceiling": 300,
        "may_generate": True,
    }
    provider.transition(held.item_id, held.revision, "Running", actor, validate=validate_transition)
    assert provider.item(item.item_id).owner == "canonical"


def test_question_gate_requires_canonical_owner_and_exact_approval(provider):
    item = provider.item("item-one")
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    owner = authority(
        "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
    )
    provider.transition(item.item_id, item.revision, "Running", owner, validate=validate_transition)
    item = provider.item(item.item_id)
    question = {"question_id": "q1", "text": "Approve?"}
    with pytest.raises(TransitionBlocked, match="canonical"):
        provider.transition(
            item.item_id,
            item.revision,
            "User Action Required",
            {**owner, "session_id": "another", "question": question},
            validate=validate_transition,
        )
    provider.transition(
        item.item_id,
        item.revision,
        "User Action Required",
        {**owner, "question": question},
        validate=validate_transition,
    )
    waiting = provider.item(item.item_id)
    assert provider.question(waiting)["text"] == "Approve?"
    answer = {"digest": "answer-hash"}
    for disposition in ["defer", "decline", "ambiguous", None]:
        with pytest.raises(TransitionBlocked, match="approval"):
            provider.transition(
                item.item_id,
                waiting.revision,
                "Running",
                {**owner, "question": question, "answer": answer, "disposition": disposition},
                validate=validate_transition,
            )
    provider.transition(
        item.item_id,
        waiting.revision,
        "Running",
        {**owner, "question": question, "answer": answer, "disposition": "approve"},
        validate=validate_transition,
    )
    assert provider.item(item.item_id).owner == "canonical"


def test_agent_provider_receipt_requires_exact_commit_evidence(provider):
    from dataclasses import asdict
    from types import SimpleNamespace

    from backlog_harness.application import Application

    app = object.__new__(Application)
    app.config = SimpleNamespace(repository=provider.repository)
    item = provider.item("item-one")
    record = {
        "stage_operation": "one:reserve",
        "item": asdict(item),
        "target": "Starting",
        "head": git(provider.repository, "rev-parse", "HEAD"),
        "paths": [item.path],
    }
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    after = provider.item(item.item_id)
    value = {
        "operation_id": "one:reserve",
        "before_revision": item.revision,
        "commit": git(provider.repository, "rev-parse", "HEAD"),
        "after": asdict(after),
    }
    assert app.verify_provider_receipt(record, value)["after"]["state"] == "Starting"
    for changed, message in [
        ({"commit": None}, "commit identity"),
        ({"before_revision": "stale"}, "revision differs"),
        ({"operation_id": "different"}, "operation differs"),
        ({"after": {**asdict(after), "owner": "management-invocation"}}, "canonical owner"),
        ({"after": {**asdict(after), "content": "agent says done"}}, "content differs"),
        ({"after": {**asdict(after), "revision": "invalid"}}, "resulting revision differs"),
    ]:
        with pytest.raises(TransitionBlocked, match=message):
            app.verify_provider_receipt(record, {**value, **changed})


def test_provider_management_cannot_invent_decision_owner(config_file, provider):
    import asyncio

    from backlog_harness.application import Application

    config, _ = config_file
    app = Application(config)
    item = provider.item("item-one")
    with pytest.raises(TransitionBlocked, match="decision-owner evidence"):
        asyncio.run(
            app.invoke_provider_transition(
                item, "Starting", authority("coordinator", operation="new"), [item.path]
            )
        )
    assert not (app.root / "provider-agent-operations").exists()
    assert provider.item(item.item_id).state == "Ready"


def test_agent_observation_refresh_is_cached_and_never_parses_headers(
    config_file, provider, monkeypatch
):
    import asyncio
    import json
    from dataclasses import asdict

    import yaml

    from backlog_harness.application import Application

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    calls = []

    async def observe(*args, **kwargs):
        calls.append(kwargs)
        return {
            "invocation_id": "observation",
            "events": [{"type": "turn.completed", "usage": {"output_tokens": 10}}],
            "text": json.dumps(
                {
                    "items": [
                        {
                            key: value
                            for key, value in asdict(item).items()
                            if key not in {"content", "revision"}
                        }
                    ],
                    "dependencies": {item.item_id: []},
                    "policy": {
                        "eligible": True,
                        "mode": "SOLO",
                        "primary_branch": "main",
                        "evidence": policy_evidence(provider.repository),
                    },
                }
            ),
        }

    monkeypatch.setattr(app, "invoke", observe)
    asyncio.run(app.refresh_provider())
    assert app.provider.item(item.item_id) == item
    asyncio.run(app.refresh_provider())
    assert len(calls) == 1
    assert calls[0]["purpose"] == "provider"
    assert app.provider.policy()["mode"] == "SOLO"
    from backlog_harness.contracts import digest

    cached = json.loads(app.provider.cache_path.read_text())
    cached["observer_digest"] = digest(
        [app.config.file_digest, app.config.binding("coordinator").relevant_digest]
    )
    app.provider.cache_path.write_text(json.dumps(cached))
    asyncio.run(app.refresh_provider())
    assert len(calls) == 1
    data["workflow"]["allowed_paths"] = ["another.py"]
    data["workflow"]["checks"] = [["python3", "-m", "unittest"]]
    config.write_text(yaml.safe_dump(data))
    asyncio.run(app.refresh_provider())
    assert len(calls) == 1
    data["profiles"]["control"]["model"] = "updated-observer"
    config.write_text(yaml.safe_dump(data))
    asyncio.run(app.refresh_provider())
    assert len(calls) == 1
    with pytest.raises(TransitionBlocked, match="responsible agent"):
        app.provider.transition(item.item_id, item.revision, "Starting", {})
    (provider.repository / item.path).write_text(item.content + "\nchanged\n")
    with pytest.raises(TransitionBlocked, match="stale"):
        app.provider.snapshot()
    asyncio.run(app.refresh_provider())
    assert app.provider.item(item.item_id).content.endswith("changed\n")


@pytest.mark.parametrize("invalid_policy", [False, True])
def test_agent_transition_recovers_committed_operation_without_second_mutation(
    config_file, provider, monkeypatch, invalid_policy
):
    import asyncio
    import json
    from dataclasses import asdict

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json, component

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    config.write_text(yaml.safe_dump(data))
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    data["profiles"]["control"]["skills"] = ["manage-work-items", "manage-work-items-file"]
    from pathlib import Path

    for skill in data["profiles"]["control"]["skills"]:
        skill_path = Path(data["methodology_root"]) / "skills" / skill / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("Test management skill")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    before = provider.item("item-one")
    decision = {
        "invocation_id": "admission",
        "outcome": "returned",
        "role": "coordinator",
        "session": {"session_id": "decision-owner", "native_session_id": "native"},
    }
    atomic_json(app._stage_path(before.item_id, "admit"), decision)
    actor = app.authority(decision, before.item_id, operation="new")
    # Telemetry validation is covered separately; this test exercises the real operation/commit path.
    monkeypatch.setattr(app, "validate_invocation_result", lambda result: None)
    mutations = []
    saved = {}

    async def agent(item_id, stage, role, prompt, **kwargs):
        if stage in saved:
            return saved[stage]
        if kwargs.get("provider_operation"):
            request = json.loads(
                (
                    app.root
                    / "provider-agent-operations"
                    / component(kwargs["provider_operation"])
                    / "requested.json"
                ).read_text()
            )
            assert request["authority"] == actor
            current = provider.item(before.item_id)
            assert current.revision == request["item"]["revision"]
            path = provider.repository / current.path
            path.write_text(current.content.replace("Status: Ready", "Status: Starting"))
            git(provider.repository, "add", "--", current.path)
            git(provider.repository, "commit", "-m", "Agent reservation", "--", current.path)
            mutations.append(git(provider.repository, "rev-parse", "HEAD"))
            payload = {
                "operation_id": request["stage_operation"],
                "before_revision": before.revision,
                "commit": mutations[-1],
                "after": asdict(provider.item(before.item_id)),
            }
            if invalid_policy:
                payload["policy"] = {
                    "eligible": True,
                    "mode": "SOLO",
                    "primary_branch": "main",
                    "evidence": [],
                }
        else:
            payload = {
                "items": [asdict(provider.item(before.item_id))],
                "dependencies": {before.item_id: []},
                "policy": {
                    "eligible": True,
                    "mode": "SOLO",
                    "primary_branch": "main",
                    "evidence": policy_evidence(provider.repository),
                },
            }
        result = {
            "invocation_id": stage,
            "session": {"session_id": "executor"},
            "evidence_path": "test-execution",
            "text": json.dumps(payload),
            "events": [{"type": "turn.completed", "usage": {"output_tokens": 10}}],
        }
        saved[stage] = result
        atomic_json(app._stage_path(item_id, stage), result)
        return result

    monkeypatch.setattr(app, "invoke", agent)
    asyncio.run(app.refresh_provider())
    native_json = atomic_json
    crashed = False

    def fail_receipt(path, value, **kwargs):
        nonlocal crashed
        if path.name == "receipt.json" and not crashed:
            crashed = True
            raise RuntimeError("crash after provider commit")
        return native_json(path, value, **kwargs)

    monkeypatch.setattr("backlog_harness.application.atomic_json", fail_receipt)
    with pytest.raises(RuntimeError, match="crash after"):
        asyncio.run(
            app.transition(
                before.item_id, before.revision, "Starting", actor, validate=validate_transition
            )
        )
    request_path = next((app.root / "provider-agent-operations").glob("*/requested.json"))
    observed_effect = app.reconcile_agent_provider(request_path)
    assert observed_effect["advancement_verified"] is False
    assert len(mutations) == 1
    if invalid_policy:
        historical = json.loads((request_path.parent / "receipt.json").read_text())
        historical["advancement_verified"] = True
        native_json(request_path.parent / "receipt.json", historical)
        receipt = asyncio.run(
            app.transition(
                before.item_id, before.revision, "Starting", actor, validate=validate_transition
            )
        )
        original_path = request_path.parent / "receipt-original-policy.json"
        original_bytes = original_path.read_bytes()
        original = json.loads(original_bytes)
        assert original["policy"]["evidence"] == []
        assert original["advancement_verified"] is False
        reconciliation_path = request_path.parent / "policy-reconciliation.json"
        reconciliation_bytes = reconciliation_path.read_bytes()
        reconciliation = json.loads(reconciliation_bytes)
        assert reconciliation["policy"] == {
            "eligible": True,
            "mode": "SOLO",
            "primary_branch": "main",
            "evidence": policy_evidence(provider.repository),
        }
        app.provider.validate_policy(reconciliation["policy"])
        assert receipt["advancement_verified"] is True
        asyncio.run(
            app.transition(
                before.item_id, before.revision, "Starting", actor, validate=validate_transition
            )
        )
        assert original_path.read_bytes() == original_bytes
        assert reconciliation_path.read_bytes() == reconciliation_bytes
        assert len(mutations) == 1
        return
    receipt = asyncio.run(
        app.transition(
            before.item_id, before.revision, "Starting", actor, validate=validate_transition
        )
    )
    assert receipt["state"] == "Starting"
    assert receipt["decision_owner"] == "decision-owner"
    assert receipt["executing_session"]["session_id"] == "executor"
    assert len(mutations) == 1
    assert app.provider.item(before.item_id).owner == before.owner


def test_agent_completion_requires_archive_move(provider):
    from dataclasses import asdict, replace
    from hashlib import sha256
    from types import SimpleNamespace

    from backlog_harness.application import Application

    app = object.__new__(Application)
    app.config = SimpleNamespace(repository=provider.repository)
    item = provider.item("item-one")
    archive = "backlog/completed-backlog/features/item-one.md"
    head = git(provider.repository, "rev-parse", "HEAD")
    content = item.content.replace("Status: Ready", "Status: Completed")
    path = provider.repository / archive
    path.parent.mkdir(parents=True)
    path.write_text(content)
    git(provider.repository, "add", "--", archive)
    git(provider.repository, "commit", "-m", "Incomplete archive copy")
    after = replace(
        item,
        path=archive,
        state="Completed",
        content=content,
        revision=sha256(archive.encode() + b"\0" + content.encode()).hexdigest(),
    )
    record = {
        "stage_operation": "complete",
        "item": asdict(item),
        "target": "Completed",
        "head": head,
        "paths": [item.path, archive],
        "expected_path": archive,
    }
    result = {
        "operation_id": "complete",
        "before_revision": item.revision,
        "commit": git(provider.repository, "rev-parse", "HEAD"),
        "after": asdict(after),
    }
    with pytest.raises(TransitionBlocked, match="archive paths are incomplete"):
        app.verify_provider_receipt(record, result)
    with pytest.raises(TransitionBlocked, match="destination"):
        app.verify_provider_receipt(
            record, {**result, "after": {**asdict(after), "path": item.path}}
        )


def test_agent_inventory_requires_exact_file_partition(provider):
    from dataclasses import asdict

    from backlog_harness.provider import AgentProvider

    view = AgentProvider(provider.repository, provider.evidence_root)
    item = provider.item("item-one")
    value = {"items": [asdict(item)], "dependencies": {item.item_id: []}}
    view.validate_inventory(value)
    with pytest.raises(TransitionBlocked, match="unobserved item"):
        view.validate_inventory({**value, "dependencies": {item.item_id: ["missing"]}})
    with pytest.raises(TransitionBlocked, match="incomplete or duplicated"):
        view.validate_inventory({"items": [], "dependencies": {}})
    extra = provider.repository / "backlog/index.md"
    extra.write_text("# Series index\n")
    with pytest.raises(TransitionBlocked, match="incomplete or duplicated"):
        view.validate_inventory(value)
    classified = {
        **value,
        "non_items": [
            {
                "path": "backlog/index.md",
                "kind": "index",
                "reason": "Navigation only; no executable work item",
            }
        ],
    }
    view.validate_inventory(classified)
    with pytest.raises(TransitionBlocked, match="incomplete or duplicated"):
        view.validate_inventory({**classified, "non_items": classified["non_items"] * 2})


def policy_evidence(repository):
    from hashlib import sha256

    content = (repository / "PROJECT.yaml").read_bytes()
    return [
        {
            "path": "PROJECT.yaml",
            "sha256": sha256(content).hexdigest(),
            "excerpt": "execution_mode: SOLO",
            "supports": ["mode", "admission"],
        }
    ]


def retained_provider_observation(app, provider):
    """Build one compact retained envelope for policy-only recovery tests."""
    import json
    from dataclasses import asdict
    from hashlib import sha256

    from backlog_harness.contracts import digest, load_config
    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider_observation import PROVIDER_OBSERVATION_PROMPT

    current = load_config(app.config_path)
    revision = app.provider.source_revision()
    observer = app.provider_observer_digest(current)
    stage = "observe-" + digest([revision, observer])
    evidence = app.root / "runs/retained-observation"
    evidence.mkdir(parents=True)
    for name in ("events.jsonl", "outcomes.jsonl"):
        (evidence / name).write_text("{}\n")
    for name in ("telemetry.json", "telemetry-report.json", "session.json"):
        atomic_json(evidence / name, {})
    atomic_json(
        evidence / "execution-context.json",
        {"purpose": "provider", "provider_operation": None, "read_only": True},
    )
    item = provider.item("item-one")
    compact = {
        key: value for key, value in asdict(item).items() if key not in {"content", "revision"}
    }
    project = provider.repository / "PROJECT.yaml"
    value = {
        "items": [compact],
        "dependencies": {item.item_id: []},
        "policy": {
            "eligible": False,
            "mode": "SOLO",
            "primary_branch": "main",
            "evidence": [
                {
                    "path": "PROJECT.yaml",
                    "sha256": sha256(project.read_bytes()).hexdigest(),
                    "excerpt": "execution_mode: SOLO\nmissing intervening authority",
                    "supports": ["mode", "admission"],
                }
            ],
        },
    }
    request_digest = digest(
        ["provider", str(current.repository), True, None, PROVIDER_OBSERVATION_PROMPT]
    )
    atomic_json(evidence / "intent.json", {"request_digest": request_digest})
    result = {
        "version": 1,
        "request_digest": request_digest,
        "invocation_id": "retained-inventory",
        "outcome": "returned",
        "role": "coordinator",
        "purpose": "provider",
        "binding": asdict(current.binding("coordinator")),
        "session": {},
        "text": json.dumps(value),
        "events": [],
        "telemetry": {},
        "telemetry_path": str(evidence / "telemetry.jsonl"),
        "evidence_path": str(evidence),
    }
    envelope = app._stage_path("provider-inventory", stage)
    atomic_json(envelope, result)
    prior = {
        "partial": False,
        "outcome": "returned",
        "operation_id": "provider-inventory:" + stage,
        "action": stage,
        "request_digest": request_digest,
        "invocation_id": result["invocation_id"],
        "binding": result["binding"],
    }
    return envelope, result, prior, value


def test_agent_policy_binds_sources_and_project_invariants(provider):
    from backlog_harness.provider import AgentProvider

    view = AgentProvider(provider.repository, provider.evidence_root)
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": policy_evidence(provider.repository),
    }
    view.validate_policy(policy)
    view.validate_policy({**policy, "eligible": False})
    with pytest.raises(TransitionBlocked, match="Structured"):
        view.validate_policy({**policy, "evidence": "trust the agent"})
    with pytest.raises(TransitionBlocked, match="Stale"):
        view.validate_policy({**policy, "evidence": [{**policy["evidence"][0], "sha256": "stale"}]})
    with pytest.raises(TransitionBlocked, match="execution mode differs"):
        view.validate_policy({**policy, "mode": "MULTITASK"})


@pytest.mark.parametrize("alias", [False, True])
def test_agent_policy_rejects_harness_operational_output_as_authority(provider, alias):
    from hashlib import sha256

    from backlog_harness.provider import AgentProvider

    operational_root = provider.repository / ".agent-ops/backlog-harness"
    operational_root.mkdir(parents=True)
    run = operational_root / "run.json"
    run.write_text('{"admission_open": false}\n')
    cited = run
    if alias:
        linked = provider.repository / "retained-output"
        linked.symlink_to(operational_root, target_is_directory=True)
        cited = linked / "run.json"
    view = AgentProvider(provider.repository, operational_root)
    policy = {
        "eligible": False,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": str(cited.relative_to(provider.repository)),
                "sha256": sha256(cited.read_bytes()).hexdigest(),
                "excerpt": '"admission_open": false',
                "supports": ["mode", "admission"],
            }
        ],
    }
    with pytest.raises(TransitionBlocked, match="operational output"):
        view.validate_policy(policy)


def test_same_revision_observation_revalidates_policy_authority(provider):
    import json
    from dataclasses import asdict
    from hashlib import sha256

    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import AgentProvider

    operational_root = provider.repository / ".agent-ops/backlog-harness"
    operational_root.mkdir(parents=True)
    run = operational_root / "run.json"
    run.write_text('{"admission_open": false}\n')
    view = AgentProvider(provider.repository, operational_root)
    item = provider.item("item-one")
    atomic_json(
        view.cache_path,
        {
            "source_revision": view.source_revision(),
            "source_manifest": view.source_manifest(),
            "items": [asdict(item)],
            "dependencies": {item.item_id: []},
            "policy": {
                "eligible": False,
                "mode": "SOLO",
                "primary_branch": "main",
                "evidence": [
                    {
                        "path": str(run.relative_to(provider.repository)),
                        "sha256": sha256(run.read_bytes()).hexdigest(),
                        "excerpt": '"admission_open": false',
                        "supports": ["mode", "admission"],
                    }
                ],
            },
        },
    )
    before = json.loads(view.cache_path.read_text())
    with pytest.raises(TransitionBlocked, match="operational output"):
        view.observation()
    assert json.loads(view.cache_path.read_text()) == before


def test_policy_reassessment_retains_inventory_across_source_only_head_advance(provider):
    from dataclasses import asdict

    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import AgentProvider

    view = AgentProvider(provider.repository, provider.evidence_root)
    item = provider.item("item-one")
    original_revision = view.source_revision()
    atomic_json(
        view.cache_path,
        {
            "source_revision": original_revision,
            "source_manifest": view.source_manifest(),
            "items": [asdict(item)],
            "dependencies": {item.item_id: []},
            "policy": {
                "eligible": False,
                "mode": "SOLO",
                "primary_branch": "main",
                "evidence": policy_evidence(provider.repository),
            },
        },
    )
    unrelated = provider.repository / "answer.txt"
    unrelated.write_text("Unrelated source change\n")
    git(provider.repository, "add", "--", "answer.txt")
    git(provider.repository, "commit", "-m", "Unrelated source")
    observed = view.policy_reassessment_observation()
    assert observed["source_revision"] == view.source_revision()
    assert observed["source_revision"] != original_revision
    assert observed["items"] == [asdict(item)]


def test_management_readiness_rejects_readonly_and_missing_skills(config_file, provider):
    import yaml

    from backlog_harness.application import Application

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    with pytest.raises(TransitionBlocked, match="workspace-write"):
        app.validate_management_readiness("coordinator")
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(TransitionBlocked, match="skills are missing"):
        app.validate_management_readiness("coordinator")


def test_helper_discovery_cannot_cross_cli_bindings(config_file, provider, tmp_path):
    import copy

    import yaml

    from backlog_harness.application import Application

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    home = tmp_path / "native-home"
    home.mkdir()
    (home / "config.toml").write_text("")
    data["agent_clis"]["primary"]["adapter_options"] = {
        "codex_home": str(home),
        "load_user_config": True,
    }
    data["agent_clis"]["other"] = copy.deepcopy(data["agent_clis"]["primary"])
    data["agents"]["orchestrator"]["cli"] = "other"
    project = provider.repository / "PROJECT.yaml"
    value = yaml.safe_load(project.read_text())
    value["resource_coordination"] = {"selected": "resource-claim"}
    value["agent_claim_transport"] = {"selected": "mcp"}
    project.write_text(yaml.safe_dump(value))
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    with pytest.raises(TransitionBlocked, match="matching helper discovery binding"):
        app.validate_management_readiness("orchestrator")


def test_explicit_unknown_dependencies_are_retained_but_block_execution(
    config_file, provider, monkeypatch
):
    import asyncio
    from dataclasses import asdict

    from backlog_harness.application import Application
    from backlog_harness.provider import AgentProvider

    item = provider.item("item-one")
    view = AgentProvider(provider.repository, provider.evidence_root)
    value = {"items": [asdict(item)], "dependencies": {}, "dependency_omissions": "unknown"}
    view.validate_inventory(value)
    with pytest.raises(TransitionBlocked, match="incomplete"):
        view.validate_inventory({"items": [asdict(item)], "dependencies": {}})
    app = Application(config_file[0])
    app.provider = view
    monkeypatch.setattr(app, "reconcile", lambda: None)
    monkeypatch.setattr(view, "policy", dict)
    monkeypatch.setattr(app, "execution_policy", lambda _: {})
    monkeypatch.setattr(view, "item", lambda _: item)
    monkeypatch.setattr(view, "observation", lambda: value)
    with pytest.raises(TransitionBlocked, match="dependencies are unknown"):
        asyncio.run(app._run_item(item.item_id))


@pytest.mark.parametrize("case", ["valid", "missing", "stale", "item_changed"])
def test_policy_reassessment_requires_current_authority(config_file, provider, monkeypatch, case):
    import asyncio
    import json
    from dataclasses import asdict

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    revision = app.provider.source_revision()
    evidence = policy_evidence(provider.repository)
    original = {
        "source_revision": revision,
        "source_manifest": app.provider.source_manifest(),
        "policy": {
            "eligible": False,
            "mode": "SOLO",
            "primary_branch": "main",
            "evidence": evidence,
        },
        "items": [asdict(item)],
        "dependencies": {item.item_id: []},
        "questions": {item.item_id: {"question_id": "q1"}},
    }
    atomic_json(app.provider.cache_path, original)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)

    async def invoke(*args, **kwargs):
        policy = {**original["policy"], "eligible": True}
        if case == "missing":
            policy["evidence"] = []
        if case == "stale":
            policy["evidence"] = [{**evidence[0], "sha256": "0" * 64}]
        if case == "item_changed":
            (provider.repository / "backlog/feature-backlog/item-one.md").write_text("Changed")
        return {
            "invocation_id": "policy-decision",
            "text": json.dumps(
                {
                    "source_revision": revision,
                    "reason": "Current global authority",
                    "policy": policy,
                }
            ),
        }

    monkeypatch.setattr(app, "invoke", invoke)
    if case == "valid":
        assert asyncio.run(app.reassess_policy())["eligible"] is True
        current = app.provider.observation()
        assert current["questions"] == original["questions"]
        assert current["items"] == original["items"]
        assert current["policy_reassessment"]["invocation_id"] == "policy-decision"
    else:
        with pytest.raises(TransitionBlocked):
            asyncio.run(app.reassess_policy())
        assert json.loads(app.provider.cache_path.read_text()) == original


@pytest.mark.parametrize("compatibility", ["exact", "observer-drift", "capability-drift"])
def test_policy_reassessment_repairs_rejected_cached_authority_without_inventory_call(
    config_file, provider, monkeypatch, compatibility
):
    import asyncio
    import inspect
    import json
    from dataclasses import asdict
    from hashlib import sha256

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.contracts import digest, load_config
    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import AgentProvider
    from backlog_harness.provider_observation import (
        LEGACY_POLICY_VALIDATOR_DIGEST,
        LEGACY_PROVIDER_OBSERVATION_PROMPT,
        PROVIDER_OBSERVATION_SCHEMA,
        effective_instruction_sources,
        effective_skill_sources,
        semantic_observation_fingerprint,
    )

    config, data = config_file
    operational_root = provider.repository / ".agent-ops/backlog-harness"
    operational_root.mkdir(parents=True)
    data.update(
        repository=str(provider.repository),
        operational_root=str(operational_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    current = load_config(config)
    legacy_schema = {
        **PROVIDER_OBSERVATION_SCHEMA,
        "validators": {
            "inventory": digest(inspect.getsource(AgentProvider.validate_inventory)),
            "policy": LEGACY_POLICY_VALIDATOR_DIGEST,
            "acceptance": digest(inspect.getsource(Application._accept_provider_observation)),
        },
    }
    legacy_observer = semantic_observation_fingerprint(
        current,
        prompt=LEGACY_PROVIDER_OBSERVATION_PROMPT,
        schema=legacy_schema,
        instruction_sources=effective_instruction_sources(current),
        skill_sources=effective_skill_sources(current),
    )
    run = operational_root / "run.json"
    run.write_text('{"admission_open": false}\n')
    item = provider.item("item-one")
    inventory = [asdict(item)]
    old_policy = {
        "eligible": False,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": str(run.relative_to(provider.repository)),
                "sha256": sha256(run.read_bytes()).hexdigest(),
                "excerpt": '"admission_open": false',
                "supports": ["mode", "admission"],
            }
        ],
    }
    cached = {
        "source_revision": app.provider.source_revision(),
        "source_manifest": app.provider.source_manifest(),
        "observer_digest": (
            legacy_observer if compatibility != "observer-drift" else "unrelated-contract"
        ),
        "capability_digest": (
            app.provider_capability_digest(current, legacy_observer)
            if compatibility != "capability-drift"
            else "changed-capability"
        ),
        "observer_binding_digest": current.binding("coordinator").relevant_digest,
        "items": inventory,
        "dependencies": {item.item_id: []},
        "policy": old_policy,
        "invocation_id": "original-inventory",
    }
    atomic_json(app.provider.cache_path, cached)
    calls = []

    async def invoke(item_id, stage, role, prompt, **kwargs):
        calls.append((item_id, stage, role, prompt, kwargs))
        assert item_id == "provider-policy"
        return {
            "invocation_id": "policy-decision",
            "text": json.dumps(
                {
                    "source_revision": cached["source_revision"],
                    "reason": "Canonical project authority permits independent Ready work",
                    "policy": {
                        "eligible": True,
                        "mode": "SOLO",
                        "primary_branch": "main",
                        "evidence": policy_evidence(provider.repository),
                    },
                }
            ),
        }

    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    assert asyncio.run(app.reassess_policy())["eligible"] is True
    repaired = json.loads(app.provider.cache_path.read_text())
    assert repaired["items"] == inventory
    assert repaired["invocation_id"] == "original-inventory"
    if compatibility == "exact":
        assert repaired["observer_digest"] == app.provider_observer_digest(current)
        assert repaired["capability_digest"] == app.provider_capability_digest(
            current, repaired["observer_digest"]
        )
        asyncio.run(app.refresh_provider())
    elif compatibility == "observer-drift":
        assert repaired["observer_digest"] == "unrelated-contract"
    else:
        assert repaired["observer_digest"] == legacy_observer
        assert repaired["capability_digest"] == "changed-capability"
    assert len(calls) == 1


def test_policy_reassessment_accepts_exact_retained_inventory_without_repeating_observation(
    config_file, provider, monkeypatch
):
    import asyncio
    import json

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceStore, atomic_json

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    envelope, retained, prior, _ = retained_provider_observation(app, provider)
    original = {"stale": "cache must survive until the replacement is valid"}
    atomic_json(app.provider.cache_path, original)
    monkeypatch.setattr(EvidenceStore, "reconcile", staticmethod(lambda _: prior))
    monkeypatch.setattr(app, "recover_invocation", lambda _: retained)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    calls = []
    saved = None

    async def invoke(item_id, stage, role, prompt, **kwargs):
        nonlocal saved
        if saved is None:
            calls.append((item_id, stage, role, prompt, kwargs))
            saved = {
                "invocation_id": "replacement-policy",
                "text": json.dumps(
                    {
                        "source_revision": app.provider.source_revision(),
                        "reason": "Canonical project authority",
                        "policy": {
                            "eligible": True,
                            "mode": "SOLO",
                            "primary_branch": "main",
                            "evidence": policy_evidence(provider.repository),
                        },
                    }
                ),
            }
        return saved

    monkeypatch.setattr(app, "invoke", invoke)
    assert asyncio.run(app.reassess_policy(envelope))["eligible"] is True
    accepted = json.loads(app.provider.cache_path.read_text())
    assert len(calls) == 1 and calls[0][0] == "provider-policy"
    assert accepted["invocation_id"] == retained["invocation_id"]
    assert accepted["items"][0]["content"] == provider.item("item-one").content
    assert accepted["items"][0]["revision"] == provider.item("item-one").revision
    assert accepted["policy_reassessment"]["invocation_id"] == "replacement-policy"
    app.provider.validate_policy(accepted["policy"])
    assert asyncio.run(app.reassess_policy(envelope))["eligible"] is True
    assert len(calls) == 1


def test_retained_policy_reassessment_requires_existing_nonmutating_terminal_report(
    config_file, provider, monkeypatch
):
    import asyncio
    import json

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceStore, atomic_json

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    envelope, _, prior, _ = retained_provider_observation(app, provider)
    evidence = provider.evidence_root / "runs/retained-observation"
    (evidence / "telemetry-report.json").unlink()
    original = {"stale": "preserved"}
    atomic_json(app.provider.cache_path, original)
    before = {path: path.read_bytes() for path in evidence.iterdir() if path.is_file()}
    monkeypatch.setattr(EvidenceStore, "reconcile", staticmethod(lambda _: prior))
    recovered = []
    invoked = []
    monkeypatch.setattr(app, "recover_invocation", lambda _: recovered.append(True))

    async def invoke(*args, **kwargs):
        invoked.append((args, kwargs))

    monkeypatch.setattr(app, "invoke", invoke)
    with pytest.raises(TransitionBlocked, match="terminal evidence"):
        asyncio.run(app.reassess_policy(envelope))
    assert not recovered and not invoked
    assert json.loads(app.provider.cache_path.read_text()) == original
    assert {path: path.read_bytes() for path in evidence.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("fault", ["path", "action", "recovered"])
def test_retained_policy_reassessment_rejects_mismatched_observation_identity(
    config_file, provider, monkeypatch, fault
):
    import asyncio
    import json

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import EvidenceStore, atomic_json

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    envelope, retained, prior, _ = retained_provider_observation(app, provider)
    original = {"stale": "preserved"}
    atomic_json(app.provider.cache_path, original)
    if fault == "path":
        changed = envelope.with_name("different-observation.json")
        changed.write_bytes(envelope.read_bytes())
        envelope = changed
    elif fault == "action":
        prior = {**prior, "action": "different-observation"}
    else:
        retained = {**retained, "invocation_id": "different-invocation"}
    monkeypatch.setattr(EvidenceStore, "reconcile", staticmethod(lambda _: prior))
    monkeypatch.setattr(app, "recover_invocation", lambda _: retained)
    invoked = []

    async def invoke(*args, **kwargs):
        invoked.append((args, kwargs))

    monkeypatch.setattr(app, "invoke", invoke)
    with pytest.raises(TransitionBlocked, match="observation"):
        asyncio.run(app.reassess_policy(envelope))
    assert not invoked
    assert json.loads(app.provider.cache_path.read_text()) == original


@pytest.mark.parametrize("drift", ["observer", "capability", "binding"])
def test_retained_policy_reassessment_rejects_midcall_authority_drift(
    config_file, provider, monkeypatch, drift
):
    import asyncio
    import json

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.contracts import load_config
    from backlog_harness.evidence import EvidenceStore, atomic_json

    config, data = config_file
    data.update(
        repository=str(provider.repository),
        operational_root=str(provider.evidence_root),
        provider_interaction="agent",
    )
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    envelope, retained, prior, _ = retained_provider_observation(app, provider)
    original = {"stale": "preserved"}
    atomic_json(app.provider.cache_path, original)
    monkeypatch.setattr(EvidenceStore, "reconcile", staticmethod(lambda _: prior))
    monkeypatch.setattr(app, "recover_invocation", lambda _: retained)
    monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    original_observer = app.provider_observer_digest
    original_capability = app.provider_capability_digest
    current = load_config(config)
    stable_observer = original_observer(current)
    stable_capability = original_capability(current, stable_observer)
    observer_calls = []
    capability_calls = []

    if drift == "observer":
        monkeypatch.setattr(
            app,
            "provider_observer_digest",
            lambda current: (
                observer_calls.append(True)
                or (original_observer(current) if len(observer_calls) == 1 else "changed-observer")
            ),
        )
    elif drift == "capability":
        monkeypatch.setattr(
            app,
            "provider_capability_digest",
            lambda current, observer: (
                capability_calls.append(True)
                or (
                    original_capability(current, observer)
                    if len(capability_calls) == 1
                    else "changed-capability"
                )
            ),
        )

    async def invoke(*args, **kwargs):
        if drift == "binding":
            changed = yaml.safe_load(config.read_text())
            changed["profiles"]["control"]["permissions"] = ["workspace-write"]
            config.write_text(yaml.safe_dump(changed))
            monkeypatch.setattr(
                app, "provider_capability_digest", lambda current, observer: stable_capability
            )
        return {
            "invocation_id": "replacement-policy",
            "text": json.dumps(
                {
                    "source_revision": app.provider.source_revision(),
                    "reason": "Canonical project authority",
                    "policy": {
                        "eligible": True,
                        "mode": "SOLO",
                        "primary_branch": "main",
                        "evidence": policy_evidence(provider.repository),
                    },
                }
            ),
        }

    monkeypatch.setattr(app, "invoke", invoke)
    with pytest.raises(TransitionBlocked, match="changed during policy reassessment"):
        asyncio.run(app.reassess_policy(envelope))
    assert json.loads(app.provider.cache_path.read_text()) == original


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("confirmed_operation", ["new", "assess"])
def test_admission_uses_current_dependencies_and_retains_clarification(
    config_file, provider, monkeypatch, confirmed_operation, legacy
):
    import asyncio
    import json
    from dataclasses import asdict, replace

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    predecessor = replace(
        item,
        item_id="predecessor",
        state="Completed",
        content="Status: Completed\nAccepted mapping authorizes Phase 2.",
    )
    monkeypatch.setattr(
        app.provider, "item", lambda key: item if key == item.item_id else predecessor
    )
    monkeypatch.setattr(
        app.provider, "observation", lambda: {"dependencies": {item.item_id: ["predecessor"]}}
    )
    monkeypatch.setattr(app, "reconcile", list)
    monkeypatch.setattr(app, "execution_policy", lambda _: {})
    monkeypatch.setattr(app, "validate_call_limits", lambda *_: None)
    session = {
        "session_id": "coordinator",
        "native_session_id": "native",
        "binding": asdict(app.config.binding("coordinator")),
    }
    calls = []

    async def invoke(item_id, stage, role, prompt, **kwargs):
        calls.append(stage)
        if stage == "admit-confirmed":
            assert kwargs["session"].session_id == session["session_id"]
        assert '"state": "Completed"' in prompt
        assert "Accepted mapping authorizes Phase 2" in prompt
        value = {
            "operation": "assess" if stage == "admit" else confirmed_operation,
            "item_id": item_id,
            "provider_revision": item.revision,
            "reason": "Current predecessor proof",
        }
        result = {
            "invocation_id": stage,
            "outcome": "returned",
            "session": session,
            "binding": session["binding"],
            "role": role,
            "text": json.dumps(value),
        }
        atomic_json(app._stage_path(item_id, stage), result)
        return result

    async def transition(item_id, revision, target, authority, **kwargs):
        assert target == "Starting" and authority["invocation_id"] == "admit-confirmed"
        raise RuntimeError("verified clarified admission")

    original_bytes = None
    if legacy:
        original = {
            "invocation_id": "old-admit",
            "request_digest": "old-prompt-digest",
            "session": session,
            "binding": session["binding"],
            "role": "coordinator",
            "outcome": "returned",
            "text": json.dumps(
                {"operation": "assess", "item_id": item.item_id, "provider_revision": item.revision}
            ),
        }
        atomic_json(app._stage_path(item.item_id, "admit"), original)
        original_bytes = app._stage_path(item.item_id, "admit").read_bytes()
        monkeypatch.setattr(app, "validate_invocation_result", lambda _: None)
    monkeypatch.setattr(app, "invoke", invoke)
    monkeypatch.setattr(app, "transition", transition)
    with pytest.raises(
        (RuntimeError, TransitionBlocked),
        match="verified clarified admission"
        if confirmed_operation == "new"
        else "No current Coordinator admission",
    ):
        asyncio.run(app._run_item(item.item_id))
    assert calls == (["admit-confirmed"] if legacy else ["admit", "admit-confirmed"])
    if legacy:
        assert app._stage_path(item.item_id, "admit").read_bytes() == original_bytes
    assert app.admission_path(item.item_id).name == "admit-confirmed.json"
    changed = json.loads(app.admission_path(item.item_id).read_text())
    changed["session"] = {**session, "session_id": "other"}
    atomic_json(app._stage_path(item.item_id, "admit-confirmed"), changed)
    with pytest.raises(TransitionBlocked, match="clarification identity"):
        app.admission_path(item.item_id)


def test_helper_discovery_uses_provider_fingerprint_after_workflow_change(
    config_file, provider, monkeypatch, tmp_path
):
    from pathlib import Path

    import yaml

    from backlog_harness.application import Application

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    home = tmp_path / "native-home"
    home.mkdir()
    (home / "config.toml").write_text("")
    data["agent_clis"]["primary"]["adapter_options"] = {
        "codex_home": str(home),
        "load_user_config": True,
    }
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    project = provider.repository / "PROJECT.yaml"
    value = yaml.safe_load(project.read_text())
    value["resource_coordination"] = {"selected": "resource-claim"}
    value["agent_claim_transport"] = {"selected": "mcp"}
    project.write_text(yaml.safe_dump(value))
    for name in [
        "manage-work-items",
        "manage-work-items-file",
        "resource-claim",
        "resource-claim-helper",
        "resource-claim-helper-mcp",
    ]:
        skill = Path(data["methodology_root"]) / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("Fixture skill")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    observer = app.provider_observer_digest(app.config)
    observed = {
        "observer_digest": observer,
        "capability_digest": app.provider_capability_digest(app.config, observer),
        "helper": {
            "discovery": "native_tool_catalog",
            "available": True,
            "tools": ["claim_acquire", "claim_release", "claim_heartbeat", "claim_status"],
        },
    }
    import asyncio
    import json
    from dataclasses import asdict

    from test_telemetry import payload

    from backlog_harness.evidence import atomic_json
    from backlog_harness.telemetry import Sink

    item = provider.item("item-one")
    predecessor_path = provider.repository / "backlog/feature-backlog/predecessor.md"
    predecessor_path.write_text(
        item.content.replace("item-one", "predecessor").replace(
            "Status: Ready", "Status: Completed"
        )
    )
    predecessor = provider.item("predecessor")
    observed.update(
        source_revision=app.provider.source_revision(),
        source_manifest=app.provider.source_manifest(),
        policy={
            "eligible": True,
            "mode": "SOLO",
            "primary_branch": "main",
            "evidence": policy_evidence(provider.repository),
        },
        items=[asdict(item), asdict(predecessor)],
        dependencies={item.item_id: [predecessor.item_id], predecessor.item_id: []},
    )
    atomic_json(app.provider.cache_path, observed)
    telemetry_path = app.root / "retained-admission-telemetry.jsonl"
    sink = Sink(telemetry_path, {})
    sink.write(payload())
    binding = asdict(app.config.binding("coordinator"))
    receipt = {
        "invocation_id": "old-admission",
        "role": "coordinator",
        "outcome": "returned",
        "session": {"session_id": "coordinator", "native_session_id": "native", "binding": binding},
        "binding": binding,
        "telemetry_path": str(telemetry_path),
        "telemetry": {
            "span_count": 1,
            "rejected_exports": 0,
            "evidence_sha256": sink.evidence_digest(),
        },
        "events": [{"type": "turn.completed", "usage": {"output_tokens": 10}}],
        "text": json.dumps(
            {"operation": "assess", "item_id": item.item_id, "provider_revision": item.revision}
        ),
        "request_digest": "historical-prompt",
    }
    admit = app._stage_path(item.item_id, "admit")
    atomic_json(admit, receipt)
    original = admit.read_bytes()
    external_calls = []

    async def invoke(item_id, stage, role, prompt, **kwargs):
        external_calls.append(stage)
        assert stage == "admit-confirmed"
        assert kwargs["session"].native_session_id == "native"
        assert '"state": "Completed"' in prompt
        raise RuntimeError("reached retained-session clarification")

    monkeypatch.setattr(app, "invoke", invoke)
    data["workflow"]["allowed_paths"] = ["updated.py"]
    config.write_text(yaml.safe_dump(data))
    app.validate_management_readiness("coordinator")
    app = Application(config)
    monkeypatch.setattr(app, "invoke", invoke)
    with pytest.raises(RuntimeError, match="reached retained-session clarification"):
        asyncio.run(app.run_item(item.item_id))
    assert external_calls == ["admit-confirmed"]
    assert admit.read_bytes() == original
    assert provider.item(item.item_id).state == "Ready"
    data["profiles"]["control"]["model"] = "different-model"
    config.write_text(yaml.safe_dump(data))
    app.validate_management_readiness("coordinator")
    (home / "config.toml").write_text("[mcp_servers.changed]\ncommand = 'changed'\n")
    with pytest.raises(TransitionBlocked, match="discovery configuration is stale"):
        app.validate_management_readiness("coordinator")


@pytest.mark.parametrize("changed_word", [False, True])
def test_policy_excerpt_allows_wrapping_but_not_different_words(provider, changed_word):
    from hashlib import sha256

    from backlog_harness.provider import AgentProvider

    source = provider.repository / "policy.md"
    source.write_text("Independent Ready work is permitted\nunder SOLO execution.\n")
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": [
            {
                "path": "policy.md",
                "sha256": sha256(source.read_bytes()).hexdigest(),
                "excerpt": "Independent Ready work is "
                + ("prohibited" if changed_word else "permitted")
                + " under SOLO execution.",
                "supports": ["mode", "admission"],
            }
        ],
    }
    view = AgentProvider(provider.repository, provider.evidence_root)
    if changed_word:
        with pytest.raises(TransitionBlocked, match="excerpt is absent"):
            view.validate_policy(policy)
    else:
        view.validate_policy(policy)
        source.write_text(source.read_text() + "Changed authority\n")
        with pytest.raises(TransitionBlocked, match="Stale policy evidence"):
            view.validate_policy(policy)


@pytest.mark.parametrize(
    "fault", [None, "missing-authority", "missing-exemption", "stale", "not-boolean"]
)
def test_claim_exemption_requires_current_explicit_authority(provider, fault):
    from hashlib import sha256

    from backlog_harness.provider import AgentProvider

    source = provider.repository / "crisis.md"
    source.write_text(
        "Epoch recovery-one is active. Agents must not use claims during this epoch.\n"
    )
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "claims_required": False,
        "claim_exemption": "Active recovery-one epoch prohibits claims",
        "evidence": policy_evidence(provider.repository)
        + [
            {
                "path": "crisis.md",
                "sha256": sha256(source.read_bytes()).hexdigest(),
                "excerpt": source.read_text().strip(),
                "supports": ["coordination"],
            }
        ],
    }
    view = AgentProvider(provider.repository, provider.evidence_root)
    if fault == "missing-authority":
        policy["evidence"] = policy["evidence"][:1]
    elif fault == "missing-exemption":
        del policy["claim_exemption"]
    elif fault == "stale":
        source.write_text("Epoch recovery-one ended.\n")
    elif fault == "not-boolean":
        policy["claims_required"] = "false"
    if fault:
        with pytest.raises(TransitionBlocked):
            view.validate_policy(policy)
    else:
        view.validate_policy(policy)


def test_claim_free_management_ignores_irrelevant_helper_but_preserves_policy_gate(
    config_file, provider
):
    from hashlib import sha256
    from pathlib import Path

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    data["profiles"]["control"]["permissions"] = ["workspace-write"]
    config.write_text(yaml.safe_dump(data))
    for name in ("manage-work-items", "manage-work-items-file"):
        skill = Path(data["methodology_root"]) / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("Management skill fixture\n")
    project = provider.repository / "PROJECT.yaml"
    value = yaml.safe_load(project.read_text())
    value["resource_coordination"] = {"selected": "resource-claim"}
    project.write_text(yaml.safe_dump(value))
    source = provider.repository / "crisis.md"
    source.write_text("Active recovery-one epoch is claim-free.\n")
    app = Application(config)
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "claims_required": False,
        "claim_exemption": "Active recovery-one epoch",
        "evidence": policy_evidence(provider.repository)
        + [
            {
                "path": "crisis.md",
                "sha256": sha256(source.read_bytes()).hexdigest(),
                "excerpt": source.read_text().strip(),
                "supports": ["coordination"],
            }
        ],
    }
    observation = {
        "source_revision": app.provider.source_revision(),
        "policy": policy,
        "observer_digest": "stale-unrelated-helper",
        "helper": {"available": False},
    }
    atomic_json(app.provider.cache_path, observation)
    app.validate_management_readiness("coordinator")
    # SOLO by itself retains the default claim capability requirements.
    del policy["claims_required"]
    atomic_json(app.provider.cache_path, observation)
    with pytest.raises(TransitionBlocked, match="Selected claim helper"):
        app.validate_management_readiness("coordinator")
    policy["claims_required"] = False
    policy["evidence"][-1]["sha256"] = "stale"
    atomic_json(app.provider.cache_path, observation)
    with pytest.raises(TransitionBlocked, match="Stale policy evidence"):
        app.validate_management_readiness("coordinator")


@pytest.mark.parametrize("fault", [None, "changed-decision", "stale-authority", "missing-field"])
def test_repeated_policy_citation_reuses_only_current_matching_authority(
    config_file, provider, tmp_path, fault
):
    import copy

    import yaml

    from backlog_harness.application import Application
    from backlog_harness.evidence import atomic_json

    config, data = config_file
    data.update(repository=str(provider.repository), provider_interaction="agent")
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    previous = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": policy_evidence(provider.repository),
    }
    atomic_json(app.provider.cache_path, {"policy": previous})
    returned = copy.deepcopy(previous)
    returned["evidence"][0]["excerpt"] = "Invented concatenation of separated lines"
    receipt = {"operation": "original-operation", "commit": "original-commit", "policy": returned}
    if fault == "changed-decision":
        returned["eligible"] = False
    elif fault == "missing-field":
        del returned["eligible"]
    elif fault == "stale-authority":
        project = provider.repository / "PROJECT.yaml"
        project.write_text(project.read_text() + "# changed source\n")
    evidence = tmp_path / "original-effect"
    if fault:
        with pytest.raises(TransitionBlocked):
            app.validate_receipt_policy(evidence, receipt)
        assert not (evidence / "policy-reconciliation.json").exists()
    else:
        original = copy.deepcopy(receipt)
        app.validate_receipt_policy(evidence, receipt)
        assert receipt["policy"] == previous
        import json

        assert json.loads((evidence / "receipt-original-policy.json").read_text()) == original
        proof = (evidence / "policy-reconciliation.json").read_bytes()
        app.validate_receipt_policy(evidence, original)
        assert (evidence / "policy-reconciliation.json").read_bytes() == proof
        altered = copy.deepcopy(receipt)
        altered["policy"]["evidence"][0]["excerpt"] = "Different invalid receipt"
        with pytest.raises(TransitionBlocked, match="Original policy receipt changed"):
            app.validate_receipt_policy(evidence, altered)


@pytest.mark.parametrize("changed", ["source", "provider", "policy"])
def test_head_drift_reuses_only_unchanged_provider_and_policy(provider, changed):
    import json
    from dataclasses import asdict
    from hashlib import sha256

    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import AgentProvider

    repo = provider.repository
    extra = repo / "policy.md"
    extra.write_text("Current policy authority\n")
    git(repo, "add", "policy.md")
    git(repo, "commit", "-m", "Policy source")
    view = AgentProvider(repo, provider.evidence_root)
    policy = {
        "eligible": True,
        "mode": "SOLO",
        "primary_branch": "main",
        "evidence": policy_evidence(repo)
        + [
            {
                "path": "policy.md",
                "sha256": sha256(extra.read_bytes()).hexdigest(),
                "excerpt": "Current policy authority",
                "supports": ["admission"],
            }
        ],
    }
    cached = {
        "source_revision": view.source_revision(),
        "source_manifest": view.source_manifest(),
        "items": [asdict(provider.item("item-one"))],
        "dependencies": {"item-one": []},
        "policy": policy,
        "observer_digest": "retained-observer",
    }
    atomic_json(view.cache_path, cached)
    original = view.cache_path.read_bytes()
    path = (
        repo / "answer.txt"
        if changed == "source"
        else extra
        if changed == "policy"
        else repo / "backlog/feature-backlog/item-one.md"
    )
    path.write_text(path.read_text() + "Changed\n" if path.exists() else "Source change\n")
    git(repo, "add", "--", str(path.relative_to(repo)))
    git(repo, "commit", "-m", "Concurrent change")
    if changed == "source":
        observation = view.observation()
        assert observation["source_revision"] == view.source_revision()
        assert observation["items"] == cached["items"]
        assert observation["observer_digest"] == "retained-observer"
    else:
        with pytest.raises(TransitionBlocked):
            view.observation()
    assert view.cache_path.read_bytes() == original
    assert json.loads(original) == cached
