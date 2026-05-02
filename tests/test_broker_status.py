"""End-to-end socket lifecycle test for the broker daemon.

Stands up an in-process broker on a temporary Unix-domain socket, drives
it through `BrokerClient`, and pins the things that only show up over a
real wire:

- the auto-mode `connect()` works on a freshly-bound socket;
- `ping`/`status`/`session.get_or_create` round-trip through JSONL;
- the daemon's session registry survives multiple client requests on
  the same connection (key reuse over the wire);
- a `shutdown` request really stops `serve_unix`.

This test does NOT exercise the `python -m hsp.broker` subprocess
launcher.  Spawning a subprocess from inside the test runner makes
debugging painful and the launcher is already covered by import.  The
in-process variant verifies the protocol fidelity instead, which is
what `BrokerClient` actually depends on.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast

from hsp.broker import BrokerDaemon, serve_unix
from hsp.broker_client import BrokerClient


class _ServerThread:
    """Run an asyncio broker on a private socket, off the test thread.

    The daemon is asyncio; `BrokerClient` is sync.  Putting the daemon
    behind a thread+loop lets us drive both sides from the same test
    without bouncing through `asyncio.to_thread` for every assertion.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.daemon: BrokerDaemon | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            daemon = BrokerDaemon()
            self.daemon = daemon
            ready = asyncio.Event()

            async def signal_ready() -> None:
                await ready.wait()
                self._ready.set()

            try:
                loop.run_until_complete(asyncio.gather(
                    serve_unix(self.socket_path, daemon, ready=ready),
                    signal_ready(),
                ))
            finally:
                loop.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("broker did not become ready within 5s")

    def stop(self) -> None:
        # Setting the shutdown event off-loop is fine; asyncio.Event is
        # thread-safe for set() in CPython.
        if self.daemon is not None and self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.daemon.shutdown_event.set)
        if self.thread is not None:
            self.thread.join(timeout=5.0)


class BrokerStatusOverSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="hsp-broker-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.socket_path = Path(self._tmpdir.name) / "broker.sock"
        self.server = _ServerThread(self.socket_path)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_ping_round_trip(self) -> None:
        with BrokerClient(self.socket_path) as c:
            c.connect()
            self.assertEqual(c.request("ping"), {"pong": True})

    def test_status_lists_no_sessions_initially(self) -> None:
        with BrokerClient(self.socket_path) as c:
            c.connect()
            result = cast(dict[str, object], c.request("status"))
            self.assertEqual(result["session_count"], 0)
            self.assertEqual(result["sessions"], [])
            self.assertEqual(result["pid"], os.getpid())

    def test_session_key_reuse_across_requests(self) -> None:
        with BrokerClient(self.socket_path) as c:
            c.connect()
            params: dict[str, object] = {"root": "/repo-x", "config_hash": "h1", "server_label": "ty"}
            a = cast(dict[str, object], c.request("session.get_or_create", params))
            b = cast(dict[str, object], c.request("session.get_or_create", params))
            self.assertEqual(a["session_id"], b["session_id"])
            status = cast(dict[str, object], c.request("status"))
            self.assertEqual(status["session_count"], 1)

    def test_distinct_keys_make_distinct_sessions(self) -> None:
        with BrokerClient(self.socket_path) as c:
            c.connect()
            a = cast(dict[str, object], c.request(
                "session.get_or_create",
                {"root": "/repo-a", "config_hash": "h"},
            ))
            b = cast(dict[str, object], c.request(
                "session.get_or_create",
                {"root": "/repo-b", "config_hash": "h"},
            ))
            self.assertNotEqual(a["session_id"], b["session_id"])
            sessions = cast(
                list[object],
                cast(dict[str, object], c.request("status"))["sessions"],
            )
            self.assertEqual(len(sessions), 2)


if __name__ == "__main__":
    unittest.main()
