"""Scripted fixture subprocess; no network, SDK transport, or model call."""

import asyncio
import json
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workflow import run


class ScriptedClient:
    def __init__(self, snapshot, role):
        self.snapshot, self.role = snapshot, role
        self.root = snapshot.workspace.parent

    def trace(self, method, **facts):
        with (self.root / "calls.jsonl").open("a") as output:
            output.write(
                json.dumps(
                    {
                        "method": method,
                        "role": self.role,
                        "config_digest": self.snapshot.file_digest,
                        "model": self.snapshot.model,
                        **facts,
                    }
                )
                + "\n"
            )

    async def __aenter__(self):
        self.trace("child-start")
        return self

    async def __aexit__(self, *exc):
        self.trace("child-stop")

    async def new_session(self):
        identity = "native-producer" if self.role == "producer" else "native-reviewer"
        self.session_exchange = {
            "method": "session/new",
            "request": {"cwd": str(self.snapshot.workspace), "mcpServers": []},
            "response": {"sessionId": identity},
        }
        self.trace("session/new", session_id=identity)
        return identity

    async def load_session(self, identity):
        self.session_exchange = {
            "method": "session/load",
            "request": {
                "cwd": str(self.snapshot.workspace),
                "sessionId": identity,
                "mcpServers": [],
            },
            "response": {},
        }
        self.trace("session/load", session_id=identity)

    async def prompt(self, identity, prompt):
        self.trace("session/prompt", session_id=identity, prompt=prompt)
        if (self.root / "uncertain").exists():
            raise RuntimeError("Fixture lost response after submission")
        if self.role == "reviewer":
            candidate = sha256((self.snapshot.workspace / "artifact.txt").read_bytes()).hexdigest()
            value = {
                "verdict": "ACCEPT",
                "candidate": candidate,
                "evidence": "Fixture checks exact artifact bytes",
            }
        elif not (self.snapshot.workspace / "completed-work.txt").exists():
            (self.snapshot.workspace / "completed-work.txt").write_text("prepared once\n")
            value = {"state": "input_required", "text": "Which language?"}
        else:
            target = self.snapshot.workspace / "artifact.txt"
            target.write_text("Bonjour\n")
            value = {
                "state": "candidate",
                "candidate": sha256(target.read_bytes()).hexdigest(),
                "artifact": "artifact.txt",
            }
        return {
            "text": json.dumps(value),
            "response": {"stopReason": "end_turn"},
            "events": [],
            "auth": {"type": "fixture, not authentication"},
        }


def after_result(reference):
    marker = Path(sys.argv[1]).parent / "crash-after-result"
    if marker.exists():
        marker.unlink()
        raise RuntimeError("Fixture finished agent before graph checkpoint")


if __name__ == "__main__":
    config = Path(sys.argv[1])
    mode = sys.argv[2]
    kwargs = {"prompt": "Prepare a greeting in my preferred language"} if mode == "start" else {}
    if mode == "answer":
        kwargs["answer"] = json.loads(sys.argv[3])
    print(
        json.dumps(
            asyncio.run(
                run(
                    config.parent / "evidence",
                    "fixture-execution",
                    config,
                    factory=ScriptedClient,
                    after_result=after_result,
                    **kwargs,
                )
            )
        )
    )
