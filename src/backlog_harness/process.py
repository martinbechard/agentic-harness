"""Shell-free subprocesses; commands read HARNESS_REQUEST and write HARNESS_RESULT."""

import asyncio
import codecs
import json
import os
import signal
import uuid
from pathlib import Path

from .engine import Item, Outcome
from .provider_lock import CoordinationUnavailable, provider_lock, require_coordinator


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
        self.exit_reported = False
        self.stop_requested = False
        self.identity = {
            "invocation": self.id,
            "role": request["role"],
            "item": request.get("item", {}).get("id"),
            "pid": self.process.pid,
        }
        self.output = asyncio.create_task(self.read_output())
        self.emit("agent_started", **self.identity)
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
            self.stop_requested = self.process.returncode is None
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.emit(
                    "agent_group_stopped",
                    **self.identity,
                    reason="stop_requested" if self.stop_requested else "cleanup",
                )
            except ProcessLookupError:
                pass
        await self.process.wait()
        await asyncio.shield(self.output)
        if not self.exit_reported:
            self.exit_reported = True
            self.emit(
                "agent_exited",
                **self.identity,
                exit_code=self.process.returncode,
                stop_requested=self.stop_requested,
            )

    async def response(self):
        # A stopped parent can leave children holding its output pipe open. Observe
        # the parent exit directly, then reap the whole invocation before recovery.
        while self.process.returncode is None:
            await asyncio.sleep(0.02)
        await self.kill()
        result_state = "missing"
        try:
            if not self.result.exists():
                return None
            result_state = "invalid"
            if self.result.stat().st_size > 1_048_576:
                raise ValueError("Agent result exceeds 1 MiB")
            value = json.loads(self.result.read_text())
            if not isinstance(value, dict):
                raise ValueError("Agent result must be an object")  # noqa: TRY004
            result_state = "reported"
            return value
        finally:
            self.emit("agent_result", **self.identity, result_state=result_state)

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
        self.invocations = {}
        self.scheduling = {"capacity": 1, "mode": "solo", "revision": 0, "request_id": None}

    def set_scheduling(self, scheduling):
        """Update the accepted admission context supplied to later agent invocations."""
        self.scheduling = dict(scheduling)

    def scheduling_instruction(self):
        """Explain the narrow authority of the current human-selected admission limit."""
        capacity = self.scheduling["capacity"]
        mode = self.scheduling["mode"]
        return (
            f"The human user selected {mode} scheduling with capacity {capacity} for this "
            "harness run. This choice governs only concurrently admitted Work items and "
            "supersedes project guidance that fixes Work item scheduling to SOLO for this run. "
            "It does not change claim-free restrictions, resource coordination, provider "
            "lifecycle rules, agent collaboration, serial merge checks, or delivery authority."
        )

    def running_invocations(self):
        return [
            agent.identity
            for agent in self.invocations.values()
            if agent.process.returncode is None
        ]

    async def start(self, role, payload, cwd=None):
        def emit(event, **fields):
            if event == "agent_exited":
                self.invocations.pop(fields["invocation"], None)
            self.emit(event, **fields)

        agent = await Process.start(
            self.config[role],
            {
                "role": role,
                "work_item_provider": self.config.get("work_item_provider"),
                **payload,
            },
            cwd or self.config["project"],
            self.config["state"],
            emit,
        )
        self.invocations[agent.id] = agent
        return agent

    async def ask(self, action, **payload):
        decision = action == "decision"
        identity = {"action": action}
        if decision:
            identity.update(
                decision_id=payload["submission"]["decision_id"],
                item=payload["submission"]["item_id"],
            )
        budget = self.config.get("decision_timeout", 120) if decision else None
        self.emit("provider_waiting", **identity, timeout_seconds=budget)
        started = False
        try:
            async with asyncio.timeout(budget):
                async with self.access_lock, provider_lock(self.config["state"]):
                    if decision:
                        require_coordinator(self.config)
                    # From here a child may start: timeout is conservatively unknown.
                    started = True
                    return await self.ask_locked(action, payload, identity)
        except TimeoutError:
            self.emit("provider_timeout", **identity, phase="running" if started else "waiting")
            if not started:
                raise CoordinationUnavailable(
                    "Decision timed out waiting for provider access; no provider mutation was "
                    "attempted. Retain and retry the identical saved submission."
                ) from None
            raise RuntimeError(
                "Decision provider timed out; persistence is unconfirmed. Retain and retry "
                "the identical saved submission to recover its recorded outcome."
            ) from None

    async def ask_locked(self, action, payload, identity):
        self.emit("provider_request", action=action)
        cwd = payload.pop("cwd", None)
        agent = await self.start("access", {"action": action, **payload}, cwd)
        self.emit(
            "provider_started",
            **identity,
            invocation=agent.id,
            role="access",
            pid=agent.process.pid,
        )
        try:
            result = await asyncio.wait_for(
                agent.response(),
                None if action == "decision" else self.config.get("access_timeout", 120),
            )
            if agent.process.returncode != 0 or not isinstance(result, dict):
                raise RuntimeError("Work item access agent failed")
            required = {
                "ready": "items",
                "status": "status",
                "failure": "transient",
                "epic_complete": "complete",
                "hold": "updated",
                "decision": "status",
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

    async def ready(self, limit, epic, outside, excluded, scheduling=None):
        if scheduling is not None:
            self.set_scheduling(scheduling)
        response = await self.ask(
            "ready",
            limit=limit,
            epic=epic,
            outside=outside,
            excluded=excluded,
            workspaces=list(self.workspaces.values()),
            instruction=self.scheduling_instruction()
            + " Reconcile lifecycle before selecting ready items. For filesystem providers, "
            "inspect the configured primary backlog and committed checkpoints in assigned "
            "worktrees, including assignments discoverable through provider records after a "
            "harness restart. When a checkpoint establishes a newer waiting state (User Action "
            "Required, Blocked, or Holding), publish a lifecycle-only update on the configured "
            "primary branch through the provider's conditional transaction procedure. This "
            "caller authorizes that main-side lifecycle reconciliation independently of product "
            "acceptance or integration; do not merge or cherry-pick unfinished product changes. "
            "Preserve the exact question, decision identities, candidate, assignment, history "
            "and evidence, including recorded blocker reason, owner, next action and waiting time. "
            "Preserve unrelated working-tree and index contents; commit only the owned lifecycle "
            "paths. Compare revision history, not timestamps or status precedence alone: "
            "a stale historical branch must not reopen delivered completion or undo a newer "
            "human answer, cancellation, or resumption. A newer answer or stored Ready label is "
            "not proof that a pending question is resolved: verify that the exact answer "
            "satisfies that question and its conditions before reconciling the assignment "
            "or dispatching it. A changed question, partial answer or conflicting resolution "
            "must remain excluded with both versions preserved until the conflict is resolved. "
            "Never publish dirty worktree evidence "
            "as a committed checkpoint or mutate an excluded active assignment. If authority "
            "or competing revisions are ambiguous, preserve both, exclude unsafe dispatch, "
            "and report the concrete conflict and next action. Do not claim reconciliation "
            "succeeded when only an exclusion or activity note was recorded. Read back and "
            "verify the published lifecycle, question and revision before reporting success. "
            "Unchanged repeat scans must not append duplicate history or create no-op commits. "
            "Then return ready items with satisfied dependencies from the reconciled provider. "
            "Prepare their worktrees and branches; respect exclusions. Consult known workspaces "
            "for unmerged status updates: never redispatch delivered or user-action items "
            "merely because main still has an older ready file. Follow provider conventions "
            "to recognize subsequent human changes back to ready. For an item resumed after human "
            "input, before returning it, "
            "reconcile its latest authoritative human decision into the exact assigned worktree "
            "and branch, preserving candidate commits, local progress and unrelated edits. "
            "For Git-backed filesystem items, verify the decision is committed at its source "
            "and publish/reconcile the owned item changes under provider conventions; do not "
            "rely on a dirty main copy or merely change the local status. Read back the assigned "
            "workspace's item and verify the original decision ID, exact answer/approval and "
            "resolved question before returning it. If publication or reconciliation is incomplete "
            "or conflicting, omit that item and report the concrete blocker; never dispatch the "
            "stale question. Do not mark items running "
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
            self.scheduling_instruction()
            + " Read this workspace's current work item and recorded human decisions before starting; "
            "honor the exact recorded answer/approval and do not re-raise a resolved question. "
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
                "instruction": self.scheduling_instruction()
                + " Check for work ready to integrate using the project's convention (approved PR, "
                "work item status and branch, or configured equivalent). For each eligible item, "
                "prepare integration against the latest target, run required tests on the combined "
                "result, and merge only if tests pass. Integrate only committed candidates; do not "
                "treat an uncommitted running-worktree status as ready. Pin the candidate before "
                "testing. Update the work item and report the outcome. "
                "Reconcile conflicting status/delivery information from separate attempts even "
                "when Git merges cleanly."
            },
        )
