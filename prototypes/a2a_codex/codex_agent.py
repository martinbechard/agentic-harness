"""One subscription-authenticated Codex agent, retaining native tools and skills."""
import asyncio
import json
import os
from pathlib import Path

DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"state": {"type": "string", "enum": ["input_required", "completed"]},
                   "text": {"type": "string"}},
    "required": ["state", "text"],
}


def sanitized_environment():
    # Keep normal CLI login/config discovery, but never offer an API-key fallback.
    return {k: v for k, v in os.environ.items()
            if not (k.endswith("API_KEY") or k in {"OPENAI_BASE_URL", "OPENAI_API_BASE"})}


class CodexAgent:
    def __init__(self, workspace: Path, evidence: Path, model=None):
        self.workspace, self.evidence, self.model = workspace.resolve(), evidence, model
        self.process = None
        self.thread_id = None
        self.sequence = 0
        self.requests = {}
        self.events = asyncio.Queue()
        self.turn_ids = []
        self.usage = []
        self.tool_items = []
        self.effective = None
        self.auth_type = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "--listen", "stdio://",
            "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
            "-c", "features.memories=false", "-c", "sandbox_workspace_write.network_access=false",
            cwd=self.workspace, env=sanitized_environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.reader = asyncio.create_task(self._read())
        await self.rpc("initialize", {"clientInfo": {"name": "a2a_codex_prototype", "version": "0.1"}})
        await self.write({"method": "initialized"})
        account = await self.rpc("account/read", {"refreshToken": False})
        self.auth_type = (account.get("account") or {}).get("type")
        if self.auth_type != "chatgpt":
            raise RuntimeError("Subscription ChatGPT authentication required; no fallback")
        params = {"cwd": str(self.workspace), "approvalPolicy": "never", "sandbox": "workspace-write",
                  "modelProvider": "openai", "developerInstructions":
                  "Return a structured decision. When essential user information is absent, ask a specific "
                  "clarification using state input_required and stop. Otherwise return completed with the artifact "
                  "text. Retain completed work across answers. Use native tools and skills when useful. "
                  "Only write within the assigned workspace. Do not invoke other models or delegate this tiny task."}
        if self.model:
            params["model"] = self.model
        result = await self.rpc("thread/start", params)
        self.thread_id = result["thread"]["id"]
        self.effective = {k: result.get(k) for k in ("approvalPolicy", "sandbox", "cwd", "modelProvider", "model")}
        sandbox = result.get("sandbox", {})
        if result.get("approvalPolicy") != "never" or sandbox.get("type") != "workspaceWrite" or sandbox.get("networkAccess"):
            raise RuntimeError("Native effective permissions did not match requested scope")
        if result.get("modelProvider") != "openai" or Path(result["cwd"]).resolve() != self.workspace:
            raise RuntimeError("Native provider/workspace mismatch")
        self.save()

    async def write(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.requests[key] = future
        await self.write({"id": key, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, 60)
        finally:
            self.requests.pop(key, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                event = json.loads(line)
                if "id" in event and "method" not in event:
                    future = self.requests.get(event["id"])
                    if future and not future.done():
                        if "error" in event:
                            future.set_exception(RuntimeError(str(event["error"])))
                        else:
                            future.set_result(event.get("result"))
                elif "id" in event:
                    # No automatic approval or unknown server-request execution.
                    await self.write({"id": event["id"], "error": {"code": -32601, "message": "Unsupported prototype server request"}})
                else:
                    await self.events.put(event)
        finally:
            for future in self.requests.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex app-server exited"))
            await self.events.put({"method": "prototype/eof"})

    async def run(self, text):
        if self.process is None:
            await self.start()
        response = await self.rpc("turn/start", {
            "threadId": self.thread_id, "input": [{"type": "text", "text": text}],
            "outputSchema": DECISION_SCHEMA, "effort": "low",
            "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(self.workspace)],
                              "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
            "approvalPolicy": "never",
        })
        turn_id = response["turn"]["id"]
        self.turn_ids.append(turn_id)
        final = None
        async with asyncio.timeout(300):
            while True:
                event = await self.events.get()
                method, params = event.get("method"), event.get("params", {})
                if method == "prototype/eof":
                    raise RuntimeError("Codex exited during turn")
                if method == "thread/tokenUsage/updated":
                    self.usage.append(params)
                if method == "item/completed" and params.get("threadId") == self.thread_id:
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        final = item.get("text")
                    elif item.get("type") in {"commandExecution", "fileChange"}:
                        self.tool_items.append({"turn_id": params.get("turnId"), "id": item.get("id"),
                                                "type": item.get("type"), "status": item.get("status")})
                if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    if params["turn"].get("status") != "completed":
                        raise RuntimeError("Native turn did not complete: " + str(params["turn"].get("error")))
                    break
        decision = json.loads(final or "null")
        if not isinstance(decision, dict) or decision.get("state") not in {"input_required", "completed"} or not decision.get("text"):
            raise RuntimeError("Missing valid agent decision")
        self.save()
        return decision

    def save(self):
        self.evidence.write_text(json.dumps({"auth_type": self.auth_type, "codex_thread_id": self.thread_id,
            "turn_ids": self.turn_ids, "effective": self.effective, "usage_notifications": self.usage,
            "usage_coverage": "native notifications only; no claim of complete billing or child coverage",
            "native_tool_items": self.tool_items, "memories": False}, indent=2) + "\n")

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            await self.reader
            self.save()
