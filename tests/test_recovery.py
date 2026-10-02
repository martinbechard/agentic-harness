import subprocess
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_provider_coordination import authority

from backlog_harness.application import Application
from backlog_harness.contracts import digest, load_config
from backlog_harness.delivery import integrate
from backlog_harness.evidence import EvidenceStore, JsonlWriter, atomic_json, component
from backlog_harness.provider import TransitionBlocked, git
from backlog_harness.workflow import validate_transition


def test_provider_commit_without_receipt_recovers_without_second_commit(provider):
    item = provider.item("item-one")
    actor = authority("coordinator", operation="new")
    receipt = provider.transition(
        item.item_id, item.revision, "Starting", actor, validate=validate_transition
    )
    operation = digest([item.item_id, item.revision, "Starting", actor])
    record = provider.evidence_root / "provider-operations" / component(operation)
    (record / "receipt.json").unlink()
    head = git(provider.repository, "rev-parse", "HEAD")
    assert (
        provider.transition(
            item.item_id, item.revision, "Starting", actor, validate=validate_transition
        )
        == receipt
    )
    assert git(provider.repository, "rev-parse", "HEAD") == head


def test_submitted_unobserved_invocation_is_not_relaunched(config_file, tmp_path):
    import asyncio

    config, _ = config_file
    app = Application(config)
    snapshot = load_config(config)
    binding = snapshot.binding("orchestrator")
    store = EvidenceStore(app.root, "item:one")
    path = store.begin(
        "one:accept",
        "old-invocation",
        snapshot,
        binding,
        action="accept",
        item_id="one",
        request_digest=digest("prompt"),
    )
    EvidenceStore.requested(path)
    with pytest.raises(TransitionBlocked, match="replacement is prohibited"):
        asyncio.run(app.invoke("one", "accept", "orchestrator", "prompt"))
    assert len(list(store.run.glob("operations/*/invocations/*/intent.json"))) == 1


def test_returned_observations_rebuild_missing_stage(config_file, tmp_path):
    config, _ = config_file
    app = Application(config)
    snapshot = load_config(config)
    binding = snapshot.binding("orchestrator")
    store = EvidenceStore(app.root, "item:one")
    path = store.begin(
        "one:accept",
        "observed-invocation",
        snapshot,
        binding,
        action="accept",
        item_id="one",
        request_digest=digest("prompt"),
    )
    EvidenceStore.requested(path)
    EvidenceStore.session(
        path, {"session_id": "portable", "native_session_id": "native", "binding": asdict(binding)}
    )
    JsonlWriter(path / "events.jsonl").append(
        {"type": "item.completed", "item_type": "agent_message", "text": '{"accepted":true}'}
    )
    JsonlWriter(path / "events.jsonl").append(
        {"type": "turn.completed", "usage": {"output_tokens": 1}}
    )
    telemetry = app.root / "telemetry/observed/spans.jsonl"
    JsonlWriter(telemetry).append(
        {
            "resourceSpans": [
                {"scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": "b" * 16}]}]}
            ]
        }
    )
    atomic_json(path / "telemetry.json", {"path": str(telemetry)})
    from backlog_harness.telemetry import Sink

    atomic_json(
        path / "telemetry-report.json",
        {
            "span_count": 1,
            "rejected_exports": 0,
            "evidence_sha256": Sink(telemetry, {}).evidence_digest(),
        },
    )
    EvidenceStore.outcome(path, "returned")
    recovered = app.recover_invocation(path)
    assert recovered["session"]["session_id"] == "portable"
    assert recovered["text"] == '{"accepted":true}'
    assert recovered["invocation_id"] == "observed-invocation"


