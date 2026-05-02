"""Command-line surface for hsp.

The MCP server stays the default `hsp` behavior for Claude Code, while
agent-bus hooks live under the same binary as `hsp log ...`. Keeping one
entrypoint avoids install-path drift between MCP, broker, and harness hooks.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from hsp import server


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if ns.command == "log":
        return _run_log(ns, parser)
    parser.error(f"unknown command: {ns.command!r}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hsp")
    subcommands = parser.add_subparsers(dest="command", required=True)

    log = subcommands.add_parser(
        "log",
        help="record or inspect warn-only agent-bus coordination events",
    )
    log.add_argument(
        "action",
        choices=(*server._BUS_ACTIONS, "hook"),
        help="bus action; hook is a CLI alias for event with --kind",
    )
    log.add_argument("--message", default="")
    log.add_argument("--files", default="")
    log.add_argument("--symbols", default="")
    log.add_argument("--aliases", default="")
    log.add_argument("--id", default="")
    log.add_argument("--timeout", default="3m")
    log.add_argument("--kind", default="")
    log.add_argument("--status", default="")
    log.add_argument("--targets", default="")
    log.add_argument("--commit", default="")
    return parser


def _run_log(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    action = str(ns.action)
    kind = str(ns.kind)
    if action == "hook":
        if not kind.strip():
            parser.error("hsp log hook requires --kind")
        action = "event"

    result = asyncio.run(
        server.lsp_log(
            action=action,
            message=str(ns.message),
            files=str(ns.files),
            symbols=str(ns.symbols),
            aliases=str(ns.aliases),
            id=str(ns.id),
            timeout=str(ns.timeout),
            kind=kind,
            status=str(ns.status),
            targets=str(ns.targets),
            commit=str(ns.commit),
        )
    )
    print(result)
    return 0


__all__ = ["build_parser", "main"]
