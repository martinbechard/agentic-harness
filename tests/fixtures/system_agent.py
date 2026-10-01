"""Deterministic external CLI transport, exercising actual harness subprocess/OTLP boundaries."""

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

if "--version" in sys.argv:
    print("codex-cli 0.159.2")
    raise SystemExit
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit
prompt = sys.stdin.read()
with (Path(os.environ["HARNESS_SYSTEM_REPO"]).parent / "agent-calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv, "prompt": prompt}) + "\n")
args = sys.argv
session = args[args.index("resume") + 1] if "resume" in args else str(uuid4())
home = Path(os.environ["CODEX_HOME"])
now = lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
path = (
    home / "sessions" / datetime.now(UTC).strftime("%Y/%m/%d") / ("rollout-" + session + ".jsonl")
)
path.parent.mkdir(parents=True, exist_ok=True)


def append(kind, payload):
    with path.open("a") as stream:
        stream.write(json.dumps({"timestamp": now(), "type": kind, "payload": payload}) + "\n")


if not path.exists():
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    append(
        "session_meta",
        {
            "id": session,
            "cwd": os.getcwd(),
            "source": "exec",
            "git": {"commit_hash": head},
            "timestamp": now(),
        },
    )
turn_id = str(uuid4())
append("event_msg", {"type": "task_started", "turn_id": turn_id})
append("turn_context", {"turn_id": turn_id})
append("event_msg", {"type": "user_message", "message": prompt})
append(
    "response_item",
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]},
)
print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
spec = importlib.util.spec_from_file_location("scenario", os.environ["HARNESS_SYSTEM_DISPATCHER"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
before_children = set(home.glob("sessions/*/*/*/*.jsonl"))
result = module.dispatch(prompt, Path.cwd(), home, args, session)
usage = module.transport_usage(prompt) if hasattr(module, "transport_usage") else {}
output_tokens = usage.get("output_tokens", 1)
text = json.dumps(result)
append("event_msg", {"type": "agent_message", "message": text})
prior = [json.loads(line) for line in path.read_text().splitlines()]
counters = [
    r["payload"]["info"]["total_token_usage"]["output_tokens"]
    for r in prior
    if r["payload"].get("type") == "token_count"
]
cumulative = (counters[-1] if counters else 0) + output_tokens
if usage.get("terminal_usage", True):
    append(
        "event_msg",
        {"type": "token_count", "info": {"total_token_usage": {"output_tokens": cumulative}}},
    )
append("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": text})
exporter = next(a for a in args if a.startswith("otel.trace_exporter="))
endpoint = json.loads(re.search(r'endpoint=("[^"]+")', exporter).group(1))
token = json.loads(re.search(r'"x-harness-invocation"=("[^"]+")', exporter).group(1))
child_tokens = 0
for child in set(home.glob("sessions/*/*/*/*.jsonl")) - before_children:
    with child.open() as stream:
        try:
            metadata = json.loads(stream.readline()).get("payload", {})
        except ValueError:
            continue  # Another invocation may still be creating its own native log.
    if metadata.get("parent_thread_id") != session:
        continue
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    counts = [
        r["payload"]["info"]["total_token_usage"]["output_tokens"]
        for r in rows
        if r["payload"].get("type") == "token_count"
    ]
    child_tokens += counts[-1] if counts else 0
span = {
    "traceId": uuid4().hex,
    "spanId": uuid4().hex[:16],
    "name": "fixture.turn",
    "attributes": [
        {
            "key": "gen_ai.usage.output_tokens",
            "value": {"intValue": str(output_tokens + child_tokens)},
        }
    ],
}
if not usage.get("export_usage", True):
    span["attributes"] = []
payload = {
    "resourceSpans": [
        {
            "resource": {"attributes": []},
            "scopeSpans": [{"scope": {"name": "fixture"}, "spans": [span]}],
        }
    ]
}
with urlopen(
    Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-harness-invocation": token},
    ),
    timeout=5,
) as response:
    assert response.status == 200
print(
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
    flush=True,
)
print(
    json.dumps(
        {
            "type": "turn.completed",
            **(
                {"usage": {"output_tokens": cumulative}}
                if usage.get("terminal_usage", True)
                else {}
            ),
        }
    ),
    flush=True,
)
if hasattr(module, "after_terminal_output"):
    module.after_terminal_output(prompt)