@pytest.mark.parametrize("untracked", [False, True])
@pytest.mark.parametrize(
    "crash",
    [
        "before_merge",
        "after_merge",
        "after_checks",
        "after_checks_primary_advanced",
        "collision",
        "exact_collision",
        "ancestor_collision",
    ],
)
def test_delivery_crash_boundaries_reconcile_once(
    provider, tmp_path, monkeypatch, crash, untracked
):
    primary = provider.repository
    candidate_repo = tmp_path / "candidate"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(primary), str(candidate_repo)],
        check=True,
        capture_output=True,
    )
    git(candidate_repo, "config", "user.name", "Test")
    git(candidate_repo, "config", "user.email", "test@example.invalid")
    base = git(candidate_repo, "rev-parse", "HEAD")
    candidate_path = "parent/answer.txt" if crash == "ancestor_collision" else "answer.txt"
    (candidate_repo / candidate_path).parent.mkdir(parents=True, exist_ok=True)
    (candidate_repo / candidate_path).write_text("42\n")
    git(candidate_repo, "add", candidate_path)
    git(candidate_repo, "commit", "-m", "Candidate")
    candidate = git(candidate_repo, "rev-parse", "HEAD")
    review = {
        "candidate": candidate,
        "producer_session": "producer",
        "reviewer_session": "reviewer",
        "verdict": "ACCEPT",
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": "review",
        "unresolved_findings": [],
    }
    checks = [
        {"candidate": candidate, "argv": ["test"], "returncode": 0, "evidence_sha256": "checks"}
    ]
    root = tmp_path / "stages"

    def stage(item, name):
        return root / (name + ".json")

    def check(repo, item, commit, name):
        result = [{**checks[0], "candidate": commit}]
        atomic_json(stage(item, name), result)
        if (
            crash in {"after_checks", "after_checks_primary_advanced"}
            and not stage(item, "crashed").exists()
        ):
            atomic_json(stage(item, "crashed"), True)
            raise RuntimeError("crash after checks")
        return result

    app = SimpleNamespace(
        provider=provider,
        config=SimpleNamespace(
            repository=primary, data={"workflow": {"allowed_paths": [candidate_path]}}
        ),
        _stage_path=stage,
        checks=check,
    )
    from backlog_harness import delivery

    native_git = delivery.git
    failed = False

    def fault_git(repo, *argv):
        nonlocal failed
        if argv[0] == "merge" and not failed and crash in {"before_merge", "after_merge"}:
            failed = True
            if crash == "after_merge":
                native_git(repo, *argv)
            raise RuntimeError("crash at merge")
        return native_git(repo, *argv)

    monkeypatch.setattr(delivery, "git", fault_git)
    if untracked:
        (primary / "existing-plan.md").write_text("preserve this user plan\n")
        (primary / "existing-link").symlink_to("existing-plan.md")
    if crash in {"collision", "exact_collision", "ancestor_collision"}:
        if crash == "collision":
            (primary / "answer.txt").mkdir()
            (primary / "answer.txt/user.md").write_text("user work")
        elif crash == "exact_collision":
            (primary / "answer.txt").write_text("user work")
        else:
            (primary / "parent").write_text("user work")
        with pytest.raises(TransitionBlocked, match="overlap"):
            integrate(app, "item-one", candidate_repo, candidate, base, review, checks)
        assert git(primary, "rev-parse", "HEAD") == base
        return
    with pytest.raises(RuntimeError, match="crash"):
        integrate(app, "item-one", candidate_repo, candidate, base, review, checks)
    advanced = None
    if crash == "after_checks_primary_advanced":
        (primary / "unrelated.txt").write_text("Independent later work\n")
        git(primary, "add", "unrelated.txt")
        git(primary, "commit", "-m", "Independent later work")
        advanced = git(primary, "rev-parse", "HEAD")
    result = integrate(app, "item-one", candidate_repo, candidate, base, review, checks)
    assert result["disposition"] == "READY"
    if untracked:
        assert (primary / "existing-plan.md").read_text() == "preserve this user plan\n"
        assert (primary / "existing-link").is_symlink()
        assert set(result["preserved_untracked"]) == {"existing-plan.md", "existing-link"}
    commits = git(primary, "rev-list", "--first-parent", base + "..HEAD").splitlines()
    assert commits == ([advanced] if advanced else []) + [result["main_commit"]]
    assert integrate(app, "item-one", candidate_repo, candidate, base, review, checks) == result
    if untracked:
        (primary / "existing-plan.md").write_text("changed externally\n")
        with pytest.raises(TransitionBlocked, match="untracked files changed"):
            integrate(app, "item-one", candidate_repo, candidate, base, review, checks)


