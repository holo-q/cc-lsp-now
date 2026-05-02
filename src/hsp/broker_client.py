"""Synchronous client for the hsp-broker JSONL daemon.

Stays sync on purpose: the MCP server already runs an asyncio loop, but
broker calls happen at well-defined seams (status checks, session
borrow/return, LSP-method forwarding) where blocking briefly is
fine.  Avoiding `async` here keeps the client usable from tests, CLI
helpers, and any other tool that doesn't want to spin up a loop just to
ping the broker.

Auto mode: `connect_or_start()` first tries to dial the existing socket
(`broker.socket_path()`).  If nobody is home — no socket file, or the
file is stale and `connect()` fails — it spawns a detached
`python -m hsp.broker` and waits for the listener to come up.
This is the path the MCP server uses in broker-first mode.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from hsp.broker import (
    BrokerError,
    broker_log_path,
    decode_message,
    encode_message,
    socket_path,
)


CONNECT_TIMEOUT = 2.0
START_TIMEOUT = 10.0
START_POLL_INTERVAL = 0.05


class BrokerClient:
    """Sync JSONL client over a Unix-domain socket.

    Keeps a single open connection; callers can pipeline multiple
    `request(...)` calls on the same instance.  Each request expects a
    single line of response, matching the daemon's
    `_connection_handler` which writes one JSON object per request.

    Not thread-safe.  Tests and other callers should hold one client per
    thread.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = Path(path) if path is not None else socket_path()
        self._sock: socket.socket | None = None
        self._reader_buf: bytes = b""

    # --- lifecycle ------------------------------------------------------

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        """Connect to an already-running broker.

        Raises the underlying `OSError` (incl. `FileNotFoundError`,
        `ConnectionRefusedError`) when no broker is reachable — callers
        that want auto-start should use `connect_or_start`.
        """
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(self.path))
        except OSError:
            s.close()
            raise
        # After connect, drop the connect-phase timeout to None so
        # individual reads/writes can use per-call timeouts.
        s.settimeout(None)
        self._sock = s

    def connect_or_start(
        self,
        connect_timeout: float = CONNECT_TIMEOUT,
        start_timeout: float = START_TIMEOUT,
    ) -> bool:
        """Auto-mode connect: dial the socket, spawning a broker if needed.

        Returns True if a fresh broker was started, False if the existing
        one accepted the connection.  Either way, on success the client
        is connected.
        """
        try:
            self.connect(timeout=connect_timeout)
            return False
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            pass
        start_broker_subprocess()
        deadline = time.monotonic() + start_timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.connect(timeout=connect_timeout)
                return True
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                last_exc = e
                time.sleep(START_POLL_INTERVAL)
        raise BrokerError(
            "broker_unreachable",
            f"failed to start broker at {self.path}: {last_exc!r}",
        )

    def close(self) -> None:
        s = self._sock
        self._sock = None
        if s is None:
            return
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    def __enter__(self) -> BrokerClient:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    # --- request/response ----------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        request_id: str | None = None,
    ) -> object:
        """Send one request, return the `result` payload.

        Raises `BrokerError` carrying the daemon's error `code` and
        `message` if the response is an error frame.  Bare connection
        problems surface as `BrokerError("transport", ...)`.
        """
        if self._sock is None:
            raise BrokerError("not_connected", "client not connected")
        rid = request_id if request_id is not None else _next_id()
        msg: dict[str, object] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        frame = encode_message(msg)
        try:
            self._sock.sendall(frame)
        except OSError as e:
            raise BrokerError("transport", f"send failed: {e!r}") from None

        line = self._read_line()
        resp = decode_message(line)
        if "error" in resp:
            err = resp["error"]
            if isinstance(err, dict):
                err_d = cast(dict[str, object], err)
                code_obj = err_d.get("code", "unknown")
                msg_obj = err_d.get("message", "")
                code = code_obj if isinstance(code_obj, str) else "unknown"
                message = msg_obj if isinstance(msg_obj, str) else json.dumps(msg_obj)
            else:
                code = "unknown"
                message = json.dumps(err)
            raise BrokerError(code, message)
        return resp.get("result")

    def _read_line(self) -> bytes:
        """Pull one newline-terminated frame from the socket buffer."""
        assert self._sock is not None
        while b"\n" not in self._reader_buf:
            try:
                chunk = self._sock.recv(4096)
            except OSError as e:
                raise BrokerError("transport", f"recv failed: {e!r}") from None
            if not chunk:
                raise BrokerError("transport", "broker closed connection")
            self._reader_buf += chunk
        idx = self._reader_buf.index(b"\n")
        line = self._reader_buf[: idx + 1]
        self._reader_buf = self._reader_buf[idx + 1 :]
        return line


# --- Subprocess launcher -----------------------------------------------------


def start_broker_subprocess() -> subprocess.Popen[bytes]:
    """Spawn a detached `python -m hsp.broker` process.

    Detached so it survives the calling MCP session.  stdout/stderr are
    appended to the same broker log as structured Python logging, so
    startup crashes and runtime traces land in one place.
    """
    log_file = broker_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_file.open("ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "hsp.broker"],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    finally:
        log_handle.close()


# --- Helpers ----------------------------------------------------------------

_id_counter: int = 0


def _next_id() -> str:
    global _id_counter
    _id_counter += 1
    return f"c{_id_counter}"


__all__ = [
    "BrokerClient",
    "CONNECT_TIMEOUT",
    "START_TIMEOUT",
    "start_broker_subprocess",
]
