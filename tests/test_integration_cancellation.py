"""Repeated cancellation preserves uncertainty while the owned worker finishes cleanup."""

import asyncio
import threading

import pytest

from backlog_harness.integration_flow import integration_effect


def test_second_cancel_reports_unfinished_cleanup_and_keeps_worker_owned():
    entered, cancellation_seen, release, finished = [threading.Event() for _ in range(4)]

    def work(*, cancellation_event):
        entered.set()
        try:
            assert cancellation_event.wait(5)
            cancellation_seen.set()
            assert release.wait(5)
        finally:
            finished.set()

    async def exercise():
        task = asyncio.create_task(integration_effect(work))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            assert await asyncio.to_thread(cancellation_seen.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as failure:
                await task
            assert "cleanup did not finish" in " ".join(failure.value.__notes__)
            assert not finished.is_set()
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(exercise())
