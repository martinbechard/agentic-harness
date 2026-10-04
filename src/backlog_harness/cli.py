"""Foreground harness with human pause/resume controls."""

import argparse
import asyncio
import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

from .control import SchedulingControl
from .decision import prepare, submit
from .engine import Harness
from .process import Agents
from .provider_lock import CoordinationUnavailable, coordinator


class Events:
    """Write every activity to the console, JSONL log, and OTLP JSON log export."""

    def __init__(self, root, level="INFO"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run = uuid.uuid4().hex
        self.level = level

    def __call__(self, event, *, level="INFO", **fields):
        severity = {"DEBUG": 5, "INFO": 9, "ERROR": 17}
        if severity[level] < severity[self.level]:
            return
        timestamp = time.time_ns()
        entry = {"event": event, "run": self.run, "level": level, **fields}
        line = json.dumps(entry)
        print(line, flush=True)
        with (self.root / "activities.jsonl").open("a") as stream:
            stream.write(json.dumps({"timeUnixNano": str(timestamp), **entry}) + "\n")
        export = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "agentic-harness"}}
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "backlog_harness"},
                            "logRecords": [
                                {
                                    "timeUnixNano": str(timestamp),
                                    "severityNumber": severity[level],
                                    "severityText": level,
                                    "body": {"stringValue": line},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with (self.root / "otel.jsonl").open("a") as stream:
            stream.write(json.dumps(export) + "\n")


def load_config(path):
    config = yaml.safe_load(path.read_text())
    allowed = {
        "project",
        "state",
        "access",
        "development",
        "merge",
        "access_timeout",
        "decision_timeout",
        "scheduling",
        "thread_cleanup",
        "log_level",
    }
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError(
            "Configuration must contain only project, state, agent commands, and timing settings"
        )
    for key in ("project", "state"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a directory path")
        expanded = Path(value).expanduser()
        config[key] = str((path.parent / expanded).resolve())
    if not Path(config["project"]).is_dir():
        raise ValueError("project directory does not exist")
    for role in ("access", "development", "merge"):
        command = config.get(role)
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ValueError(f"{role} must be a nonempty command argument list")
    for setting in ("access_timeout", "decision_timeout"):
        duration = config.get(setting, 120)
        if type(duration) not in (int, float) or not 0 < duration < float("inf"):
            raise ValueError(f"{setting} must be positive and finite")
    scheduling = config.get("scheduling", {})
    if not isinstance(scheduling, dict) or set(scheduling) - {
        "capacity",
        "timeout",
        "poll_interval",
        "merge_interval",
        "merge_timeout",
        "heartbeat_interval",
    }:
        raise ValueError("Unknown scheduling setting")
    if config.get("log_level", "INFO") not in ("DEBUG", "INFO", "ERROR"):
        raise ValueError("log_level must be DEBUG, INFO, or ERROR")
    cleanup = config.get("thread_cleanup", {})
    if not isinstance(cleanup, dict) or set(cleanup) - {"enabled", "interval", "completed_age"}:
        raise ValueError("Unknown thread_cleanup setting")
    if type(cleanup.get("enabled", True)) is not bool:
        raise ValueError("thread_cleanup.enabled must be boolean")
    for key, default in (("interval", 900), ("completed_age", 3600)):
        value = cleanup.get(key, default)
        if type(value) not in (int, float) or not 0 < value < float("inf"):
            raise ValueError(f"thread_cleanup.{key} must be positive and finite")
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--epic")
    mode.add_argument("--decision", type=Path, metavar="REQUEST.json")
    mode.add_argument("--cleanup-once", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        decision = prepare(args.decision, config) if args.decision else None
        events = Events(config["state"], config.get("log_level", "INFO"))
        if args.cleanup_once:
            from .codex import cleanup_threads

            raise SystemExit(0 if asyncio.run(cleanup_threads(config, events, once=True)) else 1)
        configured_capacity = config.get("scheduling", {}).get("capacity", 1)
        scheduling_control = SchedulingControl(config["state"], events.run, configured_capacity)
        agents = Agents(config, events)
        scheduling = {**config.get("scheduling", {}), "capacity": scheduling_control.capacity}
        harness = Harness(
            agents,
            agents.deliver,
            agents.integrate,
            events,
            epic=args.epic,
            scheduling_revision=scheduling_control.revision,
            scheduling_request_id=scheduling_control.request_id,
            **scheduling,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    if decision is not None:
        result = asyncio.run(submit(agents, decision))
        events("decision_result", project=config["project"], **result)
        raise SystemExit(
            {
                "applied": 0,
                "already_applied": 0,
                "already_resolved": 3,
                "rejected": 3,
                "unknown": 1,
            }[result["status"]]
        )
    controls = queue.Queue()

    def read_controls():
        for line in sys.stdin:
            controls.put(line.strip())

    threading.Thread(target=read_controls, daemon=True).start()
    events("started", instruction="Human user: enter pause or resume; Ctrl-C exits")

    async def run():
        async def control_loop():
            while True:
                while not controls.empty():
                    command = controls.get_nowait()
                    if command == "pause":
                        harness.pause()
                    elif command == "resume":
                        harness.resume()
                    else:
                        events("unknown_control", instruction="Human user: enter pause or resume")
                await scheduling_control.process(harness)
                await asyncio.sleep(0.05)

        controller = asyncio.create_task(control_loop())
        from .codex import cleanup_threads

        cleaner = asyncio.create_task(cleanup_threads(config, events))
        try:
            await harness.run()
        finally:
            controller.cancel()
            cleaner.cancel()
            await asyncio.gather(controller, cleaner, return_exceptions=True)

    try:
        with coordinator(config):
            asyncio.run(run())
    except KeyboardInterrupt:
        events("exiting")
    except CoordinationUnavailable as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
