"""Command-line surface for hsp.

The MCP server stays the default `hsp` behavior for Claude Code, while
agent-bus hooks live under the same binary as `hsp log ...`. Keeping one
entrypoint avoids install-path drift between MCP, broker, and harness hooks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import Any, cast

from hsp import server

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if ns.command == "log":
        return _run_log(ns, parser)
    if ns.command == "hook":
        return _run_hook(ns, parser)
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

    hook = subcommands.add_parser(
        "hook",
        help="record a bundled plugin hook event when HSP_HOOKS is enabled",
    )
    hook.add_argument("--kind", required=True)
    hook.add_argument("--message", default="")
    hook.add_argument("--files", default="")
    hook.add_argument("--symbols", default="")
    hook.add_argument("--aliases", default="")
    hook.add_argument("--status", default="")
    hook.add_argument("--targets", default="")
    hook.add_argument("--commit", default="")
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


def _run_hook(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not _hooks_enabled():
        _drain_stdin()
        return 0

    kind = str(ns.kind).strip()
    if not kind:
        parser.error("hsp hook requires --kind")

    payload = _read_hook_payload()
    message = str(ns.message) or _hook_message(payload)
    if kind in {"prompt", "user.prompt"} and message.strip() == ".end":
        kind = "session.stop"
        message = ".end"
    files = _join_scope(str(ns.files), _hook_files(payload))
    symbols = _join_scope(str(ns.symbols), _hook_symbols(payload))
    aliases = _join_scope(str(ns.aliases), [])
    status = str(ns.status) or _hook_status(payload)
    targets = str(ns.targets)
    commit = str(ns.commit)

    asyncio.run(
        server.lsp_log(
            action="event",
            message=message,
            files=files,
            symbols=symbols,
            aliases=aliases,
            kind=kind,
            status=status,
            targets=targets,
            commit=commit,
        )
    )
    return 0


def _hooks_enabled() -> bool:
    raw = os.environ.get("HSP_HOOKS", "").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return False


def _drain_stdin() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass


def _read_hook_payload() -> dict[str, object]:
    try:
        text = sys.stdin.read()
    except Exception:
        return {}
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text.strip()}
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}


def _hook_message(payload: dict[str, object]) -> str:
    for key in ("prompt", "message", "transcript_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool_name = _string_value(payload, "tool_name", "toolName", "name")
    hook_name = _string_value(payload, "hook_event_name", "hookEventName")
    if tool_name and hook_name:
        return f"{hook_name} {tool_name}"
    return tool_name or hook_name


def _hook_files(payload: dict[str, object]) -> list[str]:
    files: list[str] = []
    _collect_path_like(payload, files)
    for key in ("tool_input", "toolInput", "input"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            _collect_path_like(cast(dict[str, object], nested), files)
    return _dedupe(files)


def _hook_symbols(payload: dict[str, object]) -> list[str]:
    symbols: list[str] = []
    for key in ("symbol", "symbols"):
        value = payload.get(key)
        symbols.extend(_scope_items(value))
    return _dedupe(symbols)


def _hook_status(payload: dict[str, object]) -> str:
    for key in ("status", "permissionDecision"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    response = payload.get("tool_response") or payload.get("toolResponse")
    if isinstance(response, dict):
        data = cast(dict[str, object], response)
        if data.get("error"):
            return "error"
        if data.get("interrupted"):
            return "interrupted"
    if payload.get("success") is True:
        return "success"
    if payload.get("success") is False:
        return "error"
    return ""


def _collect_path_like(payload: dict[str, object], out: list[str]) -> None:
    for key in (
        "file_path",
        "filePath",
        "path",
        "notebook_path",
        "notebookPath",
        "files",
        "paths",
    ):
        out.extend(_scope_items(payload.get(key)))
    command = payload.get("command")
    if isinstance(command, str):
        out.extend(_paths_from_command(command))


def _paths_from_command(command: str) -> list[str]:
    return [
        token.strip("'\"")
        for token in command.replace("\n", " ").split()
        if "/" in token and not token.startswith("-")
    ]


def _scope_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _join_scope(explicit: str, detected: list[str]) -> str:
    return ",".join(_dedupe([*_scope_items(explicit), *detected]))


def _string_value(payload: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


__all__ = ["build_parser", "main"]
