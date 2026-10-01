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
    assert len(calls) == 2
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
        with pytest.raises(TransitionBlocked, match="Structured policy evidence"):
            asyncio.run(
                app.transition(
                    before.item_id, before.revision, "Starting", actor, validate=validate_transition
                )
            )
        receipt = json.loads((request_path.parent / "receipt.json").read_text())
        assert receipt["advancement_verified"] is False
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


@pytest.mark.parametrize("case", ["valid", "missing", "stale", "item_changed"])
def test_policy_reassessment_requires_current_authority(config_file, provider, monkeypatch, case):
    import asyncio
    import json

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
    revision = app.provider.source_revision()
    evidence = policy_evidence(provider.repository)
    original = {
        "source_revision": revision,
        "policy": {
            "eligible": False,
            "mode": "SOLO",
            "primary_branch": "main",
            "evidence": evidence,
        },
        "items": [],
        "questions": {"waiting": {"question_id": "q1"}},
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
    observed = {
        "observer_digest": app.provider_observer_digest(app.config),
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
