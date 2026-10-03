"""Durable scheduling controls reject stale intent and recover uncertain receipts."""

import asyncio
import json
import uuid

import pytest
from test_engine import Access, Agent

from backlog_harness.control import SchedulingControl
from backlog_harness.engine import Harness


def request(run, revision, capacity, request_id=None):
    return {
        "schema_version": 1,
        "request_id": request_id or str(uuid.uuid4()),
        "expected_run": run,
        "expected_revision": revision,
        "capacity": capacity,
    }


def harness(capacity=1, revision=0, request_id=None):
    access, events = Access(), []

    async def launch(*_args):
        return Agent()

    async def merge():
        return Agent()

    value = Harness(
        access,
        launch,
        merge,
        lambda event, **fields: events.append((event, fields)),
        capacity=capacity,
        scheduling_revision=revision,
        scheduling_request_id=request_id,
    )
    return value, events


def submit(control, value):
    control.request_path.parent.mkdir(parents=True, exist_ok=True)
    control.request_path.write_text(json.dumps(value))


def test_valid_request_applies_persists_and_survives_restart(tmp_path):
    async def scenario():
        run = "run-one"
        control = SchedulingControl(tmp_path, run, 1)
        current, events = harness()
        value = request(run, 0, 2)
        submit(control, value)

        await control.process(current)

        result = json.loads(control.result_path.read_text())
        assert result["status"] == "applied"
        assert result["request"] == value
        assert result["capacity"] == 2
        assert result["mode"] == "parallel"
        assert result["revision"] == 1
        assert not control.request_path.exists()
        assert current.capacity == control.capacity == 2
        assert current.scheduling_revision == control.revision == 1
        assert events[-1][0] == "scheduling_changed"

        restarted = SchedulingControl(tmp_path, "run-two", 1)
        assert restarted.capacity == 2
        assert restarted.revision == 1
        assert restarted.request_id == value["request_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize(("run", "capacity"), [("", 1), (None, 1), ("run", 0), ("run", 9)])
def test_invalid_initial_control_identity_or_capacity_is_rejected(tmp_path, run, capacity):
    with pytest.raises(ValueError):
        SchedulingControl(tmp_path, run, capacity)


def test_accepted_request_recovers_missing_result_without_reapplying(tmp_path):
    async def scenario():
        value = request("old-run", 0, 2)
        control = SchedulingControl(tmp_path, "old-run", 1)
        current, _ = harness()
        submit(control, value)
        await control.process(current)
        control.result_path.unlink()
        submit(control, value)

        restarted = SchedulingControl(tmp_path, "new-run", 1)
        next_harness, events = harness(
            capacity=restarted.capacity,
            revision=restarted.revision,
            request_id=restarted.request_id,
        )
        await restarted.process(next_harness)

        result = json.loads(restarted.result_path.read_text())
        assert result["status"] == "applied"
        assert result["request"] == value
        assert result["revision"] == 1
        assert not [event for event, _ in events if event == "scheduling_changed"]
        assert not restarted.request_path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"expected_run": "stale-run"}, "stale_run"),
        ({"expected_revision": 7}, "stale_revision"),
        ({"capacity": 0}, "invalid_request"),
        ({"capacity": 9}, "invalid_request"),
        ({"capacity": True}, "invalid_request"),
        ({"extra": "field"}, "invalid_request"),
        ({"schema_version": 2}, "invalid_request"),
        ({"request_id": "not-a-uuid"}, "invalid_request"),
        ({"request_id": str(uuid.uuid4()).upper()}, "invalid_request"),
        ({"expected_run": ""}, "invalid_request"),
        ({"expected_revision": -1}, "invalid_request"),
    ],
)
def test_invalid_or_stale_requests_are_rejected_without_mutation(tmp_path, change, reason):
    async def scenario():
        control = SchedulingControl(tmp_path, "live-run", 1)
        current, events = harness()
        value = {**request("live-run", 0, 2), **change}
        submit(control, value)

        await control.process(current)

        result = json.loads(control.result_path.read_text())
        assert result["status"] == "rejected"
        assert result["reason"] == reason
        assert result["request"] == value
        assert result["revision"] == 0
        assert current.capacity == control.capacity == 1
        assert not control.scheduling_path.exists()
        assert not control.request_path.exists()
        assert events[-1][0] == "scheduling_change_rejected"

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["{", "[]"])
def test_malformed_request_is_removed_and_rejected(tmp_path, raw):
    async def scenario():
        control = SchedulingControl(tmp_path, "live-run", 1)
        current, _ = harness()
        control.request_path.write_text(raw)
        await control.process(current)
        assert not control.request_path.exists()
        assert json.loads(control.result_path.read_text())["reason"] == "invalid_request"

    asyncio.run(scenario())


