"""Observable scheduling behavior for UC-001 through UC-014."""

import asyncio
from pathlib import Path

import pytest

from backlog_harness.engine import Harness, Item, Outcome


class Agent:
    def __init__(self, outcome=None, stopped=False):
        self.result = outcome
        self.stopped = asyncio.Event()
        self.killed = False
        if stopped:
            self.stopped.set()

    async def wait(self):
        await self.stopped.wait()
        return self.result

    async def kill(self):
        self.killed = True
        self.stopped.set()

    def finish(self, result=None):
        self.result = result
        self.stopped.set()


class Access:
    def __init__(self):
        self.items = [Item("one", "/one", "one")]
        self.failures = []
        self.held = []
        self.current_status = "running"
        self.transient = True
        self.finished = False
        self.queries = []

    async def ready(self, limit, epic, outside, excluded):
        self.queries.append((limit, epic, outside, excluded))
        result, self.items = self.items[:limit], self.items[limit:]
        return result

    async def status(self, item):
        return self.current_status

    async def failure(self, item, reason):
        self.failures.append(reason)
        return self.transient

    async def hold(self, item):
        self.held.append(item.id)

    async def epic_complete(self, epic):
        return self.finished


def make(**options):
    access, launched, merged, events = Access(), [], [], []

    async def launch(item, interrupted):
        agent = Agent()
        launched.append((item, interrupted, agent))
        return agent

    async def merge():
        agent = Agent()
        merged.append(agent)
        return agent

    harness = Harness(
        access, launch, merge, lambda event, **kw: events.append((event, kw)), **options
    )
    return harness, access, launched, merged, events


async def settle(h):
    await asyncio.gather(*(a.task for a in h.active.values()))
    h.reap()


async def started(h):
    await h.cycle()
    await asyncio.sleep(0)


def test_success_and_user_action_are_updated_by_development_agent():
    async def scenario():
        for status in ("success", "user_action_required"):
            h, access, launched, _, _ = make()
            await started(h)
            launched[0][2].finish(Outcome(status))
            await settle(h)
            assert not h.active and not h.pending and not access.failures
            assert h.merge_requested.is_set() == (status == "success")

    asyncio.run(scenario())


def test_timeout_kills_before_retry_and_limits_to_two_retries():
    async def scenario():
        h, access, launched, _, _ = make(timeout=0.01)
        for _ in range(3):
            await started(h)
            await settle(h)
        assert len(launched) == 3
        assert access.held == ["one"]
        assert len(access.failures) == 3
        assert all(a[2].killed for a in launched)

    asyncio.run(scenario())


def test_interrupted_recovery_preserves_workspace_and_waits_for_resume():
    async def scenario():
        h, access, launched, _, _ = make()
        await started(h)
        h.pause()
        launched[0][2].finish()
        await settle(h)
        await h.cycle()
        assert len(launched) == 1
        h.resume()
        await started(h)
        assert launched[1][:2] == (launched[0][0], True)
        launched[1][2].finish(Outcome("success"))
        await settle(h)
        assert not access.failures

    asyncio.run(scenario())


def test_exception_nontransient_holds_and_transient_retries():
    async def scenario():
        for transient in (False, True):
            h, access, launched, _, _ = make()
            await started(h)
            launched[0][2].finish(Outcome("failed", transient, "exception"))
            await settle(h)
            assert access.failures == ["exception"]
            assert bool(h.pending) == transient
            assert bool(access.held) != transient

    asyncio.run(scenario())


def test_stopped_process_without_running_item_uses_failure_policy():
    async def scenario():
        h, access, launched, _, _ = make()
        access.current_status = "ready"
        access.transient = False
        await started(h)
        launched[0][2].finish()
        await settle(h)
        assert access.held == ["one"]

    asyncio.run(scenario())


def test_concurrency_epic_priority_and_outside_capacity():
    async def scenario():
        h, access, launched, _, _ = make(capacity=3, epic="epic")

        async def ready(limit, epic, outside, excluded):
            access.queries.append((limit, epic, outside, excluded))
            return [Item("other", "/other", "other")] if outside else access.items

        access.ready = ready
        await started(h)
        assert access.queries == [(3, "epic", False, []), (2, "epic", True, ["one"])]
        assert [a[0].id for a in launched] == ["one", "other"]
        for _, _, agent in launched:
            agent.finish(Outcome("success"))
        await settle(h)

    asyncio.run(scenario())