@pytest.mark.parametrize(
    "report", [{"span_count": 0, "rejected_exports": 0}, {"span_count": 1, "rejected_exports": 1}]
)
def test_saved_bad_telemetry_never_advances_on_retry(config_file, report):
    import asyncio

    config, _ = config_file
    app = Application(config)
    atomic_json(
        app._stage_path("one", "accept"),
        {"request_digest": digest("prompt"), "outcome": "returned", "telemetry": report},
    )
    for _ in range(2):
        with pytest.raises(TransitionBlocked, match="Telemetry"):
            asyncio.run(app.invoke("one", "accept", "orchestrator", "prompt"))
    assert not list((app.root / "runs").glob("*/operations/*/invocations/*/intent.json"))


def test_crash_after_answer_commit_resumes_exact_operation(config_file, provider, monkeypatch):
    import asyncio
    import json

    import yaml

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    item = provider.item(item.item_id)
    canonical = authority(
        "orchestrator", accepted=True, session_id="canonical", native_session_id="native"
    )
    provider.transition(
        item.item_id, item.revision, "Running", canonical, validate=validate_transition
    )
    item = provider.item(item.item_id)
    provider.transition(
        item.item_id,
        item.revision,
        "User Action Required",
        {**canonical, "question": {"question_id": "q1", "text": "Proceed?"}},
        validate=validate_transition,
    )
    question = provider.item(item.item_id)
    atomic_json(
        app._stage_path(item.item_id, "accept"),
        {
            "session": {
                "session_id": "canonical",
                "native_session_id": "native",
                "binding": asdict(app.config.binding("orchestrator")),
            }
        },
    )
    monkeypatch.setattr(app, "guard", lambda name: None)
    calls = []

    async def crash(*args, **kwargs):
        raise RuntimeError("crash after persisted answer")

    monkeypatch.setattr(app, "invoke", crash)
    with pytest.raises(RuntimeError, match="persisted"):
        asyncio.run(app.answer(item.item_id, "q1", question.revision, "yes"))
    persisted_head = git(provider.repository, "rev-parse", "HEAD")

    async def classify(item_id, stage, role, prompt, **kwargs):
        calls.append(kwargs["session"].session_id)
        request = json.loads(prompt.split("\n")[-1])
        return {
            "role": role,
            "outcome": "returned",
            "invocation_id": "classification",
            "session": {"session_id": "canonical", "native_session_id": "native"},
            "text": json.dumps(
                {
                    "question_id": "q1",
                    "answer_digest": request["answer"]["digest"],
                    "disposition": "approve",
                }
            ),
        }

    monkeypatch.setattr(app, "invoke", classify)
    result = asyncio.run(app.resume_answer(item.item_id))
    assert result["state"] == "Running"
    assert calls == ["canonical"]
    assert git(provider.repository, "rev-parse", "HEAD^") == persisted_head
    assert provider.item(item.item_id).owner == "canonical"


def test_call_budget_blocks_advancement_when_observed_output_exceeds_limit():
    result = {"events": [{"type": "turn.completed", "usage": {"output_tokens": 201}}]}
    with pytest.raises(TransitionBlocked, match="Generated tokens"):
        Application.validate_call_limits(result, {"turns": 1, "generated_tokens": 200})
    with pytest.raises(TransitionBlocked, match="turn count"):
        Application.validate_call_limits(
            {"events": result["events"] * 2}, {"turns": 1, "generated_tokens": 1000}
        )
    Application.validate_call_limits(result, {"turns": 1, "generated_tokens": 300})


