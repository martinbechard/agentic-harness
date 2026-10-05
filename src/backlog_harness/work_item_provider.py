"""Optional, read-only Work Item Provider protocol for inexpensive Ready and Blocked counts."""

import asyncio
import json
import os
import re
import signal
from typing import Protocol
from urllib.parse import urlencode


class WorkItemProvider(Protocol):
    async def ready_count(self, *, epic: str | None, outside: bool, excluded: list[str]) -> int:
        """Count eligible Ready work; final selection and reconciliation remain agent-owned."""

    async def blocked_count(self, *, epic: str | None, excluded: list[str]) -> int:
        """Count Blocked items in scope without changing their lifecycle."""


class FileWorkItemProvider:
    def __init__(self, project, paths, layout, command, blocked_paths=None):
        self.project, self.paths, self.layout, self.command = project, paths, layout, command
        self.blocked_paths = blocked_paths or (
            paths if layout == "status-field" else ["backlog/blocked"]
        )

    async def ready_count(self, *, epic, outside, excluded):
        # The provider owns eligibility. Sharing its observer prevents dashboard/count drift.
        request = {
            "epic": epic,
            "outside": outside,
            "excluded": excluded,
            "paths": self.paths,
            "layout": self.layout,
        }
        return await self.count(request, "ready_count")

    async def blocked_count(self, *, epic, excluded):
        return await self.count(
            {
                "action": "blocked_count",
                "epic": epic,
                "outside": False,
                "excluded": excluded,
                "paths": self.blocked_paths,
                "layout": self.layout,
            },
            "blocked_count",
        )

    async def count(self, request, field):
        process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.project,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        reader = asyncio.create_task(process.communicate(json.dumps(request).encode()))
        try:
            output, error = await asyncio.shield(reader)
            if process.returncode:
                raise RuntimeError(f"File Work Item Provider failed: {error.decode().strip()}")
            result = json.loads(output)
            if (
                not isinstance(result, dict)
                or set(result) != {field}
                or type(result[field]) is not int
                or result[field] < 0
            ):
                raise ValueError(f"File Work Item Provider must return only a nonnegative {field}")
            return result[field]
        finally:
            # A provider may run Git readers; stop/reap the entire group even after parent exit.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            await reader


class GitHubWorkItemProvider:
    def __init__(self, project, repository, ready_label, executable, blocked_label=None):
        self.project, self.repository = project, repository
        self.ready_label, self.executable = ready_label, executable
        self.blocked_label = blocked_label

    async def ready_count(self, *, epic, outside, excluded):
        return await self.count(self.ready_label, epic, outside, excluded)

    async def blocked_count(self, *, epic, excluded):
        if self.blocked_label is None:
            raise ValueError("work_item_provider.blocked_label is required for Blocked counts")
        return await self.count(self.blocked_label, epic, False, excluded)

    async def count(self, label, epic, outside, excluded):
        query = urlencode({"state": "open", "labels": label, "per_page": 100})
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "api",
            f"repos/{self.repository}/issues?{query}",
            "--paginate",
            "--slurp",
            cwd=self.project,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, error = await process.communicate()
            if process.returncode:
                raise RuntimeError(f"GitHub Work Item Provider failed: {error.decode().strip()}")
            pages = json.loads(output)
            if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
                raise ValueError("GitHub Work Item Provider expected paginated issue arrays")
            identities = set()
            for page in pages:
                for issue in page:
                    if not isinstance(issue, dict) or type(issue.get("number")) is not int:
                        raise ValueError("GitHub Work Item Provider received an invalid issue")
                    if not isinstance(issue.get("html_url", ""), str) or not isinstance(
                        issue.get("milestone") or {}, dict
                    ):
                        raise ValueError(  # noqa: TRY004
                            "GitHub Work Item Provider received invalid issue metadata"
                        )
                    if "pull_request" in issue:
                        continue
                    if epic is not None:
                        milestone = issue.get("milestone") or {}
                        if (milestone.get("title") == epic) == outside:
                            continue
                    number = str(issue["number"])
                    aliases = {number, f"#{number}", f"{self.repository}#{number}"}
                    aliases.add(issue.get("html_url", ""))
                    if not aliases.intersection(excluded):
                        identities.add(number)
            return len(identities)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


def configured_provider(config) -> WorkItemProvider | None:
    """Select explicitly; repository hosting never implies the work-item provider."""
    settings = config.get("work_item_provider", {"type": "agent"})
    if not isinstance(settings, dict):
        raise ValueError("work_item_provider must be a mapping")  # noqa: TRY004
    kind = settings.get("type")
    allowed = {
        "agent": {"type"},
        "file": {"type", "paths", "layout", "timeout", "command", "blocked_paths"},
        "github": {"type", "repository", "ready_label", "executable", "timeout", "blocked_label"},
    }
    if not isinstance(kind, str) or kind not in allowed or set(settings) - allowed[kind]:
        raise ValueError("Unknown work_item_provider type or setting")
    timeout = settings.get("timeout", 30)
    if type(timeout) not in (int, float) or not 0 < timeout < float("inf"):
        raise ValueError("work_item_provider.timeout must be positive and finite")
    if kind == "agent":
        return None
    if kind == "file":
        paths = settings.get("paths", ["backlog/ready"])
        layout = settings.get("layout", "status-folders")
        if layout not in ("status-folders", "status-field"):
            raise ValueError("work_item_provider.layout must be status-folders or status-field")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path.strip() for path in paths)
        ):
            raise ValueError("work_item_provider.paths must be a nonempty list of directories")
        command = settings.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ValueError(
                "work_item_provider.command must invoke the file provider's eligibility observer "
                "as a nonempty argument list; counting stored Status headers is unsupported"
            )
        blocked_paths = settings.get("blocked_paths")
        if blocked_paths is not None and (
            not isinstance(blocked_paths, list)
            or not blocked_paths
            or not all(isinstance(path, str) and path.strip() for path in blocked_paths)
        ):
            raise ValueError(
                "work_item_provider.blocked_paths must be a nonempty list of directories"
            )
        return FileWorkItemProvider(config["project"], paths, layout, command, blocked_paths)
    repository = settings.get("repository")
    label = settings.get("ready_label")
    executable = settings.get("executable", "gh")
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("work_item_provider.repository must be owner/repository")
    if not isinstance(label, str) or not label.strip() or "," in label:
        raise ValueError("work_item_provider.ready_label must be one nonempty label")
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("work_item_provider.executable must be an executable path or name")
    blocked_label = settings.get("blocked_label")
    if blocked_label is not None and (
        not isinstance(blocked_label, str) or not blocked_label.strip() or "," in blocked_label
    ):
        raise ValueError("work_item_provider.blocked_label must be one nonempty label")
    return GitHubWorkItemProvider(config["project"], repository, label, executable, blocked_label)
