"""Foreground run controls. Agents own decisions; this loop observes and enforces gates."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

from .contracts import ConfigError, digest, load_config, utcnow
from .evidence import EvidenceError, atomic_json, operation_lock
from .provider import TERMINAL, TransitionBlocked


def dependencies(item):
    header = item.content.split("\n## ", 1)[0]
    matches = re.findall(r"^Dependencies:\s*(.*)$", header, re.MULTILINE)
    if len(matches) > 1:
        raise TransitionBlocked("Duplicate dependency header")
    return (
        [part.strip() for part in matches[0].split(",") if part.strip() and part.strip() != "none"]
        if matches
        else []
    )


class RunController:
    def __init__(self, app, publish=None):
        self.app = app
        self.publish = publish or (lambda value: None)
        self.tasks = {}
        self.admission_open = False
        self.stopping = False
        self.mode = None
        self.state = "Available"
        self.runner = None
        self.last_record = None
        self.path = app.root / "run.json"
        self.block_path = app.root / "scheduling-blocks.json"
        self.blocked = json.loads(self.block_path.read_text()) if self.block_path.exists() else {}
        self.blocked = {
            key: value
            for key, value in self.blocked.items()
            if value.get("reason") != "Another process owns this operation"
        }
        if self.path.exists():
            previous = json.loads(self.path.read_text())
            # Restart never opens admission from the mere existence of a record.
            self.mode = previous.get("mode")
            self.state = (
                "AdmissionPaused"
                if previous.get("state") in {"Running", "AdmissionPaused", "IdleWatch"}
                else "Available"
            )

    def record(self):
        value = {
            "version": 1,
            "state": self.state,
            "mode": self.mode,
            "admission_open": self.admission_open,
            "active_items": sorted(self.tasks),
            "at": utcnow(),
        }
        fingerprint = digest({k: v for k, v in value.items() if k != "at"})
        if fingerprint == self.last_record:
            return
        self.last_record = fingerprint
        atomic_json(self.path, value)
        self.publish(value)

    def pause(self):
        if self.state not in {"Running", "IdleWatch"}:
            raise TransitionBlocked("Pause requires a running or watching run")
        self.admission_open = False
        self.state = "AdmissionPaused"
        self.record()

    def resume(self):
        if self.state != "AdmissionPaused":
            raise TransitionBlocked("Resume requires paused admission")
        evidence = self.app.reconcile()
        if any(
            v.get("quiescent") is False and v.get("item_id") not in self.tasks
            for v in evidence
            if "invocation_id" in v
        ):
            raise TransitionBlocked("Prior execution is not quiescent; retain the existing run")
        self.admission_open = True
        self.state = "Running"
        self.record()

    async def stop(self):
        self.admission_open = False
        self.stopping = True
        self.state = "Stopping"
        self.record()
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        evidence = self.app.reconcile()
        unresolved = any(v.get("quiescent") is False for v in evidence if "invocation_id" in v)
        self.state = "Unresolved" if unresolved else "Available"
        self.record()
        return {"state": self.state, "admission_open": False, "evidence": evidence}

    def eligible(self, items):
        by_id = {item.item_id: item for item in items}
        selected = []
        for item in items:
            if item.state not in {"Ready", "Starting", "Running"} or item.item_id in self.tasks:
                continue
            required = dependencies(item)
            if any(name not in by_id or by_id[name].state != "Completed" for name in required):
                continue
            premise = digest([item.revision, self.app.config.file_digest])
            if self.blocked.get(item.item_id, {}).get("premise") == premise:
                continue
            selected.append((item, premise))
        return selected

    async def execute(self, item, premise):
        try:
            result = await self.app.run_item(item.item_id)
            self.publish({"item_id": item.item_id, "result": result})
        except asyncio.CancelledError:
            self.publish(
                {"item_id": item.item_id, "interruption": "requested; inspect reconciled outcome"}
            )
            raise
        except EvidenceError as exc:
            if str(exc) != "Another process owns this operation":
                raise
            self.publish(
                {"item_id": item.item_id, "waiting": "Existing local operation owns the lock"}
            )
        except (ValueError, OSError, RuntimeError) as exc:
            self.blocked[item.item_id] = {"premise": premise, "reason": str(exc), "at": utcnow()}
            atomic_json(self.block_path, self.blocked)
            self.publish({"item_id": item.item_id, "blocked": str(exc)})

    async def run(self, mode):
        if mode not in {"until-terminal", "watch"}:
            raise TransitionBlocked("Unknown run mode")
        if self.state not in {"Available", "AdmissionPaused"}:
            raise TransitionBlocked("A run is already active")
        with operation_lock(self.app.root / "foreground-run.lock"):
            self.app.reconcile()
            self.mode, self.state, self.admission_open, self.stopping = mode, "Running", True, False
            self.record()
            try:
                while not self.stopping:
                    for item_id in list(self.tasks):
                        if self.tasks[item_id].done():
                            await self.tasks.pop(item_id)
                    try:
                        current = load_config(self.app.config_path)
                    except ConfigError as exc:
                        self.admission_open, self.state = False, "AdmissionPaused"
                        message = str(exc)
                        if getattr(self, "configuration_error", None) != message:
                            self.configuration_error = message
                            self.publish(
                                {"configuration_error": message, "generation_fenced": True}
                            )
                        self.record()
                        await asyncio.sleep(1)
                        continue
                    if (
                        current.repository != self.app.config.repository
                        or current.operational_root != self.app.root
                    ):
                        raise TransitionBlocked("Run storage identity changed")
                    if (
                        self.tasks
                        and current.data["workflow"]["mode"]
                        != self.app.config.data["workflow"]["mode"]
                    ):
                        self.admission_open, self.state = False, "AdmissionPaused"
                        self.record()
                        await asyncio.sleep(min(self.app.config.data["poll_seconds"], 1))
                        continue
                    self.app.config = current
                    items = self.app.provider.snapshot()
                    limit = (
                        1
                        if current.data["workflow"]["mode"] == "SOLO"
                        else current.data["max_active_invocations"]
                    )
                    if self.admission_open:
                        for item, premise in self.eligible(items):
                            if len(self.tasks) >= limit:
                                break
                            self.tasks[item.item_id] = asyncio.create_task(
                                self.execute(item, premise)
                            )
                    if not self.tasks and all(item.state in TERMINAL for item in items):
                        evidence = self.app.reconcile()
                        unresolved = any(v["outcome"] == "unresolved" for v in evidence)
                        if not unresolved:
                            counts = dict(Counter(item.state for item in items))
                            result = {
                                "outcome": "successful"
                                if all(i.state == "Completed" for i in items)
                                else "settled_with_nondelivery",
                                "counts": counts,
                            }
                            if mode == "until-terminal":
                                self.state, self.admission_open = "Available", False
                                self.record()
                                return result
                            self.state = "IdleWatch"
                    elif self.admission_open:
                        self.state = "Running"
                    self.record()
                    await asyncio.sleep(min(current.data["poll_seconds"], 1))
            finally:
                if self.tasks:
                    await self.stop()
            return {"outcome": "stopped", "state": self.state}