def test_original_estimate_cannot_be_reset_by_provider_edit(config_file, provider):
    import yaml

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    atomic_json(
        app._stage_path(item.item_id, "assignment"), {"content": item.content, "original_high": 100}
    )
    path = provider.repository / item.path
    path.write_text(
        path.read_text().replace(
            "Original High Generated Tokens: 100", "Original High Generated Tokens: 9999"
        )
    )
    git(provider.repository, "add", "--", item.path)
    git(provider.repository, "commit", "-m", "Change current estimate")
    assert app.usage_view(item.item_id)["original_high"] == 100
    assert app.usage_view(item.item_id)["ceiling"] == 200


@pytest.mark.parametrize("boundary", ["after_request", "before_commit"])
def test_prepared_provider_recovery_is_explicit_and_commits_once(provider, monkeypatch, boundary):
    from backlog_harness import provider as module

    item = provider.item("item-one")
    actor = authority("coordinator", operation="new")
    initial = git(provider.repository, "rev-parse", "HEAD")
    native_json, native_git = module.atomic_json, module.git

    def crash_json(path, value, **kwargs):
        native_json(path, value, **kwargs)
        if path.name == "requested.json" and boundary == "after_request":
            raise RuntimeError("crash after durable request")

    def crash_git(repo, *args):
        if args[0] == "commit" and boundary == "before_commit":
            raise RuntimeError("crash before commit")
        return native_git(repo, *args)

    monkeypatch.setattr(module, "atomic_json", crash_json)
    monkeypatch.setattr(module, "git", crash_git)
    with pytest.raises(RuntimeError, match="crash"):
        provider.transition(
            item.item_id, item.revision, "Starting", actor, validate=validate_transition
        )
    monkeypatch.setattr(module, "atomic_json", native_json)
    monkeypatch.setattr(module, "git", native_git)
    record = next((provider.evidence_root / "provider-operations").glob("*/requested.json")).parent
    with pytest.raises(TransitionBlocked, match="no unique"):
        provider.reconcile_operation(record)
    assert git(provider.repository, "rev-parse", "HEAD") == initial
    receipt = provider.recover_prepared(record, validate=validate_transition)
    assert provider.item(item.item_id).state == "Starting"
    assert git(provider.repository, "rev-parse", "HEAD^") == initial
    assert provider.recover_prepared(record, validate=validate_transition) == receipt
    assert git(provider.repository, "rev-parse", "HEAD") == receipt["provider_commit"]


@pytest.mark.parametrize("fault", ["bytes", "identity", "merge", "unrelated", "symlink", "index"])
def test_prepared_provider_recovery_rejects_conflicting_bytes(provider, monkeypatch, fault):
    from backlog_harness import provider as module

    item = provider.item("item-one")
    native_git = module.git

    def crash_git(repo, *args):
        if args[0] == "commit":
            raise RuntimeError("crash before commit")
        return native_git(repo, *args)

    monkeypatch.setattr(module, "git", crash_git)
    with pytest.raises(RuntimeError):
        provider.transition(
            item.item_id,
            item.revision,
            "Starting",
            authority("coordinator", operation="new"),
            validate=validate_transition,
        )
    monkeypatch.setattr(module, "git", native_git)
    record = next((provider.evidence_root / "provider-operations").glob("*/requested.json")).parent
    head = git(provider.repository, "rev-parse", "HEAD")
    import json

    source = provider.repository / item.path
    message = "bytes conflict"
    if fault == "bytes":
        source.write_text("unrelated replacement")
    elif fault == "identity":
        request = json.loads((record / "requested.json").read_text())
        request["item_id"] = "other"
        atomic_json(record / "requested.json", request)
        message = "identity or bytes changed"
    elif fault == "merge":
        (provider.repository / ".git/MERGE_HEAD").write_text(head + "\n")
        message = "unresolved merge"
    elif fault == "unrelated":
        (provider.repository / "unrelated.txt").write_text("preserve this")
        message = "Unrelated checkout changes"
    elif fault == "symlink":
        source.unlink()
        source.symlink_to(provider.repository / "PROJECT.yaml")
        message = "Unsafe prepared provider path"
    else:
        intended = source.read_bytes()
        source.write_text("different staged work")
        git(provider.repository, "add", "--", item.path)
        source.write_bytes(intended)
        message = "index bytes conflict"
    before = git(provider.repository, "status", "--porcelain")
    with pytest.raises(TransitionBlocked, match=message):
        provider.recover_prepared(record, validate=validate_transition)
    assert git(provider.repository, "status", "--porcelain") == before
    assert git(provider.repository, "rev-parse", "HEAD") == head


