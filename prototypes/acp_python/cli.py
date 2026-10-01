"""Explicit single-execution pilot, never connected to production dispatch."""

import argparse
import asyncio
import json
from importlib.metadata import version
from pathlib import Path

from configuration import reload_config
from native import NativeClient
from workflow import run


async def main(args):
    if args.command == "smoke":
        client = NativeClient(reload_config(args.config), "producer")
        async with client:
            identity = await client.new_session()
        return {
            "kind": "no-model ACP handshake",
            "auth": client.auth,
            "session_id": identity,
            "cleanup": client.cleanup,
            "versions": {
                name: version(name)
                for name in ("agent-client-protocol", "langgraph", "langgraph-checkpoint-sqlite")
            },
            "model_generation_tested": False,
        }
    kwargs = {}
    if args.command == "start":
        kwargs["prompt"] = args.prompt.read_text()
    elif args.command == "answer":
        kwargs["answer"] = {"question_id": args.question_id, "text": args.text}
    return await run(args.root, args.execution, args.config, **kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--execution")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--prompt", type=Path, required=True)
    answer = commands.add_parser("answer")
    answer.add_argument("--question-id", required=True)
    answer.add_argument("--text", required=True)
    commands.add_parser("resume")
    commands.add_parser("smoke")
    args = parser.parse_args()
    if args.command != "smoke" and (not args.root or not args.execution):
        parser.error("--root and --execution are required for a workflow")
    print(json.dumps(asyncio.run(main(args)), indent=2))
