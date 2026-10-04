"""Only dispatch policy lives here; agents own provider and delivery conventions."""

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Item:
    id: str
    worktree: str
    branch: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.id, self.worktree, self.branch)):
            raise ValueError("Item requires an ID, worktree, and branch")
        if not Path(self.worktree).is_absolute():
            raise ValueError("Item worktree must be absolute")


@dataclass(frozen=True)
class Outcome:
    status: str
    transient: bool = False
    detail: str = ""


@dataclass
class Attempt:
    item: Item
    task: asyncio.Task
    agent: object = None


class Harness:
    def __init__(
        self,
        access,
        launch,
        merge,
        emit,
        *,
        capacity=1,
        timeout=3600,
        poll_interval=10,
        merge_interval=60,
        merge_timeout=3600,
        heartbeat_interval=10,
        epic=None,
        scheduling_revision=0,
        scheduling_request_id=None,
        work_item_provider=None,
        provider_timeout=30,
    ):
        if (
            type(capacity) is not int
            or not 1 <= capacity <= 8
            or type(scheduling_revision) is not int
            or scheduling_revision < 0
            or any(
                type(v) not in (int, float) or not 0 < v < float("inf")
                for v in (
                    timeout,
                    poll_interval,
                    merge_interval,
                    merge_timeout,
                    heartbeat_interval,
                    provider_timeout,
                )
            )
        ):
            raise ValueError(
                "Capacity must be an integer from 1 through 8; scheduling revision must be a "
                "nonnegative integer; intervals must be positive finite numbers"
            )
        self.access, self.launch, self.merge, self.emit = access, launch, merge, emit
        self.capacity, self.timeout = capacity, timeout
        self.poll_interval, self.merge_interval = poll_interval, merge_interval
        self.merge_timeout = merge_timeout
        self.heartbeat_interval = heartbeat_interval
        self.epic = epic
        self.work_item_provider = work_item_provider
        self.provider_timeout = provider_timeout
        self.scheduling_revision = scheduling_revision
        self.scheduling_request_id = scheduling_request_id
        self.scheduling_lock = asyncio.Lock()
        self.active = {}
        self.pending = []
        self.retries = {}
        self.paused = False
        self.complete = False
        self.wake = asyncio.Event()
        self.merge_requested = asyncio.Event()
        self._publish_scheduling_context()

    def scheduling(self):
        """Return the current user-selected work-item admission context."""
        return {
            "capacity": self.capacity,
            "mode": "solo" if self.capacity == 1 else "parallel",
            "revision": self.scheduling_revision,
            "request_id": self.scheduling_request_id,
        }

    def _publish_scheduling_context(self):
        update = getattr(self.access, "set_scheduling", None)
        if update is not None:
            update(self.scheduling())

    async def set_capacity(self, capacity, revision, request_id, persist):
        """Persist and apply one validated scheduling choice without cancelling active work."""
        if type(capacity) is not int or not 1 <= capacity <= 8:
            raise ValueError("Capacity must be an integer from 1 through 8")
        if type(revision) is not int or revision != self.scheduling_revision + 1:
            raise ValueError("Scheduling revision must advance by one")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Scheduling request identity must be nonempty text")
        async with self.scheduling_lock:
            previous = self.capacity
            persist()
            self.capacity = capacity
            self.scheduling_revision = revision
            self.scheduling_request_id = request_id
            self._publish_scheduling_context()
            self.emit(
                "scheduling_changed",
                request_id=request_id,
                previous_capacity=previous,
                selected_capacity=capacity,
                scheduling_mode=self.scheduling()["mode"],
                scheduling_revision=revision,
                active_item_count=len(self.active),
                scheduling_transition=("draining" if len(self.active) > capacity else "applied"),
            )
            self.wake.set()

    def pause(self):
        self.paused = True
        self.emit("paused")
        self.wake.set()

    def resume(self):
        self.paused = False
        self.emit("resumed")
        self.wake.set()

    async def provider(self, method, *args):
        """Keep the item assigned when an exceptional provider update is unavailable."""
        while True:
            try:
                return await method(*args)
            except (OSError, RuntimeError, ValueError) as exc:
                self.emit("provider_error", operation=method.__name__, detail=str(exc))
                await asyncio.sleep(self.poll_interval)

    async def retry(self, item, interrupted=False):
        count = self.retries.get(item.id, 0)
        if count < 2:
            self.retries[item.id] = count + 1
            self.emit("retry_pending", item=item.id, retry=count + 1, interrupted=interrupted)
            return interrupted
        await self.provider(self.access.hold, item)
        self.emit("held", item=item.id)
        return None

    async def failed(self, item, reason, transient=None):
        classified = await self.provider(self.access.failure, item, reason)
        retryable = classified if transient is None else transient
        if retryable:
            return await self.retry(item)
        await self.provider(self.access.hold, item)
        self.emit("held", item=item.id)
        return None

    async def deliver(self, item, interrupted):
        agent = None
        try:
            try:
                agent = await self.launch(item, interrupted)
            except (OSError, RuntimeError, ValueError) as exc:
                return await self.failed(item, f"launch failed: {exc}")
            self.active[item.id].agent = agent
            identity = {
                "item": item.id,
                "role": "development",
                "invocation": getattr(agent, "id", None),
            }
            self.emit("dispatched", **identity, interrupted=interrupted)
            try:
                # Shield preserves the wait task until kill has reaped the process.
                waiter = asyncio.create_task(agent.wait())
                try:
                    outcome = await asyncio.wait_for(asyncio.shield(waiter), self.timeout)
                except TimeoutError:
                    self.emit(
                        "development_timeout",
                        **identity,
                        timeout_seconds=self.timeout,
                        outcome="unknown",
                    )
                    await agent.kill()
                    await waiter
                    return await self.failed(item, "timeout: outcome unknown")
                finally:
                    if not waiter.done():
                        await agent.kill()
                        await waiter
            except (OSError, RuntimeError, ValueError) as exc:
                return await self.failed(item, f"execution failed: {exc}")
            if outcome is None:
                status = await self.provider(self.access.status, item)
                if status == "running":
                    self.emit("interrupted", **identity, reason="process_stopped_without_result")
                    return await self.retry(item, interrupted=True)
                return await self.failed(item, "process stopped without delivery outcome")
            self.emit("outcome", **identity, status=outcome.status)
            if outcome.status == "success":
                self.merge_requested.set()
            elif outcome.status == "failed":
                return await self.failed(item, outcome.detail, outcome.transient)
            elif outcome.status != "user_action_required":
                return await self.failed(item, "invalid outcome", transient=False)
            return None
        finally:
            if agent is not None:
                await agent.kill()
            self.wake.set()

    def dispatch(self, item, interrupted=False):
        task = asyncio.create_task(self.deliver(item, interrupted))
        self.active[item.id] = Attempt(item, task)

    def reap(self):
        for key, attempt in list(self.active.items()):
            if attempt.task.done():
                result = attempt.task.result()
                del self.active[key]
                if result is not None:
                    self.pending.append((attempt.item, result))

    async def fill(self, outside=False):
        async with self.scheduling_lock:
            requested_room = self.capacity - len(self.active)
            scheduling = self.scheduling()
        if requested_room <= 0:
            return
        if self.work_item_provider is not None:
            try:
                count = await asyncio.wait_for(
                    self.work_item_provider.ready_count(
                        epic=self.epic, outside=outside, excluded=list(self.active)
                    ),
                    self.provider_timeout,
                )
                if type(count) is not int or count < 0:
                    raise ValueError("Work Item Provider ready count must be a nonnegative integer")
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                self.emit(
                    "ready_count_failed",
                    level="ERROR",
                    detail=str(exc) or "Work Item Provider count timed out",
                    epic=self.epic,
                    outside=outside,
                )
                return
            self.emit("ready_count", count=count, epic=self.epic, outside=outside)
            if count == 0 or self.paused:
                return
        items = await self.access.ready(
            requested_room, self.epic, outside, list(self.active), scheduling=scheduling
        )
        # Validate the full response before starting anything from it.
        if len(items) > requested_room or len({i.id for i in items}) != len(items):
            raise ValueError("Provider returned excess or duplicate ready items")
        worktrees = {str(Path(a.item.worktree).resolve()) for a in self.active.values()}
        for item in items:
            path = str(Path(item.worktree).resolve())
            if item.id in self.active or path in worktrees:
                raise ValueError("Provider returned an active item or shared worktree")
            worktrees.add(path)
        # A pause or capacity change may arrive while the provider prepares its response.
        async with self.scheduling_lock:
            if self.paused:
                return
            room = max(0, self.capacity - len(self.active))
            dispatchable, deferred = items[:room], items[room:]
            for item in dispatchable:
                self.dispatch(item)
            if deferred:
                self.emit(
                    "prepared_items_deferred",
                    items=[item.id for item in deferred],
                    selected_capacity=self.capacity,
                    active_item_count=len(self.active),
                )
        if not items:
            self.emit("no_ready_items", epic=self.epic, outside=outside)

    async def cycle(self):
        self.reap()
        if self.paused or self.complete:
            return
        async with self.scheduling_lock:
            while self.pending and len(self.active) < self.capacity:
                item, interrupted = self.pending.pop(0)
                self.dispatch(item, interrupted)
            full = len(self.active) >= self.capacity
        if full:
            return
        if self.epic and await self.access.epic_complete(self.epic):
            self.complete = True
            self.emit("epic_complete", epic=self.epic)
            return
        await self.fill()
        if self.epic and not self.paused:
            await self.fill(outside=True)

    async def merges(self):
        """One invocation at a time, with a pending completion trigger retained."""
        while True:
            self.merge_requested.clear()
            agent = None
            try:
                agent = await self.merge()
                self.emit("merge_started")
                result = await asyncio.wait_for(agent.wait(), self.merge_timeout)
                self.emit("merge_finished", outcome=result.status if result else "unknown")
            except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
                self.emit("merge_error", detail=str(exc))
            finally:
                if agent is not None:
                    await agent.kill()
            if self.complete and not self.active and not self.merge_requested.is_set():
                return
            try:
                await asyncio.wait_for(self.merge_requested.wait(), self.merge_interval)
            except TimeoutError:
                pass

    async def schedule(self, merger):
        while True:
            self.wake.clear()
            try:
                await self.cycle()
            except (OSError, RuntimeError, ValueError) as exc:
                self.emit("dispatch_error", detail=str(exc))
            if self.complete and not self.active:
                break
            try:
                await asyncio.wait_for(self.wake.wait(), self.poll_interval)
            except TimeoutError:
                pass
        self.merge_requested.set()
        await merger

    def heartbeat(self):
        active_count = len(self.active)
        scheduling = self.scheduling()
        self.emit(
            "heartbeat",
            monitoring_status="paused" if self.paused else "active" if self.active else "idle",
            interval_seconds=self.heartbeat_interval,
            active_items=[
                {"item": key, "invocation": getattr(attempt.agent, "id", None)}
                for key, attempt in self.active.items()
            ],
            active_invocations=getattr(self.access, "running_invocations", list)(),
            scheduling_revision=self.scheduling_revision,
            scheduling_request_id=self.scheduling_request_id,
            scheduling_mode=scheduling["mode"],
            selected_capacity=self.capacity,
            effective_capacity=self.capacity,
            active_item_count=active_count,
            scheduling_transition=("draining" if active_count > self.capacity else "applied"),
        )

    async def run(self):
        merger = asyncio.create_task(self.merges())
        scheduler = asyncio.create_task(self.schedule(merger))
        reason = "cancelled"
        try:
            # This is the supervising task, not a detached heartbeat timer. Slow
            # agent I/O yields here; a stalled event loop cannot emit a heartbeat.
            while not scheduler.done():
                if merger.done():
                    merger.result()
                self.heartbeat()
                await asyncio.wait({scheduler}, timeout=self.heartbeat_interval)
            await scheduler
            reason = "completed"
        except Exception:
            reason = "error"
            raise
        finally:
            self.emit("shutdown_started", reason=reason)
            scheduler.cancel()
            merger.cancel()
            tasks = [a.task for a in self.active.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(scheduler, merger, *tasks, return_exceptions=True)
            self.emit("shutdown_completed", reason=reason)
