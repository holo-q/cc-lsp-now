"""hsp-broker daemon.

This is the broker described in `docs/broker.md`: a user-level Unix-domain
socket JSONL daemon that supervises shared workspace sessions for
`hsp` and owns the expensive LSP processes for those sessions.
MCP servers in agent subprocesses connect here instead of each spawning a
fresh language-server chain.

- a stable socket path derivation (so any number of clients agree on
  where to find the broker without environment plumbing);
- a small JSON-line request/response protocol (one request per line, one
  response per line);
- a workspace session registry keyed by `(root, config_hash)` so two
  clients asking for the same workspace get the same session record;
- shared LSP request forwarding, with builtin route selection owned by the
  broker when clients send `router=true`;
- clean start/stop helpers that the client side can drive (`session.stop`
  / a graceful `shutdown` request).

The protocol intentionally does not implement JSON-RPC framing — JSONL
is enough for the workloads we expect (request/response pairs, not high
fanout).  The wire shape is stable text:

    request:  {"id": "<opaque>", "method": "...", "params": {...}}
    response: {"id": "<opaque>", "result": {...}}
              {"id": "<opaque>", "error": {"code": "...", "message": "..."}}

`id` is echoed back unchanged (or `null` if the request omitted it); the
client can pipeline by using distinct ids on the same connection.
"""

from __future__ import annotations

import asyncio
import getpass
import importlib
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from hsp.agent_bus import AgentBus
from hsp.babel_bridge import subscribe_babel_events
from hsp.alias_coordinator import (
    alias_identity_from_wire,
    alias_record_to_wire,
    alias_touch_result_to_wire,
)
from hsp.broker_session import (
    SessionKey,
    SessionRegistry,
    session_to_dict,
)
from hsp.broker_lsp import (
    BrokerLspManager,
    chain_config_hash,
    chain_from_wire,
)
from hsp.chain_server import ChainServer
from hsp.lsp_chain_config import parse_chain as parse_lsp_chain
from hsp.lsp_chain_config import parse_prefer as parse_lsp_prefer
from hsp.lsp import LspError
from hsp.router import BUILTIN_ROUTES, LanguageRoute, find_project_root, get_route, resolve_route_id_for_path

log = logging.getLogger(__name__)


# --- Socket path -------------------------------------------------------------

DEFAULT_SOCKET_NAME = "hsp-broker.sock"
SOCKET_ENV_OVERRIDE = "HSP_BROKER_SOCKET"
LOG_ENV_OVERRIDE = "HSP_BROKER_LOG"
IDLE_TTL_ENV = "HSP_BROKER_IDLE_TTL_SECONDS"
DEVTOOLS_ENV = "LSP_DEVTOOLS"
DEVTOOLS_APP_ID_ENV = "LSP_DEVTOOLS_APP_ID"
DEVTOOLS_HOST_ENV = "LSP_DEVTOOLS_HOST"
DEVTOOLS_PORT_ENV = "LSP_DEVTOOLS_PORT"
DEVTOOLS_READONLY_ENV = "LSP_DEVTOOLS_READONLY"
BABEL_BRIDGE_ENV = "HSP_BABEL_BRIDGE"
DEFAULT_IDLE_TTL_SECONDS = 4 * 60 * 60


