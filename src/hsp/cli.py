"""Command-line surface for hsp.

Bare `hsp` is the workgroup querying surface for humans and agents. The MCP
server is explicit as `hsp mcp`, while hooks and shell mirrors stay under the
same binary as `hsp hook ...` and `hsp log ...`. Keeping one entrypoint avoids
install-path drift between MCP, broker, and harness hooks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from hsp.bus_registry import BrokerMode, log_path_for, workspace_id_for
from hsp.workgroup import ScopeContext, project_root_for, scope_context_for

_server_module: ModuleType | None = None

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}
READ_CONTEXT_TOOLS = {"Read", "NotebookRead"}
EDIT_CONTEXT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
BROKER_DISABLED = {"0", "false", "no", "off", "disable", "disabled", "local"}
BROKER_REQUIRED = {"1", "true", "yes", "on", "require", "required"}
BROKER_SOCKET_NAME = "hsp-broker.sock"
BROKER_CONNECT_TIMEOUT_SECONDS = 0.25
BUS_ACTIONS: tuple[str, ...] = (
    "event",
    "note",
    "ask",
    "reply",
    "chat",
    "ticket",
    "journal",
    "question",
    "edit_gate",
    "recent",
    "settle",
    "precommit",
    "postcommit",
    "weather",
    "presence",
    "workgroup",
    "status",
)
EDIT_DENY_REASON = (
    "Edit denied by HSP workgroup policy: no active ticket is held for this "
    "workspace. Start work with hsp.ticket(\"...\") or `hsp log ticket --message "
    "\"...\"`, then retry the edit."
)
BUILD_FIRST_TOKENS = {
    "bun",
    "cargo",
    "cmake",
    "composer",
    "deno",
    "dotnet",
    "go",
    "gradle",
    "just",
    "make",
    "mvn",
    "ninja",
    "nox",
    "npm",
    "npx",
    "pnpm",
    "pytest",
    "rk",
    "spaceship",
    "tox",
    "uv",
    "yarn",
}
BUILD_SUBCOMMANDS = {
    "bench",
    "build",
    "check",
    "clippy",
    "compile",
    "install",
    "lint",
    "package",
    "publish",
    "run",
    "test",
    "verify",
}
BUILD_BATCH_CAPTURE_LIMIT = 12000
BUILD_BATCH_DEFAULT_TTL_SECONDS = 30.0
BUILD_BATCH_DEFAULT_WAIT_SECONDS = 1800.0
DIRECT_CHECKER_TOKENS = {
    "biome",
    "black",
    "eslint",
    "flake8",
    "mypy",
    "phpstan",
    "phpunit",
    "prettier",
    "pylint",
    "pyright",
    "pytest",
    "ruff",
    "isort",
    "shellcheck",
    "stylelint",
    "ty",
}
PYTHON_MODULE_CHECKERS = {"mypy", "pytest", "ruff", "unittest"}
RUNNER_TOKENS = {"npx", "poetry", "pipenv", "uv"}
PATHY_OPTIONS_WITH_VALUE = {
    "--config",
    "--config-file",
    "--directory",
    "--extra",
    "--group",
    "--manifest-path",
    "--only-group",
    "--package",
    "--python",
    "--project",
    "--target",
    "--target-dir",
    "--with",
    "--with-editable",
    "--with-requirements",
    "--without",
}


@dataclass(frozen=True)
class CommandGateSpec:
    argv: tuple[str, ...]
    full_workspace: bool
    files: tuple[str, ...] = ()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)
    if bool(getattr(ns, "global_status", False)) or ns.command == "global":
        return _run_global(ns)
    if ns.command in {None, "workgroup"}:
        return _run_workgroup(ns)
    if ns.command == "mcp":
        return _run_mcp()
    if ns.command == "log":
        return _run_log(ns, parser)
    if ns.command == "hook":
        return _run_hook(ns, parser)
    if ns.command == "run":
        return _run_command(ns, parser)
    if ns.command == "watch":
        return _run_watch(ns)
    parser.error(f"unknown command: {ns.command!r}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hsp")
    parser.set_defaults(
        command=None,
        locations=[],
        limit=8,
        broker=False,
        weather=False,
        global_status=False,
        start_broker=False,
        lsp=False,
    )
    _add_workgroup_flags(parser)
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser(
        "mcp",
        help="run the HSP MCP server over stdio",
    )

    log = subcommands.add_parser(
        "log",
        help="record or inspect warn-only agent-bus coordination events",
    )
    log.add_argument(
        "action",
        choices=(*BUS_ACTIONS, "hook"),
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
        help="record a bundled plugin hook event unless HSP_HOOKS disables it",
    )
    hook.add_argument(
        "hook_mode",
        nargs="?",
        default="",
        help="use `stdin <kind>` for plugin hook handlers",
    )
    hook.add_argument("hook_kind", nargs="?", default="")
    hook.add_argument("--kind", default="")
    hook.add_argument("--message", default="")
    hook.add_argument("--files", default="")
    hook.add_argument("--symbols", default="")
    hook.add_argument("--aliases", default="")
    hook.add_argument("--status", default="")
    hook.add_argument("--targets", default="")
    hook.add_argument("--commit", default="")

    run = subcommands.add_parser(
        "run",
        help="wait for the workgroup build gate, run a command, then record the result",
    )
    run.add_argument("--timeout", default="2m")
    run.add_argument("--kind", default="test.ran")
    run.add_argument("--files", default="")
    run.add_argument("--symbols", default="")
    run.add_argument("--message", default="")
    run.add_argument("--no-log", action="store_true")
    run.add_argument("argv", nargs=argparse.REMAINDER)

    watch = subcommands.add_parser(
        "watch",
        help="watch hook, tool, and bus traffic received by the HSP broker",
    )
    watch.add_argument(
        "locations",
        nargs="*",
        help="workgroup locations to watch; defaults to the current directory",
    )
    watch.add_argument(
        "--global",
        dest="global_events",
        action="store_true",
        help="watch all broker events",
    )
    watch.add_argument("--limit", type=int, default=25)
    watch.add_argument("--interval", type=float, default=0.5)
    watch.add_argument("--once", action="store_true", help="print one snapshot and exit")
    watch.add_argument("--exact", action="store_true", help="watch only the active workgroup root")
    watch.add_argument("--start-broker", dest="start_broker", action="store_true", help=argparse.SUPPRESS)
    watch.add_argument(
        "--no-start-broker",
        dest="start_broker",
        action="store_false",
        help="fail instead of starting the broker when the socket is missing",
    )
    watch.set_defaults(start_broker=True)

    workgroup = subcommands.add_parser(
        "workgroup",
        help="debug workgroup root, broker, and bus status from one or more locations",
    )
    workgroup.add_argument(
        "locations",
        nargs="*",
        help="directories or files to evaluate; defaults to the current directory",
    )
    _add_workgroup_flags(workgroup)
    global_status = subcommands.add_parser(
        "global",
        help="show broker-global sessions, LSP clients, and source routes",
    )
    _add_global_flags(global_status)
    return parser


def _add_workgroup_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--global", dest="global_status", action="store_true", help="show broker-global status")
    parser.add_argument("--broker", action="store_true", help="query broker bus status")
    parser.add_argument(
        "--weather",
        action="store_true",
        help="query broker bus status and recent journal weather",
    )
    parser.add_argument("--start-broker", action="store_true")
    parser.add_argument("--lsp", action="store_true", help="include lsp_session status for each location")


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-broker", action="store_true")


def _server() -> ModuleType:
    global _server_module
    if _server_module is None:
        from hsp import server

        _server_module = server
    return _server_module


def _broker_mode() -> str:
    raw = os.environ.get("HSP_BROKER", "auto").strip().lower()
    if raw in BROKER_DISABLED:
        return "off"
    if raw in BROKER_REQUIRED:
        return "on"
    return "auto"


def _broker_socket_path() -> Path:
    override = os.environ.get("HSP_BROKER_SOCKET")
    if override:
        return Path(override)
    runtime = _user_runtime_dir()
    if runtime:
        return runtime / BROKER_SOCKET_NAME
    user = os.environ.get("USER") or str(os.getuid())
    return Path(f"/tmp/hsp-broker-{user}") / BROKER_SOCKET_NAME


def _legacy_tmp_broker_socket_path() -> Path:
    user = os.environ.get("USER") or str(os.getuid())
    return Path(f"/tmp/hsp-broker-{user}") / BROKER_SOCKET_NAME


def _user_runtime_dir() -> Path | None:
    raw = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if raw:
        return Path(raw)
    candidate = Path(f"/run/user/{os.getuid()}")
    return candidate if candidate.exists() else None


def _broker_log_path() -> Path:
    override = os.environ.get("HSP_BROKER_LOG")
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "hsp" / "broker.log"


def _run_mcp() -> int:
    _server().run()
    return 0


def _run_log(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    action = str(ns.action)
    kind = str(ns.kind)
    if action == "hook":
        if not kind.strip():
            parser.error("hsp log hook requires --kind")
        action = "event"

    result = asyncio.run(
        _server().lsp_log(
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

    kind = _hook_kind_from_args(ns, parser)
    if not kind:
        parser.error("hsp hook requires --kind or `stdin <kind>`")

    payload = _read_hook_payload()
    message = str(ns.message) or _hook_message(payload)
    if kind in {"prompt", "user.prompt"} and message.strip() == ".end":
        kind = "session.stop"
        message = ".end"
    command = _hook_command(payload)
    files = _join_scope(str(ns.files), _hook_files(payload))
    symbols = _join_scope(str(ns.symbols), _hook_symbols(payload))
    if _is_edit_before_hook(kind) and _require_ticket_for_edits():
        gate = asyncio.run(
            _server().lsp_log(
                action="edit_gate",
                message=message,
                files=files,
                symbols=symbols,
                status=os.environ.get("HSP_EDIT_GATE_SCOPE", "workgroup"),
            )
        )
        if "edit gate: allowed" not in gate:
            _write_hook_denial(_edit_denial_reason(gate))
            return 0
    if _is_build_before_hook(kind, payload, command):
        gate_spec = _command_gate_spec(command)
        assert gate_spec is not None
        gate = asyncio.run(
            _server().implicit_build_gate(
                command,
                timeout=os.environ.get("HSP_BUILD_GATE_TIMEOUT", "2m"),
                files=",".join(gate_spec.files),
                full_workspace=gate_spec.full_workspace,
            )
        )
        if "build gate: unlocked" not in gate:
            print(gate, file=sys.stderr)
            return 124
        if _build_gate_reason(gate) == "all_waiting" and _authoritative_build_enabled():
            batch = _run_authoritative_build_batch(
                command=command,
                gate=gate,
                files=",".join(gate_spec.files),
                full_workspace=gate_spec.full_workspace,
            )
            _write_hook_denial(_build_batch_denial_reason(batch))
            return 0
        return 0
    context_notice = _hook_context_notice(kind, payload, files=files, symbols=symbols)
    if context_notice:
        print(context_notice)
    aliases = _join_scope(str(ns.aliases), [])
    status = str(ns.status) or _hook_status(payload)
    targets = str(ns.targets)
    commit = str(ns.commit)
    if _is_build_after_hook(kind, payload, command):
        kind = "test.ran"
        message = command
        targets = targets or command
        status = _build_status(status)

    asyncio.run(
        _server().lsp_log(
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


def _hook_context_notice(
    kind: str,
    payload: dict[str, object],
    *,
    files: str,
    symbols: str,
) -> str:
    if not _hook_context_enabled() or not _is_context_hook(kind, payload) or not (files or symbols):
        return ""
    try:
        recent = asyncio.run(
            _server().lsp_log(
                action="recent",
                files=files,
                symbols=symbols,
            )
        ).strip()
    except Exception as e:
        return f"hsp context unavailable: {type(e).__name__}: {e}"
    if not recent or recent == "recent: (none)":
        return ""
    target = ", ".join(_dedupe([*_scope_items(files), *_scope_items(symbols)]))
    return f"hsp context for {target}:\n{recent}"


def _hook_context_enabled() -> bool:
    return os.environ.get("HSP_HOOK_CONTEXT", "1").strip().lower() not in FALSE_VALUES


def _is_context_hook(kind: str, payload: dict[str, object]) -> bool:
    tool = _hook_tool_name(payload)
    if kind in {"read.before", "read.after"}:
        return True
    if kind in {"edit.before", "edit.after"}:
        return not tool or tool in EDIT_CONTEXT_TOOLS
    if kind in {"tool.before", "tool.after"}:
        return tool in READ_CONTEXT_TOOLS
    return False


def _hook_kind_from_args(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    explicit = str(ns.kind).strip()
    mode = str(getattr(ns, "hook_mode", "")).strip()
    positional = str(getattr(ns, "hook_kind", "")).strip()
    if explicit and (mode or positional):
        parser.error("hsp hook accepts either --kind or positional kind, not both")
    if explicit:
        return explicit
    if mode == "stdin":
        if not positional:
            parser.error("hsp hook stdin requires a kind")
        return positional
    if mode and not positional:
        return mode
    if mode or positional:
        parser.error("hsp hook positional form is `stdin <kind>`")
    return ""


def _run_command(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    argv = _command_argv(cast(list[str], ns.argv))
    if not argv:
        parser.error("hsp run requires a command after --")

    message = str(ns.message).strip() or " ".join(argv)
    gate_spec = _gate_spec_for_argv(argv)
    gate = asyncio.run(
        _server().implicit_build_gate(
            message,
            timeout=str(ns.timeout),
            files=",".join(gate_spec.files) if gate_spec else str(ns.files),
            full_workspace=gate_spec.full_workspace if gate_spec else not str(ns.files).strip(),
        )
    )
    if "build gate: unlocked" not in gate:
        print(gate, file=sys.stderr)
        return 124

    completed = subprocess.run(argv, check=False)
    status = "passed" if completed.returncode == 0 else "failed"
    if not bool(ns.no_log):
        asyncio.run(
            _server().lsp_log(
                action="event",
                message=message,
                files=str(ns.files),
                symbols=str(ns.symbols),
                kind=str(ns.kind),
                status=status,
                targets=message,
            )
        )
    return int(completed.returncode)


def _run_workgroup(ns: argparse.Namespace) -> int:
    locations = cast(list[str], ns.locations) or ["."]
    blocks = [
        _workgroup_block(
            location=location,
            limit=max(0, int(ns.limit)),
            include_broker=True,
            include_weather=True,
            start_broker=bool(ns.start_broker),
            include_lsp=bool(ns.lsp),
        )
        for location in locations
    ]
    print("\n\n".join(blocks))
    return 0


def _run_global(ns: argparse.Namespace) -> int:
    print(_global_block(start_broker=bool(ns.start_broker)))
    return 0


def _run_watch(ns: argparse.Namespace) -> int:
    if _broker_mode() == "off":
        print("watch: broker disabled")
        return 1
    limit = max(1, int(ns.limit))
    interval = max(0.1, float(ns.interval))
    locations = cast(list[str], ns.locations) or ["."]
    watch_scope = _watch_scope_for_locations(locations, exact=bool(ns.exact))
    roots = [] if bool(ns.global_events) else watch_scope.roots
    exact = bool(ns.exact) or watch_scope.exact
    try:
        with _open_cli_broker(start_broker=bool(ns.start_broker)) as client:
            started = bool(getattr(client, "hsp_started", False))
            scope = "global" if bool(ns.global_events) else ",".join(roots)
            scope_mode = "" if bool(ns.global_events) else f" {watch_scope.mode}"
            print(
                f"watch: broker={_broker_socket_path()}"
                f"{' started' if started else ''} scope={scope}{scope_mode} interval={interval:g}s"
            )
            after_id = 0
            while True:
                rows = _watch_events(
                    client,
                    roots=roots,
                    global_events=bool(ns.global_events),
                    exact=exact,
                    limit=limit,
                    after_id=after_id,
                )
                if rows:
                    after_id = max(after_id, *(_event_seq(event) for event in rows))
                    for event in rows:
                        print(
                            _watch_event_label(
                                event,
                                include_workspace=bool(ns.global_events) or not exact,
                            ),
                            flush=True,
                        )
                elif bool(ns.once):
                    print("watch: no events")
                if bool(ns.once):
                    return 0
                time.sleep(interval)
    except KeyboardInterrupt:
        return 130
    except _CliBrokerError as e:
        print(f"watch: broker unreachable ({e.code}: {e})")
        return 1
    except OSError as e:
        print(f"watch: broker unreachable ({type(e).__name__}: {e})")
        return 1


@dataclass(frozen=True)
class WatchScope:
    roots: list[str]
    exact: bool
    mode: str


def _watch_scope_for_locations(locations: list[str], *, exact: bool) -> WatchScope:
    scopes = [scope_context_for(location) for location in locations]
    if exact:
        return WatchScope(
            roots=list(dict.fromkeys(scope.active_workgroup_root for scope in scopes)),
            exact=True,
            mode="exact",
        )
    roots: list[str] = []
    modes: list[str] = []
    for scope in scopes:
        roots.extend(scope.observation_roots)
        modes.append(scope.observation_mode)
    deduped = list(dict.fromkeys(roots))
    is_exact = all(mode == "exact" for mode in modes)
    mode = "exact" if is_exact else ("subtree" if all(mode == "subtree" for mode in modes) else "network")
    return WatchScope(roots=deduped, exact=is_exact, mode=mode)


def _watch_events(
    client: Any,
    *,
    roots: list[str],
    global_events: bool,
    exact: bool,
    limit: int,
    after_id: int,
) -> list[dict[str, object]]:
    if global_events:
        result = client.request("bus.recent_all", {"after_id": after_id, "limit": limit})
        return _watch_result_events(result)
    if not exact:
        result = client.request(
            "bus.recent_tree",
            {"workspace_roots": list(dict.fromkeys(roots)), "after_id": after_id, "limit": limit},
        )
        return _watch_result_events(result)
    events: list[dict[str, object]] = []
    for root in dict.fromkeys(roots):
        result = client.request(
            "bus.recent",
            {"workspace_root": root, "after_id": after_id, "limit": limit},
        )
        events.extend(_watch_result_events(result))
    return sorted(events, key=_event_seq)[-limit:]


def _watch_result_events(result: object) -> list[dict[str, object]]:
    if not isinstance(result, dict):
        return []
    return [
        cast(dict[str, object], event)
        for event in _wire_list(cast(dict[str, object], result), "events")
        if isinstance(event, dict)
    ]


def _watch_event_label(event: dict[str, object], *, include_workspace: bool) -> str:
    label = _event_label(event)
    if not include_workspace:
        return label
    workspace_root = str(event.get("workspace_root") or "")
    if not workspace_root:
        return label
    return f"{workspace_root} {label}"


def _event_seq(event: dict[str, object]) -> int:
    value = event.get("seq")
    if isinstance(value, int):
        return value
    event_id = str(event.get("event_id") or "")
    if event_id.startswith("E"):
        event_id = event_id[1:]
    try:
        return int(event_id)
    except ValueError:
        return 0


def _workgroup_block(
    *,
    location: str,
    limit: int,
    include_broker: bool,
    include_weather: bool,
    start_broker: bool,
    include_lsp: bool,
) -> str:
    scope = scope_context_for(location)
    root = scope.active_workgroup_root
    wsid = workspace_id_for(root)
    lines = [
        f"workgroup: {root}",
        f"location: {Path(location).expanduser()}",
        f"workgroup_source: {scope.workgroup_source}",
        f"workspace_id: {wsid}",
        f"project: {scope.project_root}",
        "gate policy: build=project checker=file/project journal=workgroup",
        f"env HSP_WORKGROUP_ROOT: {os.environ.get('HSP_WORKGROUP_ROOT', '(unset)')}",
        f"env LSP_ROOT: {os.environ.get('LSP_ROOT', '(unset)')}",
        f"broker mode: {_broker_mode()}",
        f"broker socket: {_broker_socket_path()}",
        f"broker log: {_broker_log_path()}",
    ]
    lines.extend(_workgroup_stack_lines(scope))
    lines.extend(_workgroup_log_lines(root))
    lines.extend(
        _workgroup_broker_lines(
            root,
            limit=limit,
            include_broker=include_broker,
            include_weather=include_weather,
            start_broker=start_broker,
        )
    )
    if include_lsp:
        lines.append("lsp:")
        lines.extend(f"  {line}" for line in _workgroup_lsp_status(root).splitlines())
    return "\n".join(lines)


def _workgroup_stack_lines(scope: ScopeContext) -> list[str]:
    if not scope.workgroups:
        return [f"workgroup_stack: (none; {scope.workgroup_source})"]
    lines = ["workgroup_stack:"]
    for index, item in enumerate(scope.workgroups):
        role = "active" if index == len(scope.workgroups) - 1 else "parent"
        lines.append(f"  {role} {item.level} {item.name}: {item.root}")
    return lines


def _workgroup_root_for_location(location: str) -> str:
    return scope_context_for(location).active_workgroup_root


def _workgroup_log_lines(root: str) -> list[str]:
    append_log = Path(root) / "tmp" / "hsp-bus.jsonl"
    direct_log = log_path_for(root, BrokerMode.DIRECT)
    broker_log = log_path_for(root, BrokerMode.BROKER)
    return [
        _jsonl_status_line("append log", append_log),
        _jsonl_status_line("direct registry log", direct_log),
        _jsonl_status_line("broker registry log", broker_log),
    ]


def _jsonl_status_line(label: str, path: Path) -> str:
    count, last = _jsonl_count_and_last(path)
    if count == 0:
        return f"{label}: {path} (missing or empty)"
    return f"{label}: {path} ({count} event(s), last={last or '?'})"


def _jsonl_count_and_last(path: Path) -> tuple[int, str]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return 0, ""
    last_id = ""
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            value = payload.get("event_id") or payload.get("id") or payload.get("seq")
            last_id = str(value) if value is not None else ""
            break
    return len(lines), last_id


def _global_block(*, start_broker: bool) -> str:
    lines = [
        "global:",
        f"broker mode: {_broker_mode()}",
        f"broker socket: {_broker_socket_path()}",
        f"broker log: {_broker_log_path()}",
    ]
    if _broker_mode() == "off":
        lines.append("broker: disabled")
        return "\n".join(lines)
    try:
        with _open_cli_broker(start_broker=start_broker) as client:
            started = bool(getattr(client, "hsp_started", False))
            status = client.request("lsp.status", {})
    except _CliBrokerError as e:
        lines.append(f"broker: unreachable ({e.code}: {e})")
        return "\n".join(lines)
    except OSError as e:
        lines.append(f"broker: unreachable ({type(e).__name__}: {e})")
        return "\n".join(lines)
    if not isinstance(status, dict):
        lines.append(f"broker: invalid lsp.status ({type(status).__name__})")
        return "\n".join(lines)
    lines.extend(_render_global_status(cast(dict[str, object], status), started=started))
    lines.extend(_split_broker_lines())
    return "\n".join(lines)


def _split_broker_lines() -> list[str]:
    current = _broker_socket_path()
    legacy = _legacy_tmp_broker_socket_path()
    if legacy == current or not legacy.exists():
        return []
    try:
        with _CliBrokerClient(legacy) as client:
            client.connect()
            status = client.request("lsp.status", {})
    except (_CliBrokerError, OSError):
        return []
    if not isinstance(status, dict):
        return []
    sessions = _wire_list(cast(dict[str, object], status), "sessions")
    return [
        "split_broker_warning:",
        f"  reachable alternate socket: {legacy}",
        f"  pid: {status.get('pid', '-')} sessions: {len(sessions)}",
        "  restart old MCP clients or stop the alternate broker after migrating work.",
    ]


def _render_global_status(status: dict[str, object], *, started: bool) -> list[str]:
    lines = [
        f"broker: reachable{' (started)' if started else ''}",
        f"pid: {status.get('pid', '-')}",
        f"uptime: {_duration_label(_wire_float(status, 'uptime'))}",
        f"idle_ttl: {_duration_label(_wire_float(status, 'idle_ttl_seconds'))}",
    ]
    bus = status.get("bus")
    if isinstance(bus, dict):
        lines.append(
            "bus: "
            f"events={bus.get('event_count', 0)} "
            f"last={bus.get('last_event_id', 'E0') or 'E0'} "
            f"open_questions={bus.get('open_question_count', 0)}"
        )
    devtools = status.get("devtools")
    if isinstance(devtools, dict):
        lines.append(
            "devtools: "
            f"enabled={bool(devtools.get('enabled'))} "
            f"running={bool(devtools.get('running'))} "
            f"clients={devtools.get('n_clients', 0)}"
        )
    bridge = status.get("babel_bridge")
    if isinstance(bridge, dict):
        lines.append(
            "babel_bridge: "
            f"enabled={bool(bridge.get('enabled'))} "
            f"running={bool(bridge.get('running'))}"
        )
    sessions = [item for item in _wire_list(status, "sessions") if isinstance(item, dict)]
    lines.append(f"sessions: {len(sessions)}")
    if not sessions:
        lines.append("  (none)")
    for item in sessions:
        lines.extend(_render_global_session(cast(dict[str, object], item)))
    return lines


def _render_global_session(session: dict[str, object]) -> list[str]:
    session_id = session.get("session_id", "?")
    root = session.get("root", "?")
    config_hash = session.get("config_hash", "?")
    client_count = session.get("client_count", 0)
    lines = [f"  {session_id} root={root} hash={config_hash} clients={client_count}"]
    lsp = session.get("lsp")
    if not isinstance(lsp, dict):
        lines.append("    lsp: not spawned")
        return lines
    route = str(lsp.get("route_id") or "manual")
    language = str(lsp.get("language") or "-")
    reason = str(lsp.get("route_reason") or "-")
    markers = _wire_list(lsp, "project_markers")
    marker_text = ",".join(str(marker) for marker in markers[:6]) if markers else "-"
    lines.append(
        f"    source: route={route} language={language} reason={reason} markers={marker_text}"
    )
    lines.append(
        "    traffic: "
        f"requests={lsp.get('request_count', 0)} "
        f"last={lsp.get('last_method') or '-'} "
        f"via={lsp.get('last_server_label') or '-'} "
        f"{lsp.get('last_duration_ms', 0)}ms"
    )
    pending = _wire_list(lsp, "pending_workspace_adds")
    if pending:
        lines.append("    pending_workspaces: " + ", ".join(str(path) for path in pending[:6]))
    handlers = lsp.get("method_handlers", {})
    if isinstance(handlers, dict) and handlers:
        rendered = ", ".join(f"{method}->{label}" for method, label in sorted(handlers.items()))
        lines.append(f"    handlers: {_compact_line(rendered, 180)}")
    clients = [item for item in _wire_list(lsp, "clients") if isinstance(item, dict)]
    live_count = sum(1 for item in clients if cast(dict[str, object], item).get("state") == "live")
    lines.append(f"    lsp_clients: {live_count}/{len(clients)} live")
    for item in clients:
        lines.append("      " + _render_global_client(cast(dict[str, object], item)))
    return lines


def _render_global_client(client: dict[str, object]) -> str:
    label = client.get("label", "server")
    command = " ".join(str(part) for part in [client.get("command", ""), *_wire_list(client, "args")] if part)
    state = client.get("state", "unknown")
    pid = client.get("pid") or "-"
    open_docs = client.get("open_documents", 0)
    request_count = client.get("request_count", 0)
    folders = _wire_list(client, "folders")
    folder_text = ", ".join(str(folder) for folder in folders[:4]) if folders else "(no folders)"
    if len(folders) > 4:
        folder_text += f", +{len(folders) - 4}"
    return _compact_line(
        f"{label} {state} pid={pid} cmd={command or '-'} open={open_docs} "
        f"requests={request_count} folders={folder_text}",
        220,
    )


def _duration_label(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


def _workgroup_broker_lines(
    root: str,
    *,
    limit: int,
    include_broker: bool,
    include_weather: bool,
    start_broker: bool,
) -> list[str]:
    if _broker_mode() == "off":
        return ["broker: disabled"]
    try:
        with _open_cli_broker(start_broker=start_broker) as client:
            started = bool(getattr(client, "hsp_started", False))
            status = client.request("bus.status", {"workspace_root": root})
            weather = (
                client.request("bus.weather", {"workspace_root": root, "limit": limit})
                if include_weather
                else None
            )
    except _CliBrokerError as e:
        return [f"broker: unreachable ({e.code}: {e})"]
    except OSError as e:
        return [f"broker: unreachable ({type(e).__name__}: {e})"]
    lines = [f"broker: reachable{' (started)' if started else ''}"]
    if isinstance(status, dict):
        lines.append(
            "bus: "
            f"events={status.get('event_count', 0)} "
            f"last={status.get('last_event_id', 'E0') or 'E0'} "
            f"open_questions={status.get('open_question_count', 0)}"
        )
    if isinstance(weather, dict):
        lines.append("weather:")
        rendered = _render_bus_weather(cast(dict[str, object], weather))
        lines.extend(f"  {line}" for line in rendered.splitlines())
    return lines


def _open_cli_broker(*, start_broker: bool) -> Any:
    if start_broker:
        from hsp.broker_client import BrokerClient

        client = BrokerClient()
        started = client.connect_or_start()
        setattr(client, "hsp_started", started)
        return client
    client = _CliBrokerClient(_broker_socket_path())
    client.connect()
    return client


class _CliBrokerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _CliBrokerClient:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.hsp_started = False
        self._sock: socket.socket | None = None
        self._reader_buf = b""
        self._request_index = 0

    def __enter__(self) -> _CliBrokerClient:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(BROKER_CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(str(self.path))
        except OSError:
            sock.close()
            raise
        sock.settimeout(BROKER_CONNECT_TIMEOUT_SECONDS)
        self._sock = sock

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def request(self, method: str, params: dict[str, object] | None = None) -> object:
        sock = self._sock
        if sock is None:
            raise _CliBrokerError("not_connected", "client not connected")
        self._request_index += 1
        message: dict[str, object] = {"id": f"cli{self._request_index}", "method": method}
        if params is not None:
            message["params"] = params
        frame = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            sock.sendall(frame)
        except OSError as e:
            raise _CliBrokerError("transport", f"send failed: {e!r}") from None
        response = self._decode_response(self._read_line())
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                message_obj = error.get("message", "")
                message_text = message_obj if isinstance(message_obj, str) else json.dumps(message_obj)
                raise _CliBrokerError(code if isinstance(code, str) else "unknown", message_text)
            raise _CliBrokerError("unknown", json.dumps(error))
        return response.get("result")

    def _read_line(self) -> bytes:
        sock = self._sock
        if sock is None:
            raise _CliBrokerError("not_connected", "client not connected")
        while b"\n" not in self._reader_buf:
            try:
                chunk = sock.recv(4096)
            except OSError as e:
                raise _CliBrokerError("transport", f"recv failed: {e!r}") from None
            if not chunk:
                raise _CliBrokerError("transport", "broker closed connection")
            self._reader_buf += chunk
        index = self._reader_buf.index(b"\n")
        line = self._reader_buf[: index + 1]
        self._reader_buf = self._reader_buf[index + 1 :]
        return line

    def _decode_response(self, line: bytes) -> dict[str, object]:
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise _CliBrokerError("decode", f"invalid broker frame: {e}") from None
        if not isinstance(decoded, dict):
            raise _CliBrokerError("decode", f"broker frame was {type(decoded).__name__}")
        return cast(dict[str, object], decoded)


def _render_bus_weather(result: dict[str, object]) -> str:
    lines = [f"workspace: {result.get('workspace_root', '')}"]
    agents = _wire_list(result, "agents")
    lines.append(f"agents: {len(agents)}")
    for agent in agents[:8]:
        if isinstance(agent, dict):
            lines.append(f"  {_agent_label(cast(dict[str, object], agent))}")
    questions = _wire_list(result, "open_questions")
    lines.append(f"open questions: {len(questions)}")
    for question in questions[:5]:
        if isinstance(question, dict):
            q = cast(dict[str, object], question)
            lines.append(
                f"  {q.get('question_id', '')} {_wire_float(q, 'seconds_left'):.0f}s "
                f"{q.get('message', '')}"
            )
    recent = _wire_list(result, "recent")
    lines.append(f"recent: {len(recent)}")
    for event in recent[-5:]:
        if isinstance(event, dict):
            lines.append(f"  {_event_label(cast(dict[str, object], event))}")
    return "\n".join(lines)


def _agent_label(agent: dict[str, object]) -> str:
    agent_id = str(agent.get("agent_id") or agent.get("client_id") or agent.get("session_id") or "?")
    state = str(agent.get("state") or agent.get("status") or "?")
    idle = _wire_float(agent, "idle_seconds")
    last = str(agent.get("last_event_id") or "")
    prompt_count = agent.get("prompt_count", 0)
    pinned = " pinned" if agent.get("pinned") else ""
    return _compact_line(f"{agent_id} {state} idle={idle:.0f}s prompts={prompt_count}{pinned} last={last}", 180)


def _event_label(event: dict[str, object] | None) -> str:
    if not event:
        return "(unknown event)"
    event_id = str(event.get("event_id", ""))
    if event_id and not event_id.startswith("E"):
        event_id = f"E{event_id}"
    event_type = str(event.get("event_type", "") or event.get("kind", ""))
    message = str(event.get("message", ""))
    timestamp = _event_timestamp_label(event)
    head = " ".join(part for part in (event_id, timestamp, event_type) if part).strip()
    if message:
        head += f" {message}"
    agent = _event_agent_label(event)
    if agent:
        head += f" @{agent}"
    scope = _render_bus_scope(event)
    if scope:
        head += f" [{scope}]"
    return _compact_line(head, 220)


def _event_agent_label(event: dict[str, object]) -> str:
    return str(event.get("agent_id") or event.get("client_id") or event.get("session_id") or "").strip()


def _event_timestamp_label(event: dict[str, object]) -> str:
    raw = event.get("timestamp")
    if not isinstance(raw, int | float) or raw <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(float(raw)))


def _render_bus_scope(item: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("files", "symbols", "aliases"):
        values = _wire_list(item, key)
        if values:
            parts.append(f"{key}=" + ",".join(str(value) for value in values[:5]))
    return " ".join(parts)


def _wire_list(container: dict[str, object], key: str) -> list[object]:
    value = container.get(key, [])
    return cast(list[object], value) if isinstance(value, list) else []


def _wire_float(container: dict[str, object], key: str, default: float = 0.0) -> float:
    value = container.get(key, default)
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _compact_line(text: str, limit: int = 180) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _workgroup_lsp_status(root: str) -> str:
    old_root = os.environ.get("LSP_ROOT")
    os.environ["LSP_ROOT"] = root
    try:
        return asyncio.run(_server().lsp_session(action="status"))
    finally:
        if old_root is None:
            os.environ.pop("LSP_ROOT", None)
        else:
            os.environ["LSP_ROOT"] = old_root


def _command_argv(argv: list[str]) -> list[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _hooks_enabled() -> bool:
    raw = os.environ.get("HSP_HOOKS", "1").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return True


def _require_ticket_for_edits() -> bool:
    raw = os.environ.get("HSP_REQUIRE_TICKET_FOR_EDITS", "").strip().lower()
    return raw in TRUE_VALUES


def _authoritative_build_enabled() -> bool:
    raw = os.environ.get("HSP_AUTHORITATIVE_BUILD", "1").strip().lower()
    return raw not in FALSE_VALUES


def _is_edit_before_hook(kind: str) -> bool:
    return kind in {"edit.before", "write.before"}


def _write_hook_denial(reason: str) -> None:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()


def _edit_denial_reason(gate: str) -> str:
    gate = gate.strip()
    return f"{EDIT_DENY_REASON}\n\n{gate}" if gate else EDIT_DENY_REASON


def _build_gate_reason(gate: str) -> str:
    first = gate.splitlines()[0] if gate else ""
    start = first.find("(")
    end = first.find(")", start + 1)
    if start == -1 or end == -1:
        return ""
    return first[start + 1:end]


def _run_authoritative_build_batch(
    *,
    command: str,
    gate: str,
    files: str,
    full_workspace: bool,
) -> dict[str, object]:
    root = Path(project_root_for(os.environ.get("LSP_ROOT", os.getcwd()))).resolve()
    directory = root / "tmp" / "hsp-build-batches"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{root}\n{command}\n{gate}".encode("utf-8")).hexdigest()[:24]
    result_path = directory / f"{key}.json"
    lock_path = directory / f"{key}.lock"
    ttl = _duration_env("HSP_BUILD_BATCH_TTL", BUILD_BATCH_DEFAULT_TTL_SECONDS)
    wait_timeout = _duration_env("HSP_BUILD_BATCH_WAIT_TIMEOUT", BUILD_BATCH_DEFAULT_WAIT_SECONDS)
    fresh = _read_fresh_batch_result(result_path, ttl)
    if fresh is not None:
        fresh["owner"] = False
        return fresh
    if _try_create_lock(lock_path, ttl):
        try:
            result = _run_build_command(command, root=root)
            result.update({
                "command": command,
                "gate": gate,
                "key": key,
                "owner": True,
                "timestamp": time.time(),
            })
            _write_batch_result(result_path, result)
            _record_authoritative_build_result(command, result, files=files, full_workspace=full_workspace)
            return result
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
    waited = _wait_for_batch_result(result_path, wait_timeout)
    if waited is not None:
        waited["owner"] = False
        return waited
    return {
        "command": command,
        "gate": gate,
        "key": key,
        "owner": False,
        "returncode": 124,
        "status": "failed",
        "stdout": "",
        "stderr": f"timed out waiting for HSP build batch result after {wait_timeout:.0f}s",
        "timestamp": time.time(),
    }


def _run_build_command(command: str, *, root: Path) -> dict[str, object]:
    completed = subprocess.run(
        command,
        shell=True,
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "returncode": int(completed.returncode),
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def _record_authoritative_build_result(
    command: str,
    result: dict[str, object],
    *,
    files: str,
    full_workspace: bool,
) -> None:
    scope_files = "" if full_workspace else files
    asyncio.run(
        _server().lsp_log(
            action="event",
            message=command,
            files=scope_files,
            kind="test.ran",
            status=str(result.get("status", "")),
            targets=command,
        )
    )


def _build_batch_denial_reason(result: dict[str, object]) -> str:
    owner = bool(result.get("owner"))
    action = "ran this command once" if owner else "reused the batched result"
    command = str(result.get("command", ""))
    returncode = result.get("returncode", "?")
    stdout = _truncate_capture(str(result.get("stdout", "")))
    stderr = _truncate_capture(str(result.get("stderr", "")))
    lines = [
        f"HSP build mutex {action} for the project and denied duplicate Bash execution.",
        f"$ {command}",
        f"exit: {returncode}",
    ]
    if stdout:
        lines.extend(["--- stdout ---", stdout])
    if stderr:
        lines.extend(["--- stderr ---", stderr])
    return "\n".join(lines).strip()


def _truncate_capture(text: str) -> str:
    if len(text) <= BUILD_BATCH_CAPTURE_LIMIT:
        return text.rstrip()
    head = text[:BUILD_BATCH_CAPTURE_LIMIT]
    return f"{head.rstrip()}\n... truncated {len(text) - BUILD_BATCH_CAPTURE_LIMIT} char(s)"


def _read_fresh_batch_result(path: Path, ttl: float) -> dict[str, object] | None:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast(dict[str, object], payload) if isinstance(payload, dict) else None


def _wait_for_batch_result(path: Path, timeout: float) -> dict[str, object] | None:
    deadline = time.time() + timeout
    while time.time() <= deadline:
        result = _read_fresh_batch_result(path, max(timeout, BUILD_BATCH_DEFAULT_TTL_SECONDS))
        if result is not None:
            return result
        time.sleep(0.2)
    return None


def _write_batch_result(path: Path, result: dict[str, object]) -> None:
    path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


def _try_create_lock(path: Path, ttl: float) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
        if age > ttl:
            path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def _duration_env(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = _server()._parse_bus_duration(value, default=default)
    except Exception:
        return default
    return default if isinstance(parsed, str) else float(parsed)


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
        if data.get("success") is True:
            return "success"
        if data.get("success") is False:
            return "error"
    if payload.get("success") is True:
        return "success"
    if payload.get("success") is False:
        return "error"
    return ""


def _hook_command(payload: dict[str, object]) -> str:
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    for key in ("tool_input", "toolInput", "input"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            data = cast(dict[str, object], nested)
            command = data.get("command")
            if isinstance(command, str) and command.strip():
                return command.strip()
    return ""


def _is_build_before_hook(kind: str, payload: dict[str, object], command: str) -> bool:
    return kind in {"tool.before", "bash.before"} and _hook_tool_name(payload) == "Bash" and _is_build_command(command)


def _is_build_after_hook(kind: str, payload: dict[str, object], command: str) -> bool:
    return kind in {"tool.after", "bash.after"} and _hook_tool_name(payload) == "Bash" and _is_build_command(command)


def _hook_tool_name(payload: dict[str, object]) -> str:
    return _string_value(payload, "tool_name", "toolName", "name")


def _is_build_command(command: str) -> bool:
    return _command_gate_spec(command) is not None


def _command_gate_spec(command: str) -> CommandGateSpec | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return _gate_spec_for_argv(argv)


def _gate_spec_for_argv(argv: list[str]) -> CommandGateSpec | None:
    if not argv:
        return None
    argv = _strip_env_assignments(argv)
    if not argv:
        return None
    first = os.path.basename(argv[0])
    if first in RUNNER_TOKENS:
        nested = _runner_inner_argv(first, argv)
        if nested:
            return _gate_spec_for_argv(nested)
    if first == "python" and len(argv) >= 3 and argv[1] == "-m":
        module = argv[2]
        if module in PYTHON_MODULE_CHECKERS:
            return _path_scoped_spec(argv, argv[3:])
    if first in DIRECT_CHECKER_TOKENS:
        return _path_scoped_spec(argv, argv[1:])
    if first in {"make", "just", "ninja", "cmake", "gradle", "mvn", "rk", "xcodebuild"}:
        return CommandGateSpec(tuple(argv), full_workspace=True)
    if first == "spaceship":
        return _spaceship_gate_spec(argv)
    if first == "uv":
        nested = _runner_inner_argv(first, argv)
        return _gate_spec_for_argv(nested) if nested else None
    if first in {"npm", "pnpm", "yarn"}:
        return _node_gate_spec(first, argv)
    if first == "bun":
        return _bun_gate_spec(argv)
    if first == "deno":
        return _deno_gate_spec(argv)
    if first == "go":
        return _go_gate_spec(argv)
    if first == "cargo":
        return _subcommand_gate_spec(argv)
    if first in {"dotnet", "swift"}:
        return _subcommand_gate_spec(argv)
    if first in {"tox", "nox", "composer"}:
        return CommandGateSpec(tuple(argv), full_workspace=True)
    if first not in BUILD_FIRST_TOKENS:
        return None
    if len(argv) > 1 and argv[1] in BUILD_SUBCOMMANDS:
        return CommandGateSpec(tuple(argv), full_workspace=True)
    return None


def _strip_env_assignments(argv: list[str]) -> list[str]:
    idx = 0
    while idx < len(argv) and "=" in argv[idx] and not argv[idx].startswith("-"):
        name, _value = argv[idx].split("=", 1)
        if not name.replace("_", "").isalnum():
            break
        idx += 1
    return argv[idx:]


def _runner_inner_argv(first: str, argv: list[str]) -> list[str]:
    if first == "uv":
        if len(argv) < 2 or argv[1] not in {"run", "tool"}:
            return []
        return _skip_runner_options(argv[2:])
    if first in {"poetry", "pipenv"}:
        if len(argv) < 2 or argv[1] != "run":
            return []
        return _skip_runner_options(argv[2:])
    if first == "npx":
        return _skip_runner_options(argv[1:])
    return []


def _skip_runner_options(argv: list[str]) -> list[str]:
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--":
            return argv[idx + 1:]
        if not arg.startswith("-"):
            return argv[idx:]
        idx += 2 if _option_takes_value(arg) and idx + 1 < len(argv) else 1
    return []


def _node_gate_spec(first: str, argv: list[str]) -> CommandGateSpec | None:
    if len(argv) < 2:
        return None
    sub = argv[1]
    if first == "npm" and sub in {"test", "build", "lint", "publish"}:
        return CommandGateSpec(tuple(argv), full_workspace=True)
    if sub in {"test", "build", "lint", "publish"}:
        return CommandGateSpec(tuple(argv), full_workspace=True)
    if sub in {"run", "exec", "dlx"} and len(argv) >= 3:
        if sub == "run":
            return CommandGateSpec(tuple(argv), full_workspace=True)
        return _gate_spec_for_argv(argv[2:])
    return None


def _go_gate_spec(argv: list[str]) -> CommandGateSpec | None:
    if len(argv) < 2 or argv[1] not in {"test", "build", "vet", "list"}:
        return None
    paths = _command_paths(argv[2:])
    if _paths_cover_workspace(paths):
        return CommandGateSpec(tuple(argv), full_workspace=True)
    return CommandGateSpec(tuple(argv), full_workspace=not paths, files=tuple(paths))


def _bun_gate_spec(argv: list[str]) -> CommandGateSpec | None:
    if len(argv) < 2:
        return None
    sub = argv[1]
    if sub == "test":
        paths = _command_paths(argv[2:])
        if _paths_cover_workspace(paths):
            return CommandGateSpec(tuple(argv), full_workspace=True)
        return CommandGateSpec(tuple(argv), full_workspace=not paths, files=tuple(paths))
    if sub in {"run", "build"}:
        return CommandGateSpec(tuple(argv), full_workspace=True)
    return None


def _deno_gate_spec(argv: list[str]) -> CommandGateSpec | None:
    if len(argv) < 2 or argv[1] not in {"check", "fmt", "lint", "test"}:
        return None
    paths = _command_paths(argv[2:])
    if _paths_cover_workspace(paths):
        return CommandGateSpec(tuple(argv), full_workspace=True)
    return CommandGateSpec(tuple(argv), full_workspace=not paths, files=tuple(paths))


def _subcommand_gate_spec(argv: list[str]) -> CommandGateSpec | None:
    if len(argv) < 2 or argv[1] not in BUILD_SUBCOMMANDS:
        return None
    return CommandGateSpec(tuple(argv), full_workspace=True)


def _spaceship_gate_spec(argv: list[str]) -> CommandGateSpec | None:
    if len(argv) < 2 or argv[1] not in {"build", "check", "upgrade"}:
        return None
    return CommandGateSpec(tuple(argv), full_workspace=True)


def _path_scoped_spec(argv: list[str], args: list[str]) -> CommandGateSpec:
    paths = _command_paths(args)
    if _paths_cover_workspace(paths):
        return CommandGateSpec(tuple(argv), full_workspace=True)
    return CommandGateSpec(tuple(argv), full_workspace=not paths, files=tuple(paths))


def _paths_cover_workspace(paths: list[str]) -> bool:
    return any(path in {".", "./", "./...", "..."} for path in paths)


def _command_paths(args: list[str]) -> list[str]:
    paths: list[str] = []
    idx = 0
    after_double_dash = False
    while idx < len(args):
        arg = args[idx]
        if arg == "--":
            after_double_dash = True
            idx += 1
            continue
        if not after_double_dash and arg.startswith("-"):
            idx += 2 if _option_takes_value(arg) and idx + 1 < len(args) else 1
            continue
        if _looks_like_path(arg):
            paths.append(arg)
        idx += 1
    return _dedupe(paths)


def _option_takes_value(arg: str) -> bool:
    return arg in PATHY_OPTIONS_WITH_VALUE and "=" not in arg


def _looks_like_path(arg: str) -> bool:
    if not arg or arg.startswith("-"):
        return False
    if arg in {".", ".."}:
        return True
    return (
        "/" in arg
        or arg.startswith(".")
        or Path(arg).suffix != ""
        or Path(arg).exists()
    )


def _build_status(status: str) -> str:
    if status in {"success", "passed", "ok"}:
        return "passed"
    if status in {"error", "failed", "interrupted"}:
        return "failed"
    return status


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
