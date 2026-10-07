"""Codex Python SDK implementation of the harness agent adapter."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from openai_codex import ApprovalMode, Codex, CodexConfig, CodexError
from openai_codex.async_client import AsyncCodexClient
from openai_codex.types import ReasoningEffort

from .adapter import execute
from .desktop import PINNED_SECTION_ID, notify_archived


def project_thread(cwd, project):
    """Include the project and its managed worktrees, but no sibling projects."""
    return Path(cwd) == Path(project) or Path(project) / ".worktrees" in Path(cwd).parents


def completed_before(thread, project, cutoff):
    """Require a finished run and inactivity, not merely an idle runtime status."""
    turns = thread["turns"]
    return (
        project_thread(thread["cwd"], project)
        and (thread.get("section") or {}).get("id") != PINNED_SECTION_ID
        and thread["status"]["type"] in ("idle", "notLoaded")
        and thread["updatedAt"] < cutoff
        and bool(turns)
        and turns[-1]["status"] in ("completed", "failed", "interrupted")
        and turns[-1].get("completedAt") is not None
        and turns[-1]["completedAt"] < cutoff
    )


async def archive_completed_threads(project, age, emit):
    """Archive old finished project chats through the SDK without starting a turn."""
    cutoff = time.time() - age
    emit("thread_cleanup_started", level="DEBUG", project=project, cutoff=cutoff)
    archived, failed = 0, 0
    async with AsyncCodexClient(CodexConfig(cwd=project)) as client:
        await client.initialize()
        # Finish pagination before archiving so mutations cannot shift the listing.
        candidates = []
        cursor = None
        while True:
            page = await client.thread_list(
                {
                    "archived": False,
                    "limit": 100,
                    "cursor": cursor,
                    "useStateDbOnly": True,
                }
            )
            candidates.extend(
                t.id
                for t in page.data
                if project_thread(t.cwd.root, project) and t.updated_at < cutoff
            )
            emit("thread_cleanup_page", level="DEBUG", project=project, count=len(page.data))
            cursor = page.next_cursor
            if not cursor:
                break
        for identity in candidates:
            try:
                response = await client.thread_read(identity, include_turns=True)
                thread = response.thread.model_dump(mode="json", by_alias=True)
                if not completed_before(thread, project, cutoff):
                    emit("thread_archive_skipped", level="DEBUG", thread=identity)
                    continue
                # Re-read immediately before the mutation to catch resumed chats.
                response = await client.thread_read(identity, include_turns=True)
                current = response.thread.model_dump(mode="json", by_alias=True)
                if not completed_before(current, project, cutoff) or current != thread:
                    emit("thread_archive_skipped", level="DEBUG", thread=identity)
                    continue
                emit("thread_archiving", level="DEBUG", thread=identity, project=project)
                await client.thread_archive(identity)
                archived += 1
                emit(
                    "thread_archived",
                    thread=identity,
                    project=project,
                    completed_at=current["turns"][-1]["completedAt"],
                )
            except (CodexError, OSError, ValueError, RuntimeError) as exc:
                failed += 1
                emit("thread_archive_failed", level="ERROR", thread=identity, error=str(exc))
        # Replay persisted archives, including earlier runs, so a closed app or
        # interrupted notification does not leave permanently stale sidebar rows.
        try:
            cursor, archived_threads = None, []
            while True:
                page = await client.thread_list(
                    {
                        "archived": True,
                        "limit": 100,
                        "cursor": cursor,
                        "useStateDbOnly": True,
                    }
                )
                archived_threads.extend(
                    {"id": t.id, "cwd": t.cwd.root}
                    for t in page.data
                    if project_thread(t.cwd.root, project)
                )
                cursor = page.next_cursor
                if not cursor:
                    break
            await notify_archived(archived_threads)
            emit(
                "thread_archive_ui_notifications_sent",
                level="DEBUG",
                project=project,
                count=len(archived_threads),
            )
        except (CodexError, OSError, ValueError, RuntimeError, EOFError) as exc:
            failed += 1
            emit(
                "thread_archive_ui_sync_failed",
                level="ERROR",
                project=project,
                error=str(exc),
                retry="next cleanup pass",
            )
    emit("thread_cleanup_completed", project=project, archived=archived, failed=failed)
    return failed == 0


async def cleanup_threads(config, emit, *, once=False):
    """Run immediately, then periodically; cancellation closes the SDK connection."""
    settings = config.get("thread_cleanup", {})
    if not settings.get("enabled", True):
        emit("thread_cleanup_disabled", level="DEBUG")
        return True
    while True:
        try:
            success = await archive_completed_threads(
                config["project"], settings.get("completed_age", 300), emit
            )
        except (CodexError, OSError, ValueError, RuntimeError) as exc:
            success = False
            emit("thread_cleanup_failed", level="ERROR", error=str(exc))
        if once:
            return success
        await asyncio.sleep(settings.get("interval", 60))


def thread_name(request, cwd):
    role = request.get("role")
    if role == "development":
        return f"Develop — {request['item']['id']}"
    if role == "merge":
        return f"Merge — {Path(cwd).name}"
    if role == "access":
        action = request["action"]
        label = {
            "ready": "Find ready work",
            "status": "Check status",
            "failure": "Record failure",
            "hold": "Hold item",
            "decision": "Process decision",
            "epic_complete": "Check epic completion",
        }[action]
        subject = request.get("item", {}).get("id")
        if action == "decision":
            subject = request["submission"]["item_id"]
        elif action == "epic_complete":
            subject = request["epic"]
        return f"Backlog — {label}" + (f" — {subject}" if subject else "")
    return f"Harness — {Path(cwd).name}"


class CodexAdapter:
    def __init__(self, model=None, effort="high"):
        self.model = model
        self.effort = ReasoningEffort(effort)

    def run(self, *, instructions, request, schema, cwd, emit):
        # SDK owns app-server transport. Its child inherits the invocation group,
        # allowing the existing harness timeout/exit path to reap both processes.
        with Codex(CodexConfig(cwd=str(cwd))) as client:
            thread = client.thread_start(
                cwd=str(cwd),
                model=self.model,
                developer_instructions=instructions,
                approval_mode=ApprovalMode.deny_all,
            )
            emit({"event": "thread_started", "thread": thread.id})
            thread.set_name(thread_name(request, cwd))
            turn = thread.turn(json.dumps(request), effort=self.effort, output_schema=schema)
            response = None
            completed = False
            for notification in turn.stream():
                payload = notification.payload
                body = (
                    payload.model_dump(mode="json", by_alias=True)
                    if hasattr(payload, "model_dump")
                    else payload.params
                )
                event = {"method": notification.method, "payload": body}
                emit(event)
                payload = event["payload"]
                if event["method"] == "item/completed":
                    item = payload["item"]
                    if item["type"] == "agentMessage" and item.get("phase") in (
                        None,
                        "final_answer",
                    ):
                        response = item["text"]
                if event["method"] == "turn/completed":
                    completed = payload["turn"]["status"] == "completed"
                    if not completed:
                        raise RuntimeError(f"Codex turn did not complete: {payload['turn']}")
            if not completed or response is None:
                raise RuntimeError("Codex turn ended without a completed final response")
            return json.loads(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--effort", default="high", choices=[e.value for e in ReasoningEffort])
    args = parser.parse_args()
    execute(CodexAdapter(args.model, args.effort))


if __name__ == "__main__":
    main()