def test_duplicate_request_is_idempotent_and_conflicting_id_is_rejected(tmp_path):
    async def scenario():
        control = SchedulingControl(tmp_path, "live-run", 1)
        current, events = harness()
        value = request("live-run", 0, 2)
        submit(control, value)
        await control.process(current)
        changed_count = sum(event == "scheduling_changed" for event, _ in events)

        submit(control, value)
        await control.process(current)
        assert json.loads(control.result_path.read_text())["status"] == "applied"
        assert sum(event == "scheduling_changed" for event, _ in events) == changed_count

        conflict = {**value, "capacity": 3}
        submit(control, conflict)
        await control.process(current)
        result = json.loads(control.result_path.read_text())
        assert result["status"] == "rejected"
        assert result["reason"] == "request_id_conflict"
        assert result["request"] == conflict
        assert current.capacity == 2

    asyncio.run(scenario())


def test_rejected_request_retry_and_conflict_reconcile_from_last_result(tmp_path):
    async def scenario():
        control = SchedulingControl(tmp_path, "live-run", 1)
        current, events = harness()
        value = request("stale-run", 0, 2)
        submit(control, value)
        await control.process(current)
        rejected_count = sum(event == "scheduling_change_rejected" for event, _ in events)

        submit(control, value)
        await control.process(current)
        assert json.loads(control.result_path.read_text())["reason"] == "stale_run"
        assert sum(event == "scheduling_change_rejected" for event, _ in events) == rejected_count

        submit(control, {**value, "capacity": 3})
        await control.process(current)
        assert json.loads(control.result_path.read_text())["reason"] == "request_id_conflict"

    asyncio.run(scenario())


def test_corrupt_previous_result_does_not_block_a_new_valid_request(tmp_path):
    async def scenario():
        control = SchedulingControl(tmp_path, "live-run", 1)
        current, _ = harness()
        control.result_path.write_text("{")
        submit(control, request("live-run", 0, 2))
        await control.process(current)
        assert json.loads(control.result_path.read_text())["status"] == "applied"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"unexpected": "field"},
        {"schema_version": 2},
        {"revision": 0},
        {"capacity": 9},
        {"mode": "solo"},
        {"request": {}},
        {"request_capacity": 3},
        {"request_revision": 1},
        {"updated_at": 7},
    ],
)
def test_corrupt_accepted_state_stops_startup(tmp_path, change):
    folder = tmp_path / "control"
    folder.mkdir()
    accepted = request("old-run", 0, 2)
    state = {
        "schema_version": 1,
        "revision": 1,
        "capacity": 2,
        "mode": "parallel",
        "request": accepted,
        "updated_at": "1",
    }
    if "request_capacity" in change:
        accepted["capacity"] = change["request_capacity"]
    elif "request_revision" in change:
        accepted["expected_revision"] = change["request_revision"]
    else:
        state.update(change)
    (folder / "scheduling.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="accepted scheduling state"):
        SchedulingControl(tmp_path, "run", 1)


def test_no_pending_request_is_a_noop(tmp_path):
    async def scenario():
        control = SchedulingControl(tmp_path, "run", 1)
        current, _ = harness()
        assert await control.process(current) is False

    asyncio.run(scenario())