def test_cached_telemetry_receipt_cannot_replace_missing_file(config_file):
    config, _ = config_file
    app = Application(config)
    telemetry = app.root / "telemetry/one/spans.jsonl"
    JsonlWriter(telemetry).append(
        {
            "resourceSpans": [
                {"scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": "b" * 16}]}]}
            ]
        }
    )
    result = {
        "outcome": "returned",
        "telemetry": {"span_count": 1, "rejected_exports": 0},
        "telemetry_path": str(telemetry),
    }
    from backlog_harness.telemetry import Sink

    result["telemetry"]["evidence_sha256"] = Sink(telemetry, {}).evidence_digest()
    app.validate_invocation_result(result)
    telemetry.unlink()
    with pytest.raises(TransitionBlocked, match="no longer matches"):
        app.validate_invocation_result(result)


@pytest.mark.parametrize("incomplete", [False, True])
def test_hold_review_retries_frozen_request_after_allowance_write(
    config_file, provider, monkeypatch, incomplete
):
    import asyncio
    import json

    import yaml

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
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
        authority("coordinator", incident="usage_limit"),
        validate=validate_transition,
    )
    atomic_json(
        app._stage_path(item.item_id, "incident-one"),
        {"at": "2026-09-30", "resume_state": "Running"},
    )
    prompts = []

    def usage(name):
        allowance = app._stage_path(name, "allowance")
        ceiling = json.loads(allowance.read_text())["ceiling"] if allowance.exists() else 200
        return {
            "status": "below" if 250 < ceiling else "crossed",
            "generated_tokens": None if incomplete else 250,
            "original_high": 100,
            "ceiling": ceiling,
            "may_generate": 250 < ceiling,
        }

    async def decision(item_id, stage, role, prompt, **kwargs):
        prompts.append(prompt)
        return {
            "role": "coordinator",
            "invocation_id": "review",
            "outcome": "returned",
            "session": {"session_id": "coordinator", "native_session_id": "control"},
            "events": [{"type": "turn.completed", "usage": {"output_tokens": 10}}],
            "text": json.dumps(
                {
                    "operation": "resume",
                    "item_id": item_id,
                    "approved_ceiling": 500,
                    "retained_owner": "canonical",
                }
            ),
        }

    monkeypatch.setattr(app, "usage_view", usage)
    monkeypatch.setattr(app, "invoke", decision)
    if incomplete:
        with pytest.raises(TransitionBlocked, match="Unknown usage"):
            asyncio.run(
                app.review_hold(
                    item.item_id, requested_ceiling=500, reference="existing authorization"
                )
            )
        assert not prompts
        return
    real_transition = app.provider.transition

    def crash_transition(*args, **kwargs):
        raise RuntimeError("crash after allowance before release")

    monkeypatch.setattr(app.provider, "transition", crash_transition)
    with pytest.raises(RuntimeError, match="after allowance"):
        asyncio.run(
            app.review_hold(item.item_id, requested_ceiling=500, reference="operator approval")
        )
    monkeypatch.setattr(app.provider, "transition", real_transition)
    result = asyncio.run(
        app.review_hold(item.item_id, requested_ceiling=500, reference="operator approval")
    )
    assert result["state"] == "Running"
    assert prompts[0] == prompts[1]
    assert provider.item(item.item_id).owner == "canonical"


