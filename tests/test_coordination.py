import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from backlog_harness.application import Application
from backlog_harness.contracts import load_config
from backlog_harness.coordination import RunController
from backlog_harness.evidence import EvidenceStore, atomic_json
from backlog_harness.provider import Item, TransitionBlocked


def controller_application(**overrides):
    """Keep real Application evidence paths while isolating item execution."""
    app = Application(overrides["config_path"])
    for name, value in overrides.items():
        setattr(app, name, value)
    return app


@pytest.mark.parametrize("mode,expected", [("SOLO", 1), ("MULTITASK", 2)])
def test_three_item_execution_obeys_selected_mode(config_file, tmp_path, mode, expected):
    config, data = config_file
    data["poll_seconds"] = 0.01
    data["operational_root"] = str(tmp_path / "ops")
    data["workflow"]["mode"] = mode
    data["max_active_invocations"] = 2
    if mode == "MULTITASK":
        clones = tmp_path / "clones"
        clones.mkdir()
        data["candidate_root"] = str(clones)
        data["workflow"]["items"] = {
            name: {"allowed_paths": [name + ".py"]} for name in ("one", "two", "three")
        }
    config.write_text(yaml.safe_dump(data))
    items = {
        name: Item(name, name + ".md", name, "Ready", "Unowned", 100, "")
        for name in ("one", "two", "three")
    }
    active, maximum, calls = 0, 0, []

    async def run_item(name):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(name)
        await asyncio.sleep(0.04)
        items[name] = replace(items[name], state="Completed")
        active -= 1
        return {"state": "Completed"}

    app = controller_application(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: list(items.values())),
        run_item=run_item,
        reconcile=list,
    )
    result = asyncio.run(asyncio.wait_for(RunController(app).run("until-terminal"), 5))
    assert result == {"outcome": "successful", "counts": {"Completed": 3}}
    assert maximum == expected
    assert sorted(calls) == ["one", "three", "two"]


def test_watch_unchanged_scope_does_not_dispatch_and_pause_stop_work(config_file, tmp_path):
    config, data = config_file
    data["poll_seconds"] = 0.01
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    calls, current = [], []

    async def complete(name):
        calls.append(name)
        current[0] = replace(current[0], state="Completed")
        return {"state": "Completed"}

    app = controller_application(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: current),
        run_item=complete,
        reconcile=list,
    )

    async def exercise():
        controller = RunController(app)
        task = asyncio.create_task(controller.run("watch"))
        await asyncio.sleep(0.04)
        assert controller.state == "IdleWatch"
        controller.pause()
        assert not controller.admission_open
        await asyncio.sleep(0.04)
        assert controller.state == "AdmissionPaused"
        current.append(Item("later", "later.md", "later-revision", "Ready", "Unowned", 100, ""))
        await asyncio.sleep(0.04)
        assert not calls
        assert controller.state == "AdmissionPaused"
        controller.resume()
        await asyncio.sleep(0.04)
        assert calls == ["later"]
        assert current[0].state == "Completed"
        assert controller.state == "IdleWatch"
        await asyncio.sleep(0.04)
        assert calls == ["later"]
        await controller.stop()
        await task

    asyncio.run(asyncio.wait_for(exercise(), 5))
    assert calls == ["later"]


def test_uncertain_slot_survives_lost_process_lock(config_file):
    config, _ = config_file
    app = Application(config)
    snapshot = load_config(config)
    path = EvidenceStore(app.root, "run").begin(
        "op", "inv", snapshot, snapshot.binding("coordinator"), action="start"
    )
    EvidenceStore.requested(path)
    atomic_json(app.root / "capacity/0/reservation.json", {"evidence_path": str(path)})

    async def attempt():
        async with app.capacity_slot():
            pytest.fail("An unknown submitted effect cannot release capacity")

    with pytest.raises(TransitionBlocked, match="Uncertain"):
        asyncio.run(attempt())


def test_capacity_serializes_actual_async_requests(config_file):
    config, _ = config_file
    app = Application(config)
    active, maximum = 0, 0

    async def worker():
        nonlocal active, maximum
        async with app.capacity_slot():
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1

    async def exercise():
        await asyncio.gather(worker(), worker(), worker())

    asyncio.run(asyncio.wait_for(exercise(), 5))
    assert maximum == 1