def socket_path() -> Path:
    """Return the user-scoped Unix-domain socket path for the broker.

    Resolution order (first match wins):

    1. `$HSP_BROKER_SOCKET` — explicit override, used in tests and by
       users who run an isolated broker for a single project.
    2. `$XDG_RUNTIME_DIR/hsp-broker.sock` — the canonical location on
       systemd-managed systems; the directory is already user-private and
       cleaned on logout.
    3. `/tmp/hsp-broker-<user>/hsp-broker.sock` — fallback for shells
       without `$XDG_RUNTIME_DIR` (containers, minimal envs).  The parent
       directory is created mode `0o700` so a multi-user box keeps
       per-user isolation.

    The path is the same for every caller in the same shell / login
    session — this is what lets `BrokerClient` auto-discover the broker
    without any handshake file.
    """
    override = os.environ.get(SOCKET_ENV_OVERRIDE)
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / DEFAULT_SOCKET_NAME
    user = os.environ.get("USER") or _safe_user() or str(os.getuid())
    base = Path(f"/tmp/hsp-broker-{user}")
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        pass
    return base / DEFAULT_SOCKET_NAME


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def broker_log_path() -> Path:
    """Return the broker log file path.

    Logs belong in durable user state, not the project tree and not the
    socket's runtime directory.  The override is intentionally one env var
    so test harnesses and users can isolate broker traces when needed.
    """
    override = os.environ.get(LOG_ENV_OVERRIDE)
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "hsp" / "broker.log"


# --- Protocol framing --------------------------------------------------------