def test_cached_telemetry_same_span_count_with_changed_content_is_rejected(config_file):
    from backlog_harness.telemetry import Sink

    config, _ = config_file
    app = Application(config)
    path = app.root / "telemetry/changed/spans.jsonl"
    value = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "name": "original"}]}
                ]
            }
        ]
    }
    JsonlWriter(path).append(value)
    report = {
        "span_count": 1,
        "rejected_exports": 0,
        "evidence_sha256": Sink(path, {}).evidence_digest(),
    }
    result = {"outcome": "returned", "telemetry": report, "telemetry_path": str(path)}
    app.validate_invocation_result(result)
    path.write_text(path.read_text().replace("original", "changed"))
    with pytest.raises(TransitionBlocked, match="content is not bound"):
        app.validate_invocation_result(result)


@pytest.mark.parametrize("state", ["Starting", "Running"])
@pytest.mark.parametrize("retained_assignment", [False, True])
def test_existing_reservation_without_retained_session_evidence_never_relaunches(
    config_file, provider, monkeypatch, state, retained_assignment
):
    import asyncio

    import yaml

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        authority("coordinator", operation="new"),
        validate=validate_transition,
    )
    if state == "Running":
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
    if retained_assignment:
        atomic_json(
            app._stage_path(item.item_id, "assignment"),
            {"content": item.content, "original_high": 100},
        )
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        pytest.fail("An existing canonical assignment cannot launch a replacement")

    monkeypatch.setattr(app, "invoke", forbidden)
    with pytest.raises(TransitionBlocked, match="no retained"):
        asyncio.run(app.run_item(item.item_id))
    assert not calls
    assert app._stage_path(item.item_id, "assignment").exists() is retained_assignment
    assert not list((app.root / "runs").glob("*/operations/*/invocations/*/intent.json"))
    assert provider.item(item.item_id).state == state


@pytest.mark.parametrize("state", ["Ready", "Starting"])
def test_stale_frozen_assignment_blocks_admission_before_invocation(
    config_file, provider, monkeypatch, state
):
    import asyncio
    import json

    import yaml

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    original = provider.item("item-one")
    atomic_json(
        app._stage_path(original.item_id, "assignment"),
        {
            "content": original.content,
            "provider_revision": original.revision,
            "provider_path": original.path,
            "original_high": original.original_high,
        },
    )
    path = provider.repository / original.path
    path.write_text(original.content.replace("One item.", "Revised objective."))
    git(provider.repository, "add", "--", original.path)
    git(provider.repository, "commit", "-m", "Revise authoritative objective")
    revised = provider.item(original.item_id)
    if state == "Starting":
        provider.transition(
            revised.item_id,
            revised.revision,
            "Starting",
            authority("coordinator", operation="new"),
            validate=validate_transition,
        )
        atomic_json(
            app._stage_path(original.item_id, "admit"),
            {
                "text": json.dumps(
                    {
                        "operation": "new",
                        "item_id": original.item_id,
                        "provider_revision": revised.revision,
                    }
                )
            },
        )
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        pytest.fail("Stale assignment must block before any invocation")

    monkeypatch.setattr(app, "invoke", forbidden)
    with pytest.raises(TransitionBlocked, match="frozen assignment"):
        asyncio.run(app.run_item(original.item_id))
    assert not calls
    assert provider.item(original.item_id).state == state
    assert not list((app.root / "runs").glob("*/operations/*/invocations/*/intent.json"))