def test_lowered_capacity_counts_slots_above_new_cap(config_file):
    config, _data = config_file
    app = Application(config)
    snapshot = load_config(config)
    path = EvidenceStore(app.root, "run").begin(
        "op", "inv", snapshot, snapshot.binding("coordinator"), action="start"
    )
    EvidenceStore.requested(path)
    atomic_json(app.root / "capacity/1/reservation.json", {"evidence_path": str(path)})

    async def attempt():
        async with app.capacity_slot():
            pytest.fail("A slot above the lowered cap still occupies capacity")

    with pytest.raises(TransitionBlocked, match="Uncertain"):
        asyncio.run(attempt())


def test_transient_item_lock_does_not_become_sticky_scheduling_block(config_file, tmp_path):
    from backlog_harness.evidence import EvidenceError

    config, data = config_file
    data["poll_seconds"] = 0.01
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    item = Item("one", "one.md", "revision", "Ready", "Unowned", 100, "")
    current = [item]
    calls = 0

    async def run_item(name):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EvidenceError("Another process owns this operation")
        current[0] = replace(item, state="Completed")
        return {"state": "Completed"}

    app = controller_application(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: current),
        run_item=run_item,
        reconcile=list,
    )
    controller = RunController(app)
    result = asyncio.run(asyncio.wait_for(controller.run("until-terminal"), 5))
    assert result["outcome"] == "successful"
    assert calls == 2
    assert not controller.blocked


def test_invalid_generation_config_pauses_admission_without_cancelling_flight(
    config_file, tmp_path
):
    config, data = config_file
    data["poll_seconds"] = 0.01
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    item = Item("one", "one.md", "r", "Ready", "Unowned", 100, "")
    current = [item]
    started = asyncio.Event()
    release = asyncio.Event()
    completed = []

    async def run_item(name):
        started.set()
        await release.wait()
        completed.append(name)
        current[0] = replace(item, state="Completed")
        return {"state": "Completed"}

    app = controller_application(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: current),
        run_item=run_item,
        reconcile=list,
    )

    async def exercise():
        controller = RunController(app)
        task = asyncio.create_task(controller.run("watch"))
        await asyncio.wait_for(started.wait(), 2)
        invalid = dict(data)
        invalid["profiles"] = {}
        config.write_text(yaml.safe_dump(invalid))
        await asyncio.sleep(0.04)
        assert controller.state == "AdmissionPaused"
        assert not controller.admission_open
        release.set()
        await asyncio.sleep(0.02)
        assert completed == ["one"]
        await controller.stop()
        await task

    asyncio.run(asyncio.wait_for(exercise(), 5))


@pytest.mark.parametrize("dependency", ["missing", "blocked"])
def test_unrunnable_dependencies_do_not_starve_independent_item(config_file, tmp_path, dependency):
    config, data = config_file
    data["poll_seconds"] = 0.01
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    items = {
        "blocked": Item(
            "blocked",
            "blocked.md",
            "r1",
            "Ready",
            "Unowned",
            100,
            "Dependencies: " + dependency + "\n",
        ),
        "independent": Item("independent", "independent.md", "r2", "Ready", "Unowned", 100, ""),
    }
    calls = []

    async def complete(name):
        calls.append(name)
        items[name] = replace(items[name], state="Completed")
        return {"state": "Completed"}

    app = controller_application(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: list(items.values())),
        run_item=complete,
        reconcile=list,
    )
    result = asyncio.run(asyncio.wait_for(RunController(app).run("until-terminal"), 5))
    assert calls == ["independent"]
    assert result["outcome"] == "blocked"
    assert result["counts"] == {"Ready": 1, "Completed": 1}
    assert result["items"]["blocked"]["reason"] == "Unmet dependencies: " + dependency


def test_terminal_provider_with_uncertain_execution_reports_incomplete(config_file, tmp_path):
    config, _ = config_file
    item = Item("one", "one.md", "r", "Completed", "owner", 100, "")
    uncertain = {"outcome": "unresolved", "invocation_id": "retained", "quiescent": False}

    async def forbidden(_):
        pytest.fail("Uncertain execution must not be replaced")

    app = controller_application(
        root=load_config(config).operational_root,
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: [item]),
        run_item=forbidden,
        reconcile=lambda: [uncertain],
    )
    result = asyncio.run(asyncio.wait_for(RunController(app).run("until-terminal"), 5))
    assert result["outcome"] == "blocked"
    assert result["invocations"] == [uncertain]
    assert result["counts"] == {"Completed": 1}
