"""Reload a small, project-local prototype configuration before each invocation."""

import json
import os
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from backlog_harness.contracts import AgentBinding, digest


@dataclass(frozen=True)
class Snapshot:
    file_digest: str
    workspace: Path
    adapter: Path
    node: str
    model: str | None
    effort: str
    auth_context: str

    def binding(self, role):
        mode = "read-only" if role == "reviewer" else "workspace-write"
        permissions = digest(
            {
                "cwd": str(self.workspace),
                "mode": mode,
                "network": False,
                "permission_answers": "reject",
                "auth": "chatgpt",
                "auth_context": self.auth_context,
            }
        )
        return AgentBinding(
            role,
            "codex-acp",
            "acp-prototype",
            str(self.adapter),
            sha256(self.adapter.read_bytes()).hexdigest(),
            "chatgpt",
            role,
            digest([self.model, self.effort]),
            self.file_digest,
            auth_context=self.auth_context,
            permission_digest=permissions,
        )

    def environment(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.endswith("API_KEY")
            and key
            not in {
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "MODEL_PROVIDER",
                "DEFAULT_AUTH_REQUEST",
                "APP_SERVER_LOGS",
                "CODEX_PATH",
                "CODEX_CONFIG",
            }
        }
        settings = {
            "forced_login_method": "chatgpt",
            "model_provider": "openai",
            "features.memories": False,
            "sandbox_workspace_write.network_access": False,
            "model_reasoning_effort": self.effort,
        }
        if self.model:
            settings["model"] = self.model
        env["CODEX_HOME"] = self.auth_context
        env.update(CODEX_CONFIG=json.dumps(settings), INITIAL_AGENT_MODE="workspace-write")
        return env


def reload_config(path):
    content = Path(path).read_bytes()
    data = json.loads(content)
    allowed = {"workspace", "adapter", "node", "model", "effort"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Unsupported prototype configuration")
    workspace = Path(data["workspace"]).resolve(strict=True)
    default_adapter = (
        Path(__file__).resolve().parent.parent
        / "acp_codex/node_modules/@agentclientprotocol/codex-acp/dist/index.js"
    )
    adapter = Path(data.get("adapter", default_adapter)).resolve(strict=True)
    node = shutil.which(data.get("node", "node"))
    if not workspace.is_dir() or not node:
        raise ValueError("Existing workspace and Node executable required")
    return Snapshot(
        sha256(content).hexdigest(),
        workspace,
        adapter,
        node,
        data.get("model"),
        data.get("effort", "low"),
        str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()),
    )
