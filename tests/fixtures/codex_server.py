"""Local JSON-RPC server double used through the real Codex Python SDK."""

import json
import os
import sys
import time
from pathlib import Path

capture, mode = Path(sys.argv[1]), sys.argv[2]


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    with capture.open("a") as out:
        out.write(json.dumps(message) + "\n")
    method = message["method"]
    if "id" not in message:
        continue
    if method == "initialize":
        result = {
            "userAgent": "codex/0.160.0",
            "serverInfo": {"name": "codex", "version": "0.160.0"},
        }
    elif method == "thread/start":
        result = {
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "cwd": str(Path.cwd()),
            "model": "fixture",
            "modelProvider": "openai",
            "sandbox": {"type": "readOnly"},
            "thread": {
                "id": "thread-fixture",
                "sessionId": "session-fixture",
                "cliVersion": "fixture",
                "createdAt": 1,
                "updatedAt": 1,
                "cwd": str(Path.cwd()),
                "ephemeral": False,
                "modelProvider": "openai",
                "preview": "",
                "source": "exec",
                "status": {"type": "idle"},
                "turns": [],
            },
        }
    elif method == "turn/start":
        result = {
            "turn": {"id": "turn-fixture", "items": [], "itemsView": "full", "status": "inProgress"}
        }
    else:
        raise AssertionError(method)
    send({"id": message["id"], "result": result})
    if method != "turn/start":
        continue
    send(
        {
            "method": "fixture/progress",
            "params": {
                "threadId": "thread-fixture",
                "turnId": "turn-fixture",
                "text": "future event",
            },
        }
    )
    send(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-fixture",
                "turnId": "turn-fixture",
                "item": {"type": "plan", "id": "plan", "text": "Do the assignment"},
            },
        }
    )
    if mode == "block":
        capture.with_suffix(".pid").write_text(str(os.getpid()))
        time.sleep(60)
    if mode == "disconnect":
        sys.exit(0)
    item = {"type": "agentMessage", "id": "comment", "phase": "commentary", "text": "Working"}
    send(
        {
            "method": "item/completed",
            "params": {"threadId": "thread-fixture", "turnId": "turn-fixture", "item": item},
        }
    )
    if mode != "missing":
        value = {"items": []}
        item = {
            "type": "agentMessage",
            "id": "answer",
            "phase": "final_answer",
            "text": "invalid JSON"
            if mode == "malformed"
            else json.dumps([] if mode == "array" else value),
        }
        send(
            {
                "method": "item/completed",
                "params": {"threadId": "thread-fixture", "turnId": "turn-fixture", "item": item},
            }
        )
    send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-fixture",
                "turn": {
                    "id": "turn-fixture",
                    "items": [],
                    "itemsView": "full",
                    "status": mode if mode in ("failed", "interrupted") else "completed",
                },
            },
        }
    )
