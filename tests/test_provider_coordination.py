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
    data["profiles"]["control"]["model"] = "updated-observer"
    config.write_text(yaml.safe_dump(data))
    asyncio.run(app.refresh_provider())
    assert len(calls) == 2
    with pytest.raises(TransitionBlocked, match="responsible agent"):
        app.provider.transition(item.item_id, item.revision, "Starting", {})
    (provider.repository / item.path).write_text(item.content + "\nchanged\n")
    with pytest.raises(TransitionBlocked, match="stale"):
        app.provider.snapshot()
    asyncio.run(app.refresh_provider())
    assert app.provider.item(item.item_id).content.endswith("changed\n")


def test_agent_transition_recovers_committed_operation_without_second_mutation(
    config_file, provider, monkeypatch
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
    with pytest.raises(TransitionBlocked, match="Structured"):
        view.validate_policy({**policy, "evidence": "trust the agent"})
    with pytest.raises(TransitionBlocked, match="Stale"):
        view.validate_policy({**policy, "evidence": [{**policy["evidence"][0], "sha256": "stale"}]})
    with pytest.raises(TransitionBlocked, match="execution mode differs"):
        view.validate_policy({**policy, "mode": "MULTITASK"})


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
