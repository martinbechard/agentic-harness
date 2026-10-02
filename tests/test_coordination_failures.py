"""Scheduling rejects unsafe resumes and preserves useful execution failures."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from backlog_harness.coordination import RunController, dependencies
from backlog_harness.evidence import EvidenceError
from backlog_harness.provider import TransitionBlocked


@pytest.fixture
def controller(tmp_path):
    app = SimpleNamespace(root=tmp_path, reconcile=list)
    published = []
    return RunController(app, published.append), published


def test_duplicate_dependency_declaration_is_not_silently_selected():
    with pytest.raises(TransitionBlocked, match="Duplicate dependency"):
        dependencies(SimpleNamespace(content="Dependencies: one\nDependencies: two\n"))


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_invalid_control_state_preserves_closed_admission(controller, action):
    run, published = controller
    with pytest.raises(TransitionBlocked):
        getattr(run, action)()
    assert run.state == "Available"
    assert not run.admission_open
    assert published == []


def test_resume_refuses_unowned_live_execution(controller):
    run, published = controller
    run.state = "AdmissionPaused"
    run.app.reconcile = lambda: [{"invocation_id": "old", "item_id": "one", "quiescent": False}]
    with pytest.raises(TransitionBlocked, match="not quiescent"):
        run.resume()
    assert not run.admission_open
    assert published == []


@pytest.mark.parametrize("failure", [ValueError("bad result"), OSError("storage unavailable")])
def test_item_failure_retains_actionable_block_without_retry(controller, failure):
    run, published = controller
    calls = []

    async def fail(item_id):
        calls.append(item_id)
        raise failure

    run.app.run_item = fail
    asyncio.run(run.execute(SimpleNamespace(item_id="one"), "revision"))
    assert calls == ["one"]
    assert published == [{"item_id": "one", "blocked": str(failure)}]
    saved = json.loads(run.block_path.read_text())
    assert saved["one"]["premise"] == "revision"
    assert saved["one"]["reason"] == str(failure)


@pytest.mark.parametrize("lock_busy", [True, False])
def test_only_known_lock_contention_is_treated_as_waiting(controller, lock_busy):
    run, published = controller
    message = "Another process owns this operation" if lock_busy else "Corrupt durable evidence"

    async def fail(_item_id):
        raise EvidenceError(message)

    run.app.run_item = fail
    if lock_busy:
        asyncio.run(run.execute(SimpleNamespace(item_id="one"), "revision"))
        assert published == [
            {"item_id": "one", "waiting": "Existing local operation owns the lock"}
        ]
    else:
        with pytest.raises(EvidenceError, match=message):
            asyncio.run(run.execute(SimpleNamespace(item_id="one"), "revision"))
        assert published == []
    assert not run.block_path.exists()


@pytest.mark.parametrize(
    "mode,state,message",
    [
        ("unknown", "Available", "Unknown run mode"),
        ("watch", "Running", "already active"),
    ],
)
def test_rejected_run_does_not_open_admission(controller, mode, state, message):
    run, published = controller
    run.state = state
    with pytest.raises(TransitionBlocked, match=message):
        asyncio.run(run.run(mode))
    assert not run.admission_open
    assert published == []


@pytest.mark.parametrize("acceptance", [None, "not JSON"])
def test_bad_proof_continuation_blocks_only_its_item(controller, acceptance):
    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import Item

    run, _published = controller
    run.app.config = SimpleNamespace(data={}, file_digest="configuration")
    run.app.provider = SimpleNamespace()
    run.app._stage_path = lambda item, stage: run.app.root / item / (stage + ".json")
    atomic_json(run.app._stage_path("bad", "proof-continuation"), {})
    if acceptance is not None:
        run.app._stage_path("bad", "accept").write_text(acceptance)
    items = [
        Item(name, name + ".md", "revision", "Running", "owner", 100, "")
        for name in ["bad", "good"]
    ]
    selected = run.eligible(items)
    assert [item.item_id for item, _premise in selected] == ["good"]
    assert "bad" in json.loads(run.block_path.read_text())
    assert "good" not in run.blocked


def test_cancelled_execution_propagates_without_recording_a_permanent_block(controller):
    run, published = controller

    async def exercise():
        entered = asyncio.Event()

        async def wait(_item):
            entered.set()
            await asyncio.Event().wait()

        run.app.run_item = wait
        task = asyncio.create_task(run.execute(SimpleNamespace(item_id="one"), "revision"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert published == [
        {"item_id": "one", "interruption": "requested; inspect reconciled outcome"}
    ]
    assert not run.block_path.exists()


@pytest.mark.parametrize("registration", ["missing", "changed", "valid"])
def test_only_registered_work_continuation_changes_scheduling_premise(
    controller, monkeypatch, registration
):
    from backlog_harness.contracts import digest
    from backlog_harness.evidence import atomic_json
    from backlog_harness.provider import AgentProvider, Item

    run, _published = controller
    run.app.config = SimpleNamespace(data={}, file_digest="configuration")
    run.app.provider = AgentProvider(run.app.root, run.app.root)
    monkeypatch.setattr(run.app.provider, "observation", lambda: {"dependencies": {"one": []}})
    run.app._stage_path = lambda item, stage: run.app.root / item / (stage + ".json")
    item = Item("one", "one.md", "revision", "Running", "owner", 100, "")
    premise = digest([item.revision, run.app.config.file_digest])
    run.blocked["one"] = {"premise": premise, "reason": "Previous attempt stopped"}
    request = {
        "item_id": "one",
        "revision": "revision",
        "owner": "owner",
        "candidate": "candidate",
        "previous_invocation_id": "previous",
        "previous_result_digest": "result",
        "instruction": "Continue",
    }
    atomic_json(run.app._stage_path("one", "work-continuation"), request)
    if registration != "missing":
        atomic_json(
            run.app._stage_path("one", "work-continuation-registration"),
            request if registration == "valid" else {},
        )
    selected = run.eligible([item])
    if registration == "valid":
        assert selected == [(item, digest([premise, request]))]
    else:
        assert selected == []
        assert run.blocked["one"]["reason"] == "Work continuation registration changed"


def test_missing_observed_dependencies_cannot_be_assumed_empty(controller, monkeypatch):
    from backlog_harness.provider import AgentProvider, Item

    run, _published = controller
    run.app.config = SimpleNamespace(data={}, file_digest="configuration")
    run.app.provider = AgentProvider(run.app.root, run.app.root)
    monkeypatch.setattr(run.app.provider, "observation", lambda: {"dependencies": {}})
    assert run.eligible([Item("one", "one.md", "revision", "Ready", "Unowned", 100, "")]) == []


def test_storage_change_during_run_cannot_redirect_execution(config_file, tmp_path):
    import yaml

    from backlog_harness.contracts import load_config

    path, data = config_file
    original = load_config(path)
    app = SimpleNamespace(
        root=original.operational_root, config_path=path, config=original, reconcile=list
    )
    data["operational_root"] = str(tmp_path / "different-evidence")
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(TransitionBlocked, match="Run storage identity changed"):
        asyncio.run(RunController(app).run("until-terminal"))
    assert not (tmp_path / "different-evidence").exists()


def test_mode_change_pauses_admission_without_cancelling_active_work(config_file, tmp_path):
    import yaml

    from backlog_harness.contracts import load_config
    from backlog_harness.provider import Item

    path, data = config_file
    data["poll_seconds"] = 0.01
    data["candidate_root"] = str(tmp_path / "clones")
    (tmp_path / "clones").mkdir()
    data["workflow"]["items"] = {"one": {"allowed_paths": ["answer.py"]}}
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    app = SimpleNamespace(
        root=config.operational_root, config=config, config_path=path, reconcile=list
    )
    app._stage_path = lambda item, stage: app.root / item / (stage + ".json")
    app.provider = SimpleNamespace(
        snapshot=lambda: [Item("one", "one.md", "revision", "Ready", "Unowned", 100, "")]
    )

    async def exercise():
        paused = asyncio.Event()
        cancelled = asyncio.Event()
        calls = []

        async def work(item_id):
            calls.append(item_id)
            data["workflow"]["mode"] = "MULTITASK"
            path.write_text(yaml.safe_dump(data))
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        app.run_item = work
        run = RunController(
            app, lambda value: paused.set() if value.get("state") == "AdmissionPaused" else None
        )
        task = asyncio.create_task(run.run("watch"))
        try:
            await asyncio.wait_for(paused.wait(), 2)
            assert not cancelled.is_set()
            assert not run.admission_open
            assert calls == ["one"]
        finally:
            await run.stop()
            await task
        assert cancelled.is_set()

    asyncio.run(exercise())


def test_repeated_invalid_config_reports_once_and_keeps_controls_available(config_file):
    from backlog_harness.contracts import load_config

    path, _data = config_file
    config = load_config(path)
    app = SimpleNamespace(
        root=config.operational_root, config_path=path, config=config, reconcile=list
    )
    path.write_text("invalid: [")
    published = []
    run = RunController(app, published.append)

    async def exercise():
        task = asyncio.create_task(run.run("watch"))
        try:
            await asyncio.sleep(1.1)
            assert run.state == "AdmissionPaused"
            assert not run.admission_open
            assert len([entry for entry in published if "configuration_error" in entry]) == 1
        finally:
            await run.stop()
            await task
        assert run.state == "Available"

    asyncio.run(asyncio.wait_for(exercise(), 4))


def test_watch_retains_unresolved_execution_without_replacement(config_file):
    import yaml

    from backlog_harness.contracts import load_config
    from backlog_harness.provider import Item

    path, data = config_file
    data["poll_seconds"] = 0.01
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    evidence = [{"invocation_id": "prior", "outcome": "unresolved", "quiescent": False}]
    app = SimpleNamespace(
        root=config.operational_root,
        config_path=path,
        config=config,
        reconcile=lambda: evidence,
        provider=SimpleNamespace(
            snapshot=lambda: [Item("one", "one.md", "revision", "Completed", "owner", 100, "")]
        ),
    )
    run = RunController(app)

    async def exercise():
        task = asyncio.create_task(run.run("watch"))
        try:
            await asyncio.sleep(0.04)
            assert not task.done()
            assert run.tasks == {}
            assert run.state != "IdleWatch"
        finally:
            result = await run.stop()
            await task
        assert result["state"] == "Unresolved"
        assert result["evidence"] == evidence

    asyncio.run(asyncio.wait_for(exercise(), 2))
