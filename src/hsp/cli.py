"""Command-line surface for hsp.

The MCP server stays the default `hsp` behavior for Claude Code, while
agent-bus hooks live under the same binary as `hsp log ...`. Keeping one
entrypoint avoids install-path drift between MCP, broker, and harness hooks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hsp import server
from hsp.broker import BrokerError, broker_log_path, socket_path
from hsp.broker_client import BrokerClient
from hsp.bus_registry import BrokerMode, log_path_for, workspace_id_for

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"", "0", "false", "no", "off"}
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
    if ns.command == "log":
        return _run_log(ns, parser)
    if ns.command == "hook":
        return _run_hook(ns, parser)
    if ns.command == "run":
        return _run_command(ns, parser)
    if ns.command == "workgroup":
        return _run_workgroup(ns)
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

    workgroup = subcommands.add_parser(
        "workgroup",
        help="debug workgroup root, broker, and bus status from one or more locations",
    )
    workgroup.add_argument(
        "locations",
        nargs="*",
        help="directories or files to evaluate; defaults to the current directory",
    )
    workgroup.add_argument("--limit", type=int, default=8)
    workgroup.add_argument("--start-broker", action="store_true")
    workgroup.add_argument("--lsp", action="store_true", help="include lsp_session status for each location")
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
    command = _hook_command(payload)
    if _is_edit_before_hook(kind) and _require_ticket_for_edits():
        gate = asyncio.run(
            server.lsp_log(
                action="edit_gate",
                message=message,
                files=_join_scope(str(ns.files), _hook_files(payload)),
                symbols=_join_scope(str(ns.symbols), _hook_symbols(payload)),
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
            server.implicit_build_gate(
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
    files = _join_scope(str(ns.files), _hook_files(payload))
    symbols = _join_scope(str(ns.symbols), _hook_symbols(payload))
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


def _run_command(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    argv = _command_argv(cast(list[str], ns.argv))
    if not argv:
        parser.error("hsp run requires a command after --")

    message = str(ns.message).strip() or " ".join(argv)
    gate_spec = _gate_spec_for_argv(argv)
    gate = asyncio.run(
        server.implicit_build_gate(
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
            server.lsp_log(
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
            start_broker=bool(ns.start_broker),
            include_lsp=bool(ns.lsp),
        )
        for location in locations
    ]
    print("\n\n".join(blocks))
    return 0


def _workgroup_block(
    *,
    location: str,
    limit: int,
    start_broker: bool,
    include_lsp: bool,
) -> str:
    root = _workgroup_root_for_location(location)
    wsid = workspace_id_for(root)
    lines = [
        f"workgroup: {root}",
        f"location: {Path(location).expanduser()}",
        f"workspace_id: {wsid}",
        f"env LSP_ROOT: {os.environ.get('LSP_ROOT', '(unset)')}",
        f"broker mode: {server._broker_mode()}",
        f"broker socket: {socket_path()}",
        f"broker log: {broker_log_path()}",
    ]
    lines.extend(_workgroup_log_lines(root))
    lines.extend(_workgroup_broker_lines(root, limit=limit, start_broker=start_broker))
    if include_lsp:
        lines.append("lsp:")
        lines.extend(f"  {line}" for line in _workgroup_lsp_status(root).splitlines())
    return "\n".join(lines)


def _workgroup_root_for_location(location: str) -> str:
    path = Path(location or ".").expanduser()
    absolute = path if path.is_absolute() else Path.cwd() / path
    try:
        resolved = absolute.resolve(strict=False)
    except OSError:
        resolved = absolute.absolute()
    if resolved.exists() and resolved.is_file():
        resolved = resolved.parent
    return str(resolved)


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


def _workgroup_broker_lines(root: str, *, limit: int, start_broker: bool) -> list[str]:
    if server._broker_mode() == "off":
        return ["broker: disabled"]
    try:
        with BrokerClient() as client:
            started = client.connect_or_start() if start_broker else _connect_existing_broker(client)
            status = client.request("bus.status", {"workspace_root": root})
            weather = client.request("bus.weather", {"workspace_root": root, "limit": limit})
    except BrokerError as e:
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
        rendered = server._render_bus_weather(cast(dict[str, object], weather))
        lines.extend(f"  {line}" for line in rendered.splitlines())
    return lines


def _connect_existing_broker(client: BrokerClient) -> bool:
    client.connect()
    return False


def _workgroup_lsp_status(root: str) -> str:
    old_root = os.environ.get("LSP_ROOT")
    os.environ["LSP_ROOT"] = root
    try:
        return asyncio.run(server.lsp_session(action="status"))
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
    raw = os.environ.get("HSP_HOOKS", "").strip().lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return False


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
    root = Path(os.environ.get("LSP_ROOT", os.getcwd())).resolve()
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
        server.lsp_log(
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
        f"HSP build mutex {action} for the workgroup and denied duplicate Bash execution.",
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
        parsed = server._parse_bus_duration(value, default=default)
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