def test_no_ready_items_wait_and_pause_during_provider_query():
    async def scenario():
        h, access, launched, _, events = make()
        access.items = []
        await h.cycle()
        assert not launched and events[-1][0] == "no_ready_items"

        async def ready(*args):
            h.pause()
            return [Item("one", "/one", "one")]

        access.ready = ready
        await h.cycle()
        assert not launched

    asyncio.run(scenario())


def test_slow_provider_does_not_delay_development_timeout():
    async def scenario():
        h, access, launched, _, _ = make(capacity=2, timeout=0.02)
        await started(h)
        querying = asyncio.Event()
        release = asyncio.Event()

        async def ready(*args):
            querying.set()
            await release.wait()
            return []

        access.ready = ready
        query = asyncio.create_task(h.cycle())
        await querying.wait()
        await asyncio.wait_for(h.active["one"].task, 0.5)
        assert launched[0][2].killed
        release.set()
        await query

    asyncio.run(scenario())


def test_provider_failure_keeps_item_owned_until_update_succeeds():
    async def scenario():
        h, access, launched, _, events = make(poll_interval=0.001)
        calls = 0

        async def failure(item, reason):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("offline")
            assert "one" in h.active
            return False

        access.failure = failure
        await started(h)
        launched[0][2].finish(Outcome("failed"))
        await settle(h)
        assert access.held == ["one"] and calls == 2
        assert any(event == "provider_error" for event, _ in events)

    asyncio.run(scenario())


def test_merge_is_serial_and_completion_trigger_is_retained():
    async def scenario():
        h, _, _, merged, _ = make(merge_interval=10)
        task = asyncio.create_task(h.merges())
        await asyncio.sleep(0)
        h.merge_requested.set()
        await asyncio.sleep(0)
        assert len(merged) == 1
        merged[0].finish(Outcome("success"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(merged) == 2
        h.complete = True
        merged[1].finish(Outcome("success"))
        await task

    asyncio.run(scenario())


def test_epic_completion_drains_outside_work_and_finishes_merge():
    async def scenario():
        h, access, _, _, events = make(epic="done", poll_interval=0.001)
        access.finished = True

        async def merge():
            return Agent(Outcome("success"), stopped=True)

        h.merge = merge
        await asyncio.wait_for(h.run(), 0.5)
        assert h.complete
        assert any(e == "epic_complete" for e, _ in events)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "items",
    [
        [Item("one", "/one", "one"), Item("one", "/other", "other")],
        [Item("one", "/one", "one"), Item("two", "/one", "two")],
    ],
)
def test_invalid_provider_batch_starts_nothing(items):
    async def scenario():
        h, access, launched, _, _ = make(capacity=2)
        access.items = items
        with pytest.raises(ValueError):
            await h.cycle()
        assert not launched and not h.active

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        {"capacity": 0},
        {"capacity": 1.5},
        {"timeout": -1},
        {"poll_interval": float("nan")},
        {"heartbeat_interval": 0},
    ],
)
def test_invalid_configuration(options):
    with pytest.raises(ValueError):
        make(**options)


def test_item_requires_stable_identity_and_absolute_workspace():
    with pytest.raises(ValueError):
        Item("", "/one", "main")
    with pytest.raises(ValueError):
        Item("one", str(Path("relative")), "main")


@pytest.mark.parametrize("failure", ["launch", "wait", "invalid_outcome"])
def test_execution_boundary_failures_are_recorded_and_held(failure):
    async def scenario():
        h, access, _, _, _ = make()
        access.transient = False

        class BrokenAgent(Agent):
            async def wait(self):
                if failure == "wait":
                    raise RuntimeError("broken adapter")
                return Outcome("invalid")

        async def launch(*args):
            if failure == "launch":
                raise OSError("executable unavailable")
            return BrokenAgent()

        h.launch = launch
        await started(h)
        await settle(h)
        assert access.held == ["one"]
        assert len(access.failures) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["launch", "timeout", "unknown"])
def test_merge_errors_release_serial_slot(failure):
    async def scenario():
        h, _, _, _, events = make(merge_timeout=0.01)
        h.complete = True
        agent = Agent(stopped=failure == "unknown")

        async def merge():
            if failure == "launch":
                raise OSError("merge unavailable")
            return agent

        h.merge = merge
        await h.merges()
        assert any(
            e == ("merge_finished" if failure == "unknown" else "merge_error") for e, _ in events
        )
        if failure != "launch":
            assert agent.killed

    asyncio.run(scenario())


