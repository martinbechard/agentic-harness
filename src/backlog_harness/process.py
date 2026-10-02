"""Shell-free subprocesses; commands read HARNESS_REQUEST and write HARNESS_RESULT."""

import asyncio
import codecs
import json
import os
import signal
import uuid
from pathlib import Path

from .engine import Item, Outcome


class Process:
    @classmethod
    async def start(cls, command, request, cwd, root, emit):
        self = cls()
        self.emit = emit
        self.id = uuid.uuid4().hex
        folder = Path(root) / self.id
        folder.mkdir(parents=True)
        self.result = folder / "result.json"
        request_path = folder / "request.json"
        request_path.write_text(json.dumps(request))
        self.output_path = folder / "output.log"
        env = {
            **os.environ,
            "HARNESS_REQUEST": str(request_path.resolve()),
            "HARNESS_RESULT": str(self.result.resolve()),
        }
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.killed = False
        self.output = asyncio.create_task(self.read_output())
        self.emit("agent_started", invocation=self.id, pid=self.process.pid, role=request["role"])
        return self

    async def read_output(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        with self.output_path.open("wb") as stream:
            while chunk := await self.process.stdout.read(8192):
                stream.write(chunk)
                stream.flush()
                self.emit("agent_output", invocation=self.id, text=decoder.decode(chunk))
            tail = decoder.decode(b"", final=True)
            if tail:
                self.emit("agent_output", invocation=self.id, text=tail)

    async def kill(self):
        if not self.killed:
            self.killed = True
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.emit("agent_group_stopped", invocation=self.id)
            except ProcessLookupError:
                pass
        await self.process.wait()
        await asyncio.shield(self.output)

    async def response(self):
        # A stopped parent can leave children holding its output pipe open. Observe
        # the parent exit directly, then reap the whole invocation before recovery.
        while self.process.returncode is None:
            await asyncio.sleep(0.02)
        await self.kill()
        if not self.result.exists():
            return None
        if self.result.stat().st_size > 1_048_576:
            raise ValueError("Agent result exceeds 1 MiB")
        value = json.loads(self.result.read_text())
        if not isinstance(value, dict):
            raise ValueError("Agent result must be an object")  # noqa: TRY004
        return value

    async def wait(self):
        try:
            value = await self.response()
        except (ValueError, OSError) as exc:
            return Outcome("failed", detail=f"Invalid agent result: {exc}")
        if value is None:
            return None
        status = value.get("status")
        if status not in {"success", "failed", "user_action_required"}:
            return Outcome("failed", detail="Invalid agent outcome")
        if self.process.returncode != 0:
            return Outcome("failed", detail="Agent exited unsuccessfully")
        if type(value.get("transient", False)) is not bool:
            return Outcome("failed", detail="transient must be boolean")
        if not isinstance(value.get("detail", ""), str):
            return Outcome("failed", detail="detail must be text")
        return Outcome(status, value.get("transient", False), value.get("detail", ""))


class Agents:
    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.access_lock = asyncio.Lock()
        self.workspaces = {}

    async def start(self, role, payload, cwd=None):
        return await Process.start(
            self.config[role],
            {"role": role, **payload},
            cwd or self.config["project"],
            self.config["state"],
            self.emit,
        )

    async def ask(self, action, **payload):
        async with self.access_lock:
            self.emit("provider_request", action=action)
            cwd = payload.pop("cwd", None)
            agent = await self.start("access", {"action": action, **payload}, cwd)
            try:
                result = await asyncio.wait_for(
                    agent.response(), self.config.get("access_timeout", 120)
                )
                if agent.process.returncode != 0 or not isinstance(result, dict):
                    raise RuntimeError("Work item access agent failed")
                required = {
                    "ready": "items",
                    "status": "status",
                    "failure": "transient",
                    "epic_complete": "complete",
                    "hold": "updated",
                }[action]
                if required not in result:
                    raise ValueError(f"Provider response missing {required}")
                if (
                    action in {"failure", "epic_complete", "hold"}
                    and type(result[required]) is not bool
                ):
                    raise ValueError(f"Provider {required} must be boolean")
                if action == "hold" and not result["updated"]:
                    raise RuntimeError("Provider did not put item on hold")
                if action == "status" and not isinstance(result["status"], str):
                    raise ValueError("Provider status must be text")
                self.emit("provider_response", action=action)
                return result
            except TimeoutError:
                raise RuntimeError("Work item access agent timed out") from None
            finally:
                await agent.kill()

    async def ready(self, limit, epic, outside, excluded):
        response = await self.ask(
            "ready",
            limit=limit,
            epic=epic,
            outside=outside,
            excluded=excluded,
            workspaces=list(self.workspaces.values()),
            instruction="Return ready items with satisfied dependencies. "
            "Prepare their worktrees and branches; respect exclusions. Consult known workspaces "
            "for unmerged status updates: never redispatch delivered or user-action items "
            "merely because main still has an older ready file. Follow provider conventions "
            "to recognize subsequent human changes back to ready. Do not mark items running "
            "during selection; the development agent does that when starting.",
        )
        try:
            items = [Item(**item) for item in response["items"]]
        except (TypeError, KeyError) as exc:
            raise ValueError(
                "Provider items must contain id, absolute worktree, and branch"
            ) from exc
        self.workspaces.update({item.id: item.__dict__ for item in items})
        return items

    async def status(self, item):
        return (await self.ask("status", item=item.__dict__, cwd=item.worktree))["status"]

    async def failure(self, item, reason):
        return (await self.ask("failure", item=item.__dict__, reason=reason, cwd=item.worktree))[
            "transient"
        ] is True

    async def hold(self, item):
        await self.ask("hold", item=item.__dict__, cwd=item.worktree)

    async def epic_complete(self, epic):
        return (await self.ask("epic_complete", epic=epic))["complete"] is True

    async def deliver(self, item, interrupted):
        instruction = (
            "Mark this work item running when starting, deliver it, and update its status and "
            "delivery information directly. Leave integration to the merge agent. "
            "Record questions/actions in the item and set User Action required when needed. "
            "Record discovered defects through the provider in the current worktree; "
            "the development agent fixes blocking defects before continuing. "
            "Publish filesystem work item changes with the delivery merge. "
            "Report success, failed (with transient boolean), or user_action_required."
        )
        if interrupted:
            instruction += (
                " This work was previously started but interrupted. Evaluate the existing "
                "work and item state first, in this same worktree and branch. Resume if "
                "possible; otherwise discard only the interrupted attempt's work, preserve "
                "unrelated changes, and start over."
            )
        return await self.start(
            "development", {"item": item.__dict__, "instruction": instruction}, item.worktree
        )

    async def integrate(self):
        return await self.start(
            "merge",
            {
                "instruction": "Check for work ready to integrate using the project's convention (approved PR, "
                "work item status and branch, or configured equivalent). For each eligible item, "
                "prepare integration against the latest target, run required tests on the combined "
                "result, and merge only if tests pass. Integrate only committed candidates; do not "
                "treat an uncommitted running-worktree status as ready. Pin the candidate before "
                "testing. Update the work item and report the outcome. "
                "Reconcile conflicting status/delivery information from separate attempts even "
                "when Git merges cleanly."
            },
        )
