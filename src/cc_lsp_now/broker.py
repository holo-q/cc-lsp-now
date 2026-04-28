"""cc-lsp-broker daemon skeleton.

This is the v1 of the broker described in `docs/broker.md`: a user-level
Unix-domain socket JSONL daemon that supervises shared workspace sessions
for `cc-lsp-now`.  This first slice intentionally does *not* forward LSP
methods.  It establishes the parts that have to exist before forwarding
makes sense:

- a stable socket path derivation (so any number of clients agree on
  where to find the broker without environment plumbing);
- a small JSON-line request/response protocol (one request per line, one
  response per line);
- a workspace session registry keyed by `(root, config_hash)` so two
  clients asking for the same workspace get the same session record;
- clean start/stop helpers that the client side can drive (`stop` / a
  graceful `shutdown` request).

The MCP server (`server.py`) is *not* edited in this slice.  Direct mode
keeps spawning its own LSP chain.  Once the broker stabilises, the MCP
server will gain a `try-broker-first, fall back to direct` path; today
the broker exists as a parallel skeleton agents can dial in for
`status` / `session.get_or_create` / `ping` / `shutdown` and nothing else.

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
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from cc_lsp_now.broker_session import (
    SessionKey,
    SessionRegistry,
    session_to_dict,
)

log = logging.getLogger(__name__)


# --- Socket path -------------------------------------------------------------

DEFAULT_SOCKET_NAME = "cc-lsp-broker.sock"
SOCKET_ENV_OVERRIDE = "CC_LSP_BROKER_SOCKET"


def socket_path() -> Path:
    """Return the user-scoped Unix-domain socket path for the broker.

    Resolution order (first match wins):

    1. `$CC_LSP_BROKER_SOCKET` — explicit override, used in tests and by
       users who run an isolated broker for a single project.
    2. `$XDG_RUNTIME_DIR/cc-lsp-broker.sock` — the canonical location on
       systemd-managed systems; the directory is already user-private and
       cleaned on logout.
    3. `/tmp/cc-lsp-broker-<user>/cc-lsp-broker.sock` — fallback for shells
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
    base = Path(f"/tmp/cc-lsp-broker-{user}")
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
        self.started_at: float = time.time()
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
            result = await self._dispatch(method, params)
        except BrokerError as e:
            return _error_response(rid, e.code, str(e))
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
            return {"stopped": self.registry.stop(sid)}
        if method == "shutdown":
            self._shutdown.set()
            return {"shutting_down": True}
        raise BrokerError("unknown_method", f"unknown method: {method}")

    def _status(self) -> dict[str, object]:
        now = time.time()
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime": now - self.started_at,
            "session_count": len(self.registry),
            "sessions": [session_to_dict(s) for s in self.registry.all_sessions()],
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


def _error_response(
    rid: object, code: str, message: str
) -> dict[str, object]:
    return {"id": rid, "error": {"code": code, "message": message}}


def _str_param(params: dict[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise BrokerError("invalid_params", f"missing or non-string param: {name}")
    return value


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
    log.info("cc-lsp-broker listening on %s", path)

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
    """Entry point for `python -m cc_lsp_now.broker`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main_async(socket_path()))


if __name__ == "__main__":
    main()


__all__ = [
    "BrokerDaemon",
    "BrokerError",
    "DEFAULT_SOCKET_NAME",
    "SOCKET_ENV_OVERRIDE",
    "decode_message",
    "encode_message",
    "main",
    "serve_unix",
    "socket_path",
]
