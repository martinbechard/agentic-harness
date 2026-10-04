"""Optional, read-only Work Item Provider protocol for inexpensive Ready counts."""

import asyncio
import json
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode


class WorkItemProvider(Protocol):
    async def ready_count(self, *, epic: str | None, outside: bool, excluded: list[str]) -> int:
        """Count stored Ready candidates; selection and reconciliation remain agent-owned."""


class FileWorkItemProvider:
    def __init__(self, project, paths, layout):
        self.project = Path(project)
        self.paths = [self.project / path for path in paths]
        self.layout = layout

    async def ready_count(self, *, epic, outside, excluded):
        return await asyncio.to_thread(self.count, epic, outside, excluded)

    def count(self, epic, outside, excluded):
        scope = (self.project / epic).resolve() if epic is not None else None
        identities = set()
        for root in self.paths:
            # A missing configured queue is a configuration/read error, not an empty backlog.
            if not root.is_dir():
                raise ValueError(f"Work Item Provider directory does not exist: {root}")
            for directory, _, files in root.walk(on_error=raise_scan_error):
                for name in files:
                    if name.endswith(".md"):
                        identities.update(
                            self.ready_identity(directory / name, scope, outside, excluded)
                        )
        return len(identities)

    def ready_identity(self, path, scope, outside, excluded):
        if path.name.lower() in {"index.md", "readme.md"}:
            return set()
        if scope is not None and path.resolve().is_relative_to(scope) == outside:
            return set()
        # Lifecycle fields belong to the record header, never examples/history below it.
        header = re.split(r"^##\s", path.read_text(), maxsplit=1, flags=re.MULTILINE)[0]
        statuses = [
            value.strip() for value in re.findall(r"^Status:[ \t]*([^\n]*)", header, re.MULTILINE)
        ]
        if self.layout == "status-folders":
            if statuses and statuses != ["Ready"]:
                raise ValueError(f"Ready folder and Status disagree: {path}")
        elif statuses != ["Ready"]:
            return set()
        return {path.stem} - set(excluded)


def raise_scan_error(error):
    """A failed directory scan must not masquerade as an empty Ready queue."""
    raise error


class GitHubWorkItemProvider:
    def __init__(self, project, repository, ready_label, executable):
        self.project, self.repository = project, repository
        self.ready_label, self.executable = ready_label, executable

    async def ready_count(self, *, epic, outside, excluded):
        query = urlencode({"state": "open", "labels": self.ready_label, "per_page": 100})
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
        "file": {"type", "paths", "layout", "timeout"},
        "github": {"type", "repository", "ready_label", "executable", "timeout"},
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
        return FileWorkItemProvider(config["project"], paths, layout)
    repository = settings.get("repository")
    label = settings.get("ready_label")
    executable = settings.get("executable", "gh")
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("work_item_provider.repository must be owner/repository")
    if not isinstance(label, str) or not label.strip() or "," in label:
        raise ValueError("work_item_provider.ready_label must be one nonempty label")
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("work_item_provider.executable must be an executable path or name")
    return GitHubWorkItemProvider(config["project"], repository, label, executable)