def test_dispatch_provider_outage_is_logged_and_polled_again():
    async def scenario():
        h, access, _, _, events = make(poll_interval=0.001, epic="done")
        calls = 0

        async def complete(epic):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary provider outage")
            return True

        async def merge():
            return Agent(Outcome("success"), stopped=True)

        access.epic_complete = complete
        h.merge = merge
        await asyncio.wait_for(h.run(), 0.5)
        assert calls == 2
        assert any(e == "dispatch_error" for e, _ in events)

    asyncio.run(scenario())


def test_shutdown_reaps_live_development_process_before_returning():
    async def scenario():
        h, _, launched, merged, _ = make(poll_interval=0.01)
        run = asyncio.create_task(h.run())
        while not launched:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert launched[0][2].killed
        assert merged[0].killed

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["idle", "paused", "slow_provider"])
def test_monitor_heartbeats_continue_during_quiet_or_slow_work_and_stop_on_shutdown(state):
    async def scenario():
        h, access, _, merged, events = make(heartbeat_interval=0.01, poll_interval=1)
        access.items = []
        release = asyncio.Event()
        if state == "paused":
            h.pause()
        if state == "slow_provider":

            async def ready(*args):
                await release.wait()
                return []

            access.ready = ready
        run = asyncio.create_task(h.run())
        try:

            async def pulses():
                while sum(e == "heartbeat" for e, _ in events) < 3:
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(pulses(), 0.5)
            assert merged and not merged[0].stopped.is_set()  # Slow merge too.
            heartbeats = [v for e, v in events if e == "heartbeat"]
            assert all(
                v["monitoring_status"] == ("paused" if state == "paused" else "idle")
                for v in heartbeats
            )
            assert all(v["interval_seconds"] == 0.01 for v in heartbeats)
        finally:
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        size = len(events)
        await asyncio.sleep(0.03)
        assert len(events) == size
        assert events[-1] == ("shutdown_completed", {"reason": "cancelled"})
        assert any(e == "shutdown_started" for e, _ in events)

    asyncio.run(scenario())


def test_active_heartbeat_and_timeout_identify_the_assignment():
    async def scenario():
        h, access, launched, _, events = make(timeout=0.04, heartbeat_interval=0.005)
        access.transient = False
        original = h.launch

        async def launch(item, interrupted):
            agent = await original(item, interrupted)
            agent.id = "development-123"
            return agent

        h.launch = launch
        run = asyncio.create_task(h.run())
        try:

            async def held():
                while not access.held:
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(held(), 1)
            assert any(
                e == "heartbeat"
                and v["monitoring_status"] == "active"
                and v["active_items"] == [{"item": "one", "invocation": "development-123"}]
                for e, v in events
            )
            timeout = next(v for e, v in events if e == "development_timeout")
            assert timeout == {
                "item": "one",
                "role": "development",
                "invocation": "development-123",
                "timeout_seconds": 0.04,
                "outcome": "unknown",
            }
            assert launched[0][2].killed
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())


def test_interruption_event_identifies_missing_result_attempt():
    async def scenario():
        h, _, launched, _, events = make()
        await started(h)
        launched[0][2].finish()
        await settle(h)
        event = next(v for e, v in events if e == "interrupted")
        assert event == {
            "item": "one",
            "role": "development",
            "invocation": None,
            "reason": "process_stopped_without_result",
        }

    asyncio.run(scenario())


def test_monitor_failure_emits_shutdown_and_stops_heartbeats():
    async def scenario():
        h, _, _, _, events = make(heartbeat_interval=0.005)

        async def broken():
            raise RuntimeError("monitor dependency failed")

        h.merges = broken
        with pytest.raises(RuntimeError, match="monitor dependency failed"):
            await h.run()
        assert events[-1] == ("shutdown_completed", {"reason": "error"})
        size = len(events)
        await asyncio.sleep(0.02)
        assert len(events) == size

    asyncio.run(scenario())


def test_event_loop_stall_does_not_emit_misleading_heartbeats():
    import time

    async def scenario():
        h, access, _, _, events = make(heartbeat_interval=0.005)
        access.items = []
        run = asyncio.create_task(h.run())
        await asyncio.sleep(0.02)
        try:
            before = sum(e == "heartbeat" for e, _ in events)
            assert before > 0
            time.sleep(0.04)  # noqa: ASYNC251 - deliberately stall the monitoring event loop.
            assert sum(e == "heartbeat" for e, _ in events) == before
            await asyncio.sleep(0.02)
            assert sum(e == "heartbeat" for e, _ in events) > before
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())
