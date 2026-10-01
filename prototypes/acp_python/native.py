"""Official Python ACP SDK; maintained codex-acp owns Codex's native agent loop."""

import asyncio
import os
import signal
from contextlib import suppress

import acp
from acp.schema import (
    ClientCapabilities,
    Implementation,
    RequestPermissionResponse,
    TextContentBlock,
)


class NativeClient:
    def __init__(self, snapshot, role):
        self.snapshot, self.role = snapshot, role
        self.process = None
        self.connection = None
        self.messages = {}
        self.last_message = None
        self.events = []
        self.auth = None
        self.auth_status = None

    async def __aenter__(self):
        try:
            self.auth_status = asyncio.get_running_loop().create_future()
            self.process = await asyncio.create_subprocess_exec(
                self.snapshot.node,
                str(self.snapshot.adapter),
                cwd=self.snapshot.workspace,
                env=self.snapshot.environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.connection = acp.connect_to_agent(self, self.process.stdin, self.process.stdout)
            self.initialized = await asyncio.wait_for(
                self.connection.initialize(
                    acp.PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="northstar-python-prototype", version="0.1"),
                ),
                30,
            )
            status = await asyncio.wait_for(self.auth_status, 30)
            self.auth = {"kind": status.get("kind"), "label": status.get("label")}
            if status.get("kind") != "account" or not str(status.get("label", "")).startswith(
                "ChatGPT"
            ):
                raise RuntimeError("ChatGPT subscription authentication required; no fallback")
            return self
        except BaseException:
            await self.close()
            raise

    async def new_session(self):
        result = await asyncio.wait_for(
            self.connection.new_session(str(self.snapshot.workspace), mcp_servers=[]), 30
        )
        self.session_exchange = {
            "method": "session/new",
            "request": {"cwd": str(self.snapshot.workspace), "mcpServers": []},
            "response": result.model_dump(mode="json", by_alias=True),
        }
        await self._mode(result.session_id)
        return result.session_id

    async def load_session(self, session_id):
        result = await asyncio.wait_for(
            self.connection.load_session(str(self.snapshot.workspace), session_id, mcp_servers=[]),
            30,
        )
        self.session_exchange = {
            "method": "session/load",
            "request": {
                "cwd": str(self.snapshot.workspace),
                "sessionId": session_id,
                "mcpServers": [],
            },
            "response": result.model_dump(mode="json", by_alias=True),
        }
        await self._mode(session_id)

    async def _mode(self, session_id):
        await asyncio.wait_for(
            self.connection.set_session_mode(
                session_id, "read-only" if self.role == "reviewer" else "workspace-write"
            ),
            30,
        )

    async def prompt(self, session_id, text):
        # Session/load may replay history; keep only this invocation's messages.
        self.messages, self.last_message, self.events = {}, None, []
        response = await asyncio.wait_for(
            self.connection.prompt(session_id, [TextContentBlock(type="text", text=text)]), 300
        )
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"Incomplete ACP prompt: {response.stop_reason}")
        return {
            "text": self.messages.get(self.last_message, ""),
            "response": response.model_dump(mode="json", by_alias=True),
            "events": self.events,
            "auth": self.auth,
        }

    async def session_update(self, session_id, update, **kwargs):
        value = update.model_dump(mode="json", by_alias=True)
        kind = value.get("sessionUpdate")
        if kind == "agent_message_chunk" and value.get("content", {}).get("type") == "text":
            identity = value.get("messageId", "unidentified")
            self.messages[identity] = self.messages.get(identity, "") + value["content"]["text"]
            self.last_message = identity
        elif kind in {"tool_call", "tool_call_update", "usage_update"}:
            keep = {
                key: value[key]
                for key in ("sessionUpdate", "toolCallId", "status", "kind", "used", "size", "cost")
                if key in value
            }
            self.events.append({"session_id": session_id, **keep})
        # Thought streams, arbitrary tool output and auth account details are omitted.

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.events.append({"session_id": session_id, "permission": "rejected"})
        return RequestPermissionResponse.model_validate({"outcome": {"outcome": "cancelled"}})

    async def ext_notification(self, method, params):
        if method == "auth/status_update" and self.auth_status and not self.auth_status.done():
            # The adapter advertises this extension and derives it from native account/read.
            self.auth_status.set_result(params.get("authStatus", {}))

    def on_connect(self, connection):
        pass

    async def close(self):
        if not self.process:
            return
        if self.connection:
            with suppress(Exception):
                await asyncio.wait_for(self.connection.close(), 2)
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await asyncio.wait_for(self.process.wait(), 3)
        # Leader exit alone is not evidence its Codex descendant exited.
        for _ in range(30):
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                self.cleanup = {"process_group_gone": True}
                return
            await asyncio.sleep(0.1)
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        await asyncio.sleep(0.1)
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            self.cleanup = {"process_group_gone": True}
            return
        raise RuntimeError("Native process group did not exit")

    async def __aexit__(self, *exc):
        await self.close()
