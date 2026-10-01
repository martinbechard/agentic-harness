"""Installed terminal entry point. Inspection and validation make no model calls."""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .contracts import load_config, load_control_config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentic-harness")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("status")
    commands.add_parser("app")
    commands.add_parser("reconcile")
    commands.add_parser("refresh-provider")
    batch = commands.add_parser("run")
    modes = batch.add_mutually_exclusive_group(required=True)
    modes.add_argument("--until-terminal", action="store_true")
    modes.add_argument("--watch", action="store_true")
    review = commands.add_parser("review-hold")
    review.add_argument("item_id")
    review.add_argument("--ceiling", type=float)
    review.add_argument("--reference")
    answer = commands.add_parser("resume-answer")
    answer.add_argument("item_id")
    recover = commands.add_parser("recover-provider")
    recover.add_argument("operation_id")
    dashboard = commands.add_parser("dashboard")
    dashboard.add_argument("--port", type=int, default=8767)
    run = commands.add_parser("run-item")
    run.add_argument("item_id")
    args = parser.parse_args(argv)
    try:
        control_only = args.command in {
            "status",
            "dashboard",
            "app",
            "reconcile",
            "recover-provider",
        }
        config = (load_control_config if control_only else load_config)(args.config)
        if args.command == "validate":
            result = {
                "valid": True,
                "config_digest": config.file_digest,
                "bindings": {r: asdict(config.binding(r)) for r in config.data["agents"]},
            }
        elif args.command in ("status", "dashboard"):
            from .application import Application
            from .projections import snapshot

            app = Application(args.config, control_only=True)
            if args.command == "dashboard":
                from .dashboard import create_dashboard_server

                with create_dashboard_server(lambda: snapshot(app), port=args.port) as server:
                    print(f"Read-only dashboard: http://127.0.0.1:{server.server_port}", flush=True)
                    server.serve_forever()
                return 0
            result = snapshot(app)
        elif args.command == "run-item":
            from .application import Application

            result = asyncio.run(Application(args.config).run_item(args.item_id))
        else:
            from .application import Application

            app = Application(args.config, control_only=control_only)
            if args.command == "reconcile":
                result = app.reconcile()
            elif args.command == "refresh-provider":
                from .provider import AgentProvider, TransitionBlocked

                if not isinstance(app.provider, AgentProvider):
                    raise TransitionBlocked("refresh-provider requires provider_interaction: agent")
                asyncio.run(app.refresh_provider())
                observed = app.provider.observation()
                result = {
                    "source_revision": observed["source_revision"],
                    "invocation_id": observed["invocation_id"],
                    "item_count": len(observed["items"]),
                    "policy": observed["policy"],
                }
            elif args.command == "resume-answer":
                result = asyncio.run(app.resume_answer(args.item_id))
            elif args.command == "recover-provider":
                result = app.recover_provider(args.operation_id)
            elif args.command == "review-hold":
                result = asyncio.run(
                    app.review_hold(
                        args.item_id, requested_ceiling=args.ceiling, reference=args.reference
                    )
                )
            elif args.command == "app":
                from .terminal import terminal

                return asyncio.run(terminal(app))
            else:
                from .coordination import RunController

                result = asyncio.run(
                    RunController(app, lambda value: print(json.dumps(value), flush=True)).run(
                        "until-terminal" if args.until_terminal else "watch"
                    )
                )
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
