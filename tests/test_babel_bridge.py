from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hsp.agent_bus import AgentBus
from hsp.babel_bridge import bus_params_from_babel_frame, subscribe_babel_events
from hsp.broker import BABEL_BRIDGE_ENV, BrokerDaemon, serve_unix


class BabelBridgeAdapterTests(unittest.TestCase):
    def test_session_state_changed_maps_to_workgroup_heartbeat(self) -> None:
        frame: dict[str, object] = {
            "status": "event",
            "event": {
                "timestamp": "2026-05-06T12:00:00Z",
                "seq": 7,
                "event": "session_state_changed",
                "addr": {"socket": "unix:/run/user/1000/kitty.sock", "id": 42},
                "session_id": "sess-1",
                "workspace": 3,
                "old_state": "thinking",
                "new_state": "awaiting_input",
                "agent_kind": "claude",
            },
        }

        params = bus_params_from_babel_frame(frame)

        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params["event_type"], "agent.heartbeat")
        self.assertEqual(params["agent_id"], "claude:sess-1")
        self.assertEqual(params["session_id"], "sess-1")
        metadata = cast(dict[str, object], params["metadata"])
        self.assertEqual(metadata["source"], "babel")
        self.assertEqual(metadata["native_event"], "session_state_changed")

    def test_tool_started_maps_to_tool_before(self) -> None:
        frame: dict[str, object] = {
            "status": "event",
            "event": {
                "event": "tool_started",
                "session_id": "sess-1",
                "tool_name": "Edit",
                "agent_kind": "claude",
            },
        }

        params = bus_params_from_babel_frame(frame)

        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params["event_type"], "tool.before")
        self.assertEqual(params["message"], "Babel tool_started Edit")


class BabelBridgeSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_babel_events_appends_frames_to_bus(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            socket_path = Path(root) / "babel.sock"
            server = await asyncio.start_unix_server(
                self._fake_babel_connection,
                path=str(socket_path),
            )
            bus = AgentBus()
            task = asyncio.create_task(
                subscribe_babel_events(bus, socket_path=socket_path, reconnect_delay=0.01)
            )
            try:
                await asyncio.sleep(0.05)
                presence = bus.presence({"workspace_root": os.getcwd()})
            finally:
                task.cancel()
                server.close()
                await server.wait_closed()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        agents = cast(list[dict[str, Any]], presence["agents"])
        self.assertEqual(agents[0]["agent_id"], "codex:sess-2")

    async def _fake_babel_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readline()
        writer.write(json.dumps({"status": "subscribed", "subscriber_id": 1}).encode() + b"\n")
        writer.write(
            json.dumps({
                "status": "event",
                "event": {
                    "event": "session_started",
                    "session_id": "sess-2",
                    "agent_kind": "codex",
                    "cwd": os.getcwd(),
                },
            }).encode()
            + b"\n"
        )
        try:
            await writer.drain()
        except ConnectionResetError:
            return
        writer.close()
        await writer.wait_closed()


class BrokerBabelBridgeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_serve_unix_starts_bridge_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            daemon = BrokerDaemon()
            ready = asyncio.Event()
            with patch.dict(os.environ, {BABEL_BRIDGE_ENV: "1"}, clear=False):
                task = asyncio.create_task(
                    serve_unix(Path(root) / "hsp.sock", daemon, ready=ready)
                )
                await ready.wait()
                await asyncio.sleep(0)
                status = daemon._status()
                daemon.shutdown_event.set()
                await task

        bridge = cast(dict[str, object], status["babel_bridge"])
        self.assertTrue(bridge["enabled"])
        self.assertTrue(bridge["running"])


if __name__ == "__main__":
    unittest.main()