@pytest.mark.parametrize("changed_content", [False, True])
def test_starting_continuation_requires_content_bound_provider_admission(
    config_file, provider, monkeypatch, changed_content
):
    import asyncio
    import json

    import yaml

    from backlog_harness.telemetry import Sink

    config, data = config_file
    data["repository"] = str(provider.repository)
    data["operational_root"] = str(provider.evidence_root)
    config.write_text(yaml.safe_dump(data))
    app = Application(config)
    item = provider.item("item-one")
    trace_path = app.root / "telemetry/admission/spans.jsonl"
    JsonlWriter(trace_path).append(
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "a" * 32,
                                    "spanId": "b" * 16,
                                    "name": "admission",
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )
    sink = Sink(trace_path, {})
    admission = {
        "role": "coordinator",
        "invocation_id": "admission",
        "outcome": "returned",
        "session": {"session_id": "coordinator", "native_session_id": "native-coordinator"},
        "text": json.dumps(
            {
                "operation": "new",
                "item_id": item.item_id,
                "provider_revision": item.revision,
            }
        ),
        "telemetry_path": str(trace_path),
        "telemetry": {
            "span_count": 1,
            "rejected_exports": 0,
            "evidence_sha256": sink.evidence_digest(),
        },
    }
    atomic_json(
        app._stage_path(item.item_id, "assignment"),
        {
            "content": item.content.replace("One item.", "Different objective.")
            if changed_content
            else item.content,
            "provider_revision": item.revision,
            "provider_path": item.path,
            "original_high": item.original_high,
        },
    )
    atomic_json(app._stage_path(item.item_id, "admit"), admission)
    provider.transition(
        item.item_id,
        item.revision,
        "Starting",
        app.authority(admission, item.item_id, operation="new"),
        validate=validate_transition,
    )
    calls = []

    async def acceptance_boundary(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("valid acceptance boundary reached")

    monkeypatch.setattr(app, "guard", lambda name: None)
    monkeypatch.setattr(app, "invoke", acceptance_boundary)
    if changed_content:
        with pytest.raises(TransitionBlocked, match="admitted provider content"):
            asyncio.run(app.run_item(item.item_id))
        assert not calls
    else:
        with pytest.raises(RuntimeError, match="valid acceptance boundary reached"):
            asyncio.run(app.run_item(item.item_id))
        assert len(calls) == 1 and calls[0][1] == "accept"
        assert item.content in calls[0][3]


@pytest.mark.parametrize("staged", [False, True])
def test_delivery_preservation_rejects_tracked_changes(provider, staged):
    from backlog_harness.delivery import preserved_untracked

    path = provider.repository / "PROJECT.yaml"
    path.write_text(path.read_text() + "# user edit\n")
    if staged:
        git(provider.repository, "add", "--", "PROJECT.yaml")
    with pytest.raises(TransitionBlocked, match="tracked files or index"):
        preserved_untracked(provider.repository)


def test_delivery_preservation_ignores_ambient_git_overrides(provider, tmp_path, monkeypatch):
    from backlog_harness.delivery import preserved_untracked

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    (other / "wrong.txt").write_text("wrong repository")
    real = provider.repository / "preserve.txt"
    real.write_text("original")
    expected = preserved_untracked(provider.repository)
    for key, value in {
        "GIT_DIR": str(other / ".git"),
        "GIT_WORK_TREE": str(other),
        "GIT_INDEX_FILE": str(other / ".git/index"),
    }.items():
        monkeypatch.setenv(key, value)
    assert preserved_untracked(provider.repository) == expected
    real.write_text("changed")
    assert preserved_untracked(provider.repository) != expected


@pytest.mark.parametrize("overlap", ["identical", "content", "mode"])
def test_delivery_allows_only_identical_advanced_tree_entries(provider, tmp_path, overlap):
    primary = provider.repository
    candidate_repo = tmp_path / "candidate"
    subprocess.run(
        ["git", "clone", str(primary), str(candidate_repo)], check=True, capture_output=True
    )
    git(candidate_repo, "config", "user.name", "Test")
    git(candidate_repo, "config", "user.email", "test@example.invalid")
    base = git(candidate_repo, "rev-parse", "HEAD")
    (candidate_repo / "answer.txt").write_text("42\n")
    git(candidate_repo, "add", "answer.txt")
    git(candidate_repo, "commit", "-m", "Candidate source")
    candidate = git(candidate_repo, "rev-parse", "HEAD")
    (primary / "answer.txt").write_text("different\n" if overlap == "content" else "42\n")
    if overlap == "mode":
        (primary / "answer.txt").chmod(0o755)
    git(primary, "add", "answer.txt")
    git(primary, "commit", "-m", "Independent primary source")
    before = git(primary, "rev-parse", "HEAD")
    review = {
        "candidate": candidate,
        "producer_session": "producer",
        "reviewer_session": "reviewer",
        "verdict": "ACCEPT",
        "native_verified": True,
        "fresh_context": True,
        "evidence_sha256": "review",
        "unresolved_findings": [],
    }
    checks = [
        {"candidate": candidate, "argv": ["test"], "returncode": 0, "evidence_sha256": "check"}
    ]
    app = SimpleNamespace(
        provider=provider,
        config=SimpleNamespace(
            repository=primary, data={"workflow": {"allowed_paths": ["answer.txt"]}}
        ),
        _stage_path=lambda item, name: tmp_path / "stages" / (name + ".json"),
        checks=lambda repo, item, commit, name: [{**checks[0], "candidate": commit}],
    )
    if overlap != "identical":
        with pytest.raises(TransitionBlocked, match="advanced"):
            integrate(app, "item-one", candidate_repo, candidate, base, review, checks)
        assert git(primary, "rev-parse", "HEAD") == before
    else:
        result = integrate(app, "item-one", candidate_repo, candidate, base, review, checks)
        assert result["verified"] and result["disposition"] == "READY"
        head = git(primary, "rev-parse", "HEAD")
        assert integrate(app, "item-one", candidate_repo, candidate, base, review, checks) == result
        assert git(primary, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("name", ["é.txt", " leading.txt", "line\nbreak.txt"])
def test_advanced_path_comparison_preserves_exact_git_names(provider, tmp_path, name):
    from backlog_harness.delivery import compatible_primary_paths

    primary = provider.repository
    candidate_repo = tmp_path / "candidate"
    subprocess.run(
        ["git", "clone", str(primary), str(candidate_repo)], check=True, capture_output=True
    )
    git(candidate_repo, "config", "user.name", "Test")
    git(candidate_repo, "config", "user.email", "test@example.invalid")
    base = git(primary, "rev-parse", "HEAD")
    for repo, content in [(primary, "main"), (candidate_repo, "candidate")]:
        (repo / name).write_text(content)
        git(repo, "add", "--", name)
        git(repo, "commit", "-m", "Advance")
    assert not compatible_primary_paths(
        primary, candidate_repo, git(candidate_repo, "rev-parse", "HEAD"), base, "HEAD", [name]
    )


def test_pending_answer_reconciles_effect_before_reading_stale_projection(config_file, monkeypatch):
    import asyncio

    config, _ = config_file
    app = Application(config)

    operation = {
        "question": {"question_id": "q1", "text": "Proceed?"},
        "answer": {"text": "yes", "digest": digest("yes"), "question_revision": "before"},
        "before_revision": "before",
    }
    atomic_json(
        app._stage_path("item-one", "answer-operation-" + digest(["q1", "before", "yes"])),
        operation,
    )

    def stale(_):
        raise AssertionError("Stale projection must not prevent retained effect reconciliation")

    monkeypatch.setattr(app.provider, "item", stale)
    monkeypatch.setattr(app, "item_quiescent", lambda _: True)

    async def reconcile(item_id, revision, target, authority, **kwargs):
        assert (item_id, revision, target) == ("item-one", "before", "User Action Required")
        assert authority["answer"] == operation["answer"]
        raise RuntimeError("Reached original effect reconciliation")

    monkeypatch.setattr(app, "transition", reconcile)
    with pytest.raises(RuntimeError, match="Reached original effect reconciliation"):
        asyncio.run(app.resume_answer("item-one"))
