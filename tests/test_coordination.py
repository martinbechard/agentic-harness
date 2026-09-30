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

    app = SimpleNamespace(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: list(items.values())),
        run_item=run_item,
        reconcile=list,
    )
    result = asyncio.run(RunController(app).run("until-terminal"))
    assert result == {"outcome": "successful", "counts": {"Completed": 3}}
    assert maximum == expected
    assert sorted(calls) == ["one", "three", "two"]


def test_watch_unchanged_scope_does_not_dispatch_and_pause_stop_work(config_file, tmp_path):
    config, data = config_file
    data["poll_seconds"] = 0.01
    data["operational_root"] = str(tmp_path / "ops")
    config.write_text(yaml.safe_dump(data))
    calls = []

    async def forbidden(name):
        calls.append(name)

    app = SimpleNamespace(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=list),
        run_item=forbidden,
        reconcile=list,
    )

    async def exercise():
        controller = RunController(app)
        task = asyncio.create_task(controller.run("watch"))
        await asyncio.sleep(0.04)
        assert controller.state == "IdleWatch"
        controller.pause()
        assert not controller.admission_open
        controller.resume()
        await asyncio.sleep(0.04)
        await controller.stop()
        await task

    asyncio.run(exercise())
    assert not calls


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

    asyncio.run(exercise())
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

    app = SimpleNamespace(
        root=tmp_path / "ops",
        config_path=config,
        config=load_config(config),
        provider=SimpleNamespace(snapshot=lambda: current),
        run_item=run_item,
        reconcile=list,
    )
    controller = RunController(app)
    result = asyncio.run(controller.run("until-terminal"))
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

    app = SimpleNamespace(
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
        await started.wait()
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

    asyncio.run(exercise())
