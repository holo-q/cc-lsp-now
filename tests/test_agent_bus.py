from __future__ import annotations

import asyncio
import unittest
from typing import cast
from unittest.mock import patch

from cc_lsp_now import server
from cc_lsp_now.agent_bus import AgentBus
from cc_lsp_now.broker import BrokerDaemon


class AgentBusPureTests(unittest.TestCase):
    def test_note_records_workspace_scoped_event(self) -> None:
        bus = AgentBus()

        result = bus.note({
            "workspace_root": "/repo",
            "message": "touching broker bus",
            "files": ["src/cc_lsp_now/broker.py"],
            "agent_id": "agent-a",
        })

        event = cast(dict[str, object], result["event"])
        self.assertEqual(event["event_id"], "E1")
        self.assertEqual(event["event_type"], "note.posted")
        self.assertEqual(event["workspace_root"], "/repo")
        self.assertEqual(event["files"], ["src/cc_lsp_now/broker.py"])

    def test_question_collects_related_events_and_settles(self) -> None:
        bus = AgentBus()
        question_result = bus.ask({
            "workspace_root": "/repo",
            "message": "anyone touching server?",
            "files": ["src/cc_lsp_now/server.py"],
            "timeout": 0,
        })
        question = cast(dict[str, object], question_result["question"])
        qid = cast(str, question["question_id"])

        bus.event({
            "workspace_root": "/repo",
            "event_type": "file.touched",
            "files": ["src/cc_lsp_now/server.py"],
            "message": "added lsp_log",
        })
        bus.reply({"workspace_root": "/repo", "id": qid, "message": "same file, coordinating"})

        settled = bus.settle({"workspace_root": "/repo"})

        closed = cast(list[dict[str, object]], settled["closed"])
        self.assertEqual(len(closed), 1)
        digest = closed[0]
        events = cast(list[dict[str, object]], digest["events"])
        self.assertIn("file.touched", {event["event_type"] for event in events})
        self.assertIn("bus.reply", {event["event_type"] for event in events})

    def test_recent_filters_by_scope(self) -> None:
        bus = AgentBus()
        bus.note({"workspace_root": "/repo", "message": "a", "files": ["a.py"]})
        bus.note({"workspace_root": "/repo", "message": "b", "files": ["b.py"]})

        recent = bus.recent({"workspace_root": "/repo", "files": ["b.py"]})

        events = cast(list[dict[str, object]], recent["events"])
        self.assertEqual([event["message"] for event in events], ["b"])


class BrokerBusWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_exposes_bus_methods(self) -> None:
        daemon = BrokerDaemon()

        note = await daemon.handle_request({
            "id": "1",
            "method": "bus.note",
            "params": {"workspace_root": "/repo", "message": "hello"},
        })
        weather = await daemon.handle_request({
            "id": "2",
            "method": "bus.weather",
            "params": {"workspace_root": "/repo"},
        })

        self.assertIn("result", note)
        weather_result = cast(dict[str, object], weather["result"])
        self.assertEqual(len(cast(list[object], weather_result["recent"])), 1)


class ServerLspLogTests(unittest.TestCase):
    def test_lsp_log_routes_to_broker_bus(self) -> None:
        async def fake_bus_call(method: str, params: dict[str, object]) -> object:
            self.assertEqual(method, "bus.note")
            self.assertEqual(params["files"], ["src/cc_lsp_now/server.py"])
            return {
                "event": {
                    "event_id": 7,
                    "event_type": "note.posted",
                    "message": params["message"],
                    "files": params["files"],
                    "symbols": [],
                    "aliases": [],
                }
            }

        with patch.object(server, "_broker_enabled", return_value=True):
            with patch.object(server, "_broker_base_params", return_value={}):
                with patch.object(server, "_broker_bus_call", side_effect=fake_bus_call):
                    text = asyncio.run(server.lsp_log(
                        action="note",
                        message="coordinating",
                        files="src/cc_lsp_now/server.py",
                    ))

        self.assertIn("logged E7 note.posted coordinating", text)


if __name__ == "__main__":
    unittest.main()