class BrokerError(Exception):
    """Wire-shaped error raised inside request handlers.

    Code is a short string (e.g. `"unknown_method"`); the broker formats
    it into a structured `error` field on the response.  Plain `Exception`
    instances bubble up as `internal` errors with `repr(exc)` as the
    message — that path is for bugs, not for protocol-level negative
    answers.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def encode_message(msg: dict[str, object]) -> bytes:
    """Serialise one JSONL frame.

    Sorted keys + compact separators give a deterministic byte layout —
    helpful for snapshot tests and for hashing requests in later slices.
    """
    return (json.dumps(msg, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> dict[str, object]:
    """Parse one JSONL frame.  Trailing newline is optional.

    Raises `BrokerError("invalid_request", ...)` for any framing-level
    failure (non-JSON, non-object root) so handlers can surface the same
    structured error regardless of which decode site detected the
    problem.
    """
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="replace")
    else:
        text = line
    text = text.strip()
    if not text:
        raise BrokerError("invalid_request", "empty frame")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise BrokerError("invalid_request", f"malformed json: {e.msg}") from None
    if not isinstance(obj, dict):
        raise BrokerError("invalid_request", "frame must be a JSON object")
    return obj


# --- Daemon ------------------------------------------------------------------


Handler = Callable[[dict[str, object]], Awaitable[object]]


class BrokerDaemon:
    """In-process broker state.

    All wire-facing logic lives in `handle_request`; the asyncio
    socket plumbing in `serve_unix` is a thin wrapper that reads JSONL
    frames and pipes them through this method.  Tests can drive the
    daemon directly without sockets by `await`-ing `handle_request`.
    """

    def __init__(self) -> None:
        self.registry = SessionRegistry()
        self.lsp = BrokerLspManager(self.registry)
        self.bus = AgentBus()
        self.started_at: float = time.time()
        self.devtools = _maybe_start_devtools(self)
        self.babel_bridge_task: asyncio.Task[None] | None = None
        # Set when a `shutdown` request is processed.  `serve_unix` waits
        # on this to break out of `serve_forever`.
        self._shutdown = asyncio.Event()

    @property
    def shutdown_event(self) -> asyncio.Event:
        return self._shutdown

    async def handle_request(self, req: dict[str, object]) -> dict[str, object]:
        """Dispatch one decoded request and return its response object.

        The response always carries the same `id` as the request (echoed
        verbatim).  Handlers can raise `BrokerError` for protocol-level
        failures; anything else surfaces as an `internal` error.
        """
        rid = req.get("id")
        method = req.get("method")
        params_obj = req.get("params") or {}
        if not isinstance(method, str) or not method:
            return _error_response(rid, "invalid_request", "missing method")
        if not isinstance(params_obj, dict):
            return _error_response(rid, "invalid_request", "params must be an object")
        params = cast(dict[str, object], params_obj)
        try:
            await self._evict_idle_sessions()
            result = await self._dispatch(method, params)
        except BrokerError as e:
            return _error_response(rid, e.code, str(e))
        except LspError as e:
            return _error_response(rid, f"lsp:{e.code}", str(e))
        except Exception as e:
            log.exception("broker handler crashed: method=%s", method)
            return _error_response(rid, "internal", repr(e))
        return {"id": rid, "result": result}

    async def _dispatch(self, method: str, params: dict[str, object]) -> object:
        if method == "ping":
            return {"pong": True}
        if method == "status":
            return self._status()
        if method == "session.get_or_create":
            return self._session_get_or_create(params)
        if method == "session.list":
            return [session_to_dict(s) for s in self.registry.all_sessions()]
        if method == "session.stop":
            sid = _str_param(params, "session_id")
            return {"stopped": await self.lsp.stop_session(sid)}
        if method == "session.stop_matching":
            if _bool_param(params, "router"):
                config = _lsp_session_config(params)
                return {
                    "stopped": await self.lsp.stop_matching(
                        root=config.root,
                        config_hash_value=config.config_hash,
                    )
                }
            return {
                "stopped": await self.lsp.stop_matching(
                    root=_str_param(params, "root"),
                    config_hash_value=_str_param(params, "config_hash"),
                )
            }
        if method == "lsp.status":
            return self._lsp_status()
        if method == "lsp.request":
            return await self._lsp_request(params)
        if method == "lsp.add_workspace":
            return await self._lsp_add_workspace(params)
        if method == "lsp.diagnostics":
            return await self._lsp_diagnostics(params)
        if method == "lsp.notify_files":
            return await self._lsp_notify_files(params)
        if method == "render.touch":
            return await self._render_touch(params)
        if method == "render.lookup":
            return await self._render_lookup(params)
        if method == "render.status":
            return self._render_status(params)
        if method == "render.reset_client":
            return await self._render_reset_client(params)
        if method == "render.reset_session":
            return await self._render_reset_session(params)
        if method == "bus.status":
            return self.bus.status()
        if method == "bus.event" or method == "bus.append":
            try:
                return self.bus.event(params)
            except ValueError as e:
                raise BrokerError("invalid_params", str(e)) from None
        if method == "bus.heartbeat":
            return self.bus.heartbeat(params)
        if method == "bus.note":
            return self.bus.note(params)
        if method == "bus.ask":
            return self.bus.ask(params)
        if method == "bus.reply":
            try:
                return self.bus.reply(params)
            except ValueError as e:
                raise BrokerError("invalid_params", str(e)) from None
        if method == "bus.recent":
            return self.bus.recent(params)
        if method == "bus.settle":
            return self.bus.settle(params)
        if method == "bus.precommit":
            return self.bus.precommit(params)
        if method == "bus.postcommit":
            return self.bus.postcommit(params)
        if method == "bus.weather":
            return self.bus.weather(params)
        if method == "bus.presence" or method == "bus.workgroup":
            return self.bus.presence(params)
        if method == "shutdown":
            self.stop_babel_bridge()
            await self.lsp.stop_all()
            if self.devtools is not None:
                self.devtools.stop()
            self._shutdown.set()
            return {"shutting_down": True}
        raise BrokerError("unknown_method", f"unknown method: {method}")

    async def _evict_idle_sessions(self) -> None:
        evicted = await self.lsp.evict_idle(ttl_seconds=_idle_ttl_seconds())
        if evicted:
            log.info("evicted idle broker sessions: %s", ",".join(evicted))

    def _status(self) -> dict[str, object]:
        now = time.time()
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime": now - self.started_at,
            "session_count": len(self.registry),
            "sessions": [session_to_dict(s) for s in self.registry.all_sessions()],
            "bus": self.bus.status(),
            "devtools": _devtools_status(self.devtools),
            "babel_bridge": self._babel_bridge_status(),
        }

    def _lsp_status(self) -> dict[str, object]:
        status = self.lsp.lsp_status()
        status.update(
            {
                "pid": os.getpid(),
                "socket": str(socket_path()),
                "log_path": str(broker_log_path()),
                "started_at": self.started_at,
                "uptime": time.time() - self.started_at,
                "idle_ttl_seconds": _idle_ttl_seconds(),
                "bus": self.bus.status(),
                "devtools": _devtools_status(self.devtools),
                "babel_bridge": self._babel_bridge_status(),
            }
        )
        return status

    def start_babel_bridge(self) -> None:
        if self.babel_bridge_task is not None and not self.babel_bridge_task.done():
            return
        self.babel_bridge_task = asyncio.create_task(subscribe_babel_events(self.bus))
        log.info("babel bridge enabled")

    def stop_babel_bridge(self) -> None:
        task = self.babel_bridge_task
        if task is None:
            return
        task.cancel()

    def _babel_bridge_status(self) -> dict[str, object]:
        task = self.babel_bridge_task
        return {
            "enabled": _env_enabled(BABEL_BRIDGE_ENV),
            "running": bool(task is not None and not task.done()),
            "done": bool(task is not None and task.done()),
        }

    def _session_get_or_create(self, params: dict[str, object]) -> dict[str, object]:
        root = _str_param(params, "root")
        chash = _str_param(params, "config_hash")
        label_obj = params.get("server_label", "")
        label = label_obj if isinstance(label_obj, str) else ""
        session = self.registry.get_or_create(
            SessionKey(root=root, config_hash=chash),
            server_label=label,
        )
        return session_to_dict(session)

    def _lsp_session_from_params(self, params: dict[str, object]):
        config = _lsp_session_config(params)
        _sid, session = self.lsp.get_or_create(
            root=config.root,
            config_hash_value=config.config_hash,
            chain=config.chain,
            server_label=config.server_label,
            prefer=config.prefer,
            project_markers=config.project_markers,
            language=config.language,
            route_id=config.route_id,
            route_reason=config.route_reason,
        )
        return session

    async def _lsp_request(self, params: dict[str, object]) -> dict[str, object]:
        session = self._lsp_session_from_params(params)
        method = _str_param(params, "lsp_method")
        lsp_params = params.get("lsp_params")
        if lsp_params is not None and not isinstance(lsp_params, dict):
            raise BrokerError("invalid_params", "lsp_params must be an object or null")
        uri_obj = params.get("uri")
        uri = uri_obj if isinstance(uri_obj, str) and uri_obj else None
        empty_fallback = set(_str_list_param(params, "empty_fallback_methods"))
        result = await session.request(
            method,
            cast(dict | None, lsp_params),
            uri=uri,
            empty_fallback_methods=empty_fallback,
        )
        return result.to_wire()

    async def _lsp_add_workspace(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        path = _str_param(params, "path")
        return await session.add_workspace(path)

    async def _lsp_diagnostics(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        uri = _str_param(params, "uri")
        return await session.diagnostics(uri)

    async def _lsp_notify_files(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        renamed = _rename_list_param(params, "renamed")
        created = _str_list_param(params, "created")
        deleted = _str_list_param(params, "deleted")
        return await session.notify_files(
            renamed=renamed,
            created=created,
            deleted=deleted,
        )

    async def _render_touch(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        client_id = _str_param(params, "client_id")
        identities_obj = params.get("identities", [])
        if not isinstance(identities_obj, list):
            raise BrokerError("invalid_params", "identities must be a list")
        try:
            identities = [alias_identity_from_wire(item) for item in identities_obj]
        except ValueError as e:
            raise BrokerError("invalid_params", str(e)) from None
        result = await session.render_touch(client_id, identities)
        return alias_touch_result_to_wire(result)

    async def _render_lookup(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        token = _str_param(params, "token")
        result = await session.render_lookup(token)
        if result.ok and result.record is not None:
            return {"ok": True, "record": alias_record_to_wire(result.record), "message": ""}
        return {
            "ok": False,
            "error": result.error.value if result.error is not None else "unknown",
            "message": result.message,
        }

    def _render_status(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        status = session.aliases.status()
        if bool(params.get("include_records", False)):
            status["records"] = [
                alias_record_to_wire(record)
                for record in session.aliases.memory.snapshot().records
            ]
        return status

    async def _render_reset_client(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        client_id = _str_param(params, "client_id")
        return await session.render_reset_client(client_id)

    async def _render_reset_session(self, params: dict[str, object]) -> object:
        session = self._lsp_session_from_params(params)
        reason_obj = params.get("reason", "")
        reason = reason_obj if isinstance(reason_obj, str) else ""
        return await session.render_reset_session(reason)


@dataclass(frozen=True)
class LspSessionConfig:
    root: str
    config_hash: str
    chain: list[ChainServer]
    server_label: str
    prefer: dict[str, int]
    project_markers: list[str]
    language: str
    route_id: str
    route_reason: str


def _error_response(
    rid: object, code: str, message: str
) -> dict[str, object]:
    return {"id": rid, "error": {"code": code, "message": message}}


def _bool_param(params: dict[str, object], name: str) -> bool:
    value = params.get(name, False)
    return bool(value) if isinstance(value, bool) else False


def _optional_str_param(params: dict[str, object], name: str) -> str:
    value = params.get(name, "")
    return value if isinstance(value, str) else ""


def _uri_to_path(uri: str) -> str:
    return uri.removeprefix("file://") if uri.startswith("file://") else uri


def _route_probe_path(params: dict[str, object], root: str) -> str:
    uri = _optional_str_param(params, "uri")
    if uri:
        return _uri_to_path(uri)
    for name in ("route_path", "path"):
        value = _optional_str_param(params, name)
        if value:
            return value
    renamed = params.get("renamed", [])
    if isinstance(renamed, list) and renamed:
        first = renamed[0]
        if isinstance(first, list) and first and isinstance(first[0], str):
            return first[0]
    for name in ("created", "deleted"):
        values = params.get(name, [])
        if isinstance(values, list) and values and isinstance(values[0], str):
            return values[0]
    return root


def _route_env(route: LanguageRoute, name: str, default: str = "") -> str:
    if name in route.env:
        return route.env[name]
    return os.environ.get(name, default)


def _route_markers(route: LanguageRoute) -> list[str]:
    raw = _route_env(route, "LSP_PROJECT_MARKERS", ".git").strip()
    return [m.strip() for m in raw.split(",") if m.strip()]


def _config_language(language: str, project_markers: list[str]) -> str:
    if project_markers:
        return f"{language}|markers={','.join(project_markers)}"
    return language


def _legacy_lsp_session_config(params: dict[str, object]) -> LspSessionConfig:
    root = _str_param(params, "root")
    chash = _str_param(params, "config_hash")
    try:
        chain = chain_from_wire(params.get("chain"))
    except ValueError as e:
        raise BrokerError("invalid_params", str(e)) from None
    label = _optional_str_param(params, "server_label")
    return LspSessionConfig(
        root=root,
        config_hash=chash,
        chain=chain,
        server_label=label,
        prefer=_prefer_param(params),
        project_markers=_str_list_param(params, "project_markers"),
        language=_optional_str_param(params, "language"),
        route_id=_optional_str_param(params, "route_id"),
        route_reason="client-supplied chain",
    )


def _router_lsp_session_config(params: dict[str, object]) -> LspSessionConfig:
    requested_root = os.path.abspath(_str_param(params, "root"))
    probe_path = os.path.abspath(_route_probe_path(params, requested_root))
    override = _optional_str_param(params, "route").strip().lower()
    route_id = override or resolve_route_id_for_path(probe_path) or resolve_route_id_for_path(requested_root)
    if not route_id:
        known = ", ".join(sorted(BUILTIN_ROUTES))
        raise BrokerError(
            "invalid_params",
            "HSP broker router could not select a language route. "
            f"Set HSP_ROUTE to one of: {known}; or set LSP_SERVERS explicitly.",
        )
    route = get_route(route_id)
    if route is None:
        known = ", ".join(sorted(BUILTIN_ROUTES))
        raise BrokerError("invalid_params", f"Unknown HSP_ROUTE {route_id!r}. Known: {known}")

    def env(name: str, default: str = "") -> str:
        return _route_env(route, name, default)

    try:
        chain = parse_lsp_chain(env)
    except ValueError as e:
        raise BrokerError("invalid_params", str(e)) from None
    project_markers = _route_markers(route)
    root = find_project_root(probe_path, project_markers) or requested_root
    language = _route_env(route, "LSP_LANGUAGE", route.language)
    reason = f"{probe_path} -> {route_id}"
    return LspSessionConfig(
        root=os.path.abspath(root),
        config_hash=chain_config_hash(_config_language(language, project_markers), chain),
        chain=chain,
        server_label=chain[0].label if chain else "",
        prefer=parse_lsp_prefer(env, chain),
        project_markers=project_markers,
        language=language,
        route_id=route_id,
        route_reason=reason,
    )


def _lsp_session_config(params: dict[str, object]) -> LspSessionConfig:
    if _bool_param(params, "router"):
        return _router_lsp_session_config(params)
    return _legacy_lsp_session_config(params)


def _str_param(params: dict[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise BrokerError("invalid_params", f"missing or non-string param: {name}")
    return value


def _str_list_param(params: dict[str, object], name: str) -> list[str]:
    value = params.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BrokerError("invalid_params", f"{name} must be a list of strings")
    return list(cast(list[str], value))


def _rename_list_param(params: dict[str, object], name: str) -> list[tuple[str, str]]:
    value = params.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise BrokerError("invalid_params", f"{name} must be a list of [old, new] pairs")
    result: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise BrokerError("invalid_params", f"{name} must be a list of [old, new] pairs")
        result.append((item[0], item[1]))
    return result


def _prefer_param(params: dict[str, object]) -> dict[str, int]:
    value = params.get("prefer", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BrokerError("invalid_params", "prefer must be an object")
    result: dict[str, int] = {}
    for method, idx in value.items():
        if not isinstance(method, str) or not isinstance(idx, int):
            raise BrokerError("invalid_params", "prefer must map methods to integer indices")
        result[method] = idx
    return result


def _idle_ttl_seconds() -> float:
    raw = os.environ.get(IDLE_TTL_ENV, str(DEFAULT_IDLE_TTL_SECONDS)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(DEFAULT_IDLE_TTL_SECONDS)


def _env_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _devtools_port() -> int:
    raw = os.environ.get(DEVTOOLS_PORT_ENV, "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _maybe_start_devtools(daemon: BrokerDaemon):
    """Expose the live broker over python-devtools when explicitly requested.

    This intentionally stays opt-in and import-optional. Production broker
    sessions should not grow a runtime-inspection surface unless the caller sets
    ``LSP_DEVTOOLS=1``. When enabled, agents can attach through the
    ``python-devtools`` MCP bridge using the stable app id
    ``hsp-broker`` and inspect ``broker``, ``bus``, ``registry``, and
    ``lsp``.
    """
    if not _env_enabled(DEVTOOLS_ENV):
        return None
    try:
        devtools = importlib.import_module("python_devtools")
    except Exception as e:
        log.warning("LSP_DEVTOOLS requested but python_devtools import failed: %r", e)
        return None

    app_id = os.environ.get(DEVTOOLS_APP_ID_ENV, "hsp-broker")
    host = os.environ.get(DEVTOOLS_HOST_ENV, "localhost")
    readonly = _env_enabled(DEVTOOLS_READONLY_ENV, default=True)
    devtools.register("broker", daemon)
    devtools.register("bus", daemon.bus)
    devtools.register("registry", daemon.registry)
    devtools.register("lsp", daemon.lsp)
    devtools.start(
        host=host,
        port=_devtools_port(),
        app_id=app_id,
        readonly=readonly,
    )
    log.info(
        "broker devtools enabled: app_id=%s readonly=%s running=%s",
        app_id,
        readonly,
        devtools.running,
    )
    return devtools


def _devtools_status(devtools: object | None) -> dict[str, object]:
    if devtools is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "running": bool(getattr(devtools, "running", False)),
        "readonly": bool(getattr(devtools, "readonly", False)),
        "app_id": getattr(devtools, "app_id", None),
        "n_clients": getattr(devtools, "n_clients", 0),
        "n_commands": getattr(devtools, "n_commands", 0),
        "last_command_time": getattr(devtools, "last_command_time", 0.0),
    }


# --- Socket server -----------------------------------------------------------


async def serve_unix(
    path: Path,
    daemon: BrokerDaemon | None = None,
    *,
    ready: asyncio.Event | None = None,
) -> BrokerDaemon:
    """Run the broker on a Unix-domain socket at `path` until shutdown.

    A stale socket file at `path` is unlinked first — the broker is
    designed as a per-user singleton, so colliding paths usually mean a
    crashed previous run.  Concurrent-broker collision detection is a
    later concern (see `docs/broker.md`).

    `ready` is set once the listener is bound, so callers (tests) can
    wait for the broker to be reachable before connecting.
    """
    if daemon is None:
        daemon = BrokerDaemon()

    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)

    server = await asyncio.start_unix_server(
        lambda r, w: _connection_handler(daemon, r, w),
        path=str(path),
    )

    if ready is not None:
        ready.set()
    log.info("hsp-broker listening on %s", path)
    if _env_enabled(BABEL_BRIDGE_ENV):
        daemon.start_babel_bridge()

    try:
        async with server:
            shutdown_task = asyncio.create_task(daemon.shutdown_event.wait())
            serve_task = asyncio.create_task(server.serve_forever())
            _, pending = await asyncio.wait(
                {shutdown_task, serve_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        daemon.stop_babel_bridge()
        task = daemon.babel_bridge_task
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            path.unlink()
        except OSError:
            pass
    return daemon


async def _connection_handler(
    daemon: BrokerDaemon,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """One connection = many JSONL frames; loop until peer closes."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = decode_message(line)
            except BrokerError as e:
                resp: dict[str, object] = _error_response(None, e.code, str(e))
            else:
                resp = await daemon.handle_request(req)
            writer.write(encode_message(resp))
            try:
                await writer.drain()
            except ConnectionResetError:
                break
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# --- Entry point -------------------------------------------------------------


def _install_signal_handlers(daemon: BrokerDaemon) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.shutdown_event.set)
        except (NotImplementedError, RuntimeError):
            pass


async def _main_async(path: Path) -> None:
    daemon = BrokerDaemon()
    _install_signal_handlers(daemon)
    await serve_unix(path, daemon)


def main() -> None:
    """Entry point for `python -m hsp.broker`."""
    log_file = broker_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main_async(socket_path()))


if __name__ == "__main__":
    main()


__all__ = [
    "BrokerDaemon",
    "BrokerError",
    "DEFAULT_SOCKET_NAME",
    "DEFAULT_IDLE_TTL_SECONDS",
    "DEVTOOLS_APP_ID_ENV",
    "DEVTOOLS_ENV",
    "DEVTOOLS_HOST_ENV",
    "DEVTOOLS_PORT_ENV",
    "DEVTOOLS_READONLY_ENV",
    "IDLE_TTL_ENV",
    "LOG_ENV_OVERRIDE",
    "SOCKET_ENV_OVERRIDE",
    "broker_log_path",
    "decode_message",
    "encode_message",
    "main",
    "serve_unix",
    "socket_path",
]
