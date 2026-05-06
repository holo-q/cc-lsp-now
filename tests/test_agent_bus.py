from __future__ import annotations

import asyncio
import unittest
from typing import cast
from unittest.mock import patch

from hsp import server
from hsp.agent_bus import AgentBus
from hsp.broker import BrokerDaemon


class AgentBusPureTests(unittest.TestCase):
    def test_note_records_workspace_scoped_event(self) -> None:
        bus = AgentBus()

        result = bus.note({
            "workspace_root": "/repo",
            "message": "touching broker bus",
            "files": ["src/hsp/broker.py"],
            "agent_id": "agent-a",
        })

        event = cast(dict[str, object], result["event"])
        self.assertEqual(event["event_id"], "E1")
        self.assertEqual(event["event_type"], "note.posted")
        self.assertEqual(event["workspace_root"], "/repo")
        self.assertEqual(event["files"], ["src/hsp/broker.py"])

    def test_question_collects_related_events_and_settles(self) -> None:
        bus = AgentBus()
        question_result = bus.ask({
            "workspace_root": "/repo",
            "message": "anyone touching server?",
            "files": ["src/hsp/server.py"],
            "timeout": 0,
        })
        question = cast(dict[str, object], question_result["question"])
        qid = cast(str, question["question_id"])

        bus.event({
            "workspace_root": "/repo",
            "event_type": "file.touched",
            "files": ["src/hsp/server.py"],
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

    def test_heartbeat_registers_presence_without_recent_event_noise(self) -> None:
        bus = AgentBus()

        bus.heartbeat({
            "workspace_root": "/repo",
            "agent_id": "agent-tool",
            "client_id": "client-1",
        })

        presence = bus.presence({"workspace_root": "/repo"})
        agents = cast(list[dict[str, object]], presence["agents"])
        recent = bus.recent({"workspace_root": "/repo"})
        self.assertEqual(agents[0]["agent_id"], "agent-tool")
        self.assertEqual(recent["events"], [])

    def test_session_stop_goes_asleep_immediately(self) -> None:
        bus = AgentBus()

        bus.event({
            "workspace_root": "/repo",
            "agent_id": "agent-done",
            "event_type": "session.stop",
        })

        presence = bus.presence({"workspace_root": "/repo"})
        agents = cast(list[dict[str, object]], presence["agents"])
        self.assertEqual(agents[0]["state"], "asleep")

    def test_ticket_join_and_release_records_compact_lifecycle(self) -> None:
        bus = AgentBus()

        first = bus.ticket({
            "workspace_root": "/repo",
            "agent_id": "agent-a",
            "message": "wire team tickets",
        })
        second = bus.ticket({
            "workspace_root": "/repo",
            "agent_id": "agent-b",
            "message": "wire team tickets",
        })

        ticket = cast(dict[str, object], second["ticket"])
        holders = cast(list[dict[str, object]], ticket["holders"])
        self.assertEqual(cast(dict[str, object], first["ticket"])["ticket_id"], "T1")
        self.assertEqual(ticket["ticket_id"], "T1")
        self.assertEqual({holder["agent_id"] for holder in holders}, {"agent-a", "agent-b"})

        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": ""})
        release = bus.ticket({"workspace_root": "/repo", "agent_id": "agent-b", "message": ""})
        events = cast(list[dict[str, object]], bus.journal({"workspace_root": "/repo"})["events"])

        self.assertEqual(cast(list[object], release["active_tickets"]), [])
        self.assertIn("ticket.started", {event["event_type"] for event in events})
        self.assertIn("ticket.joined", {event["event_type"] for event in events})
        self.assertIn("ticket.released", {event["event_type"] for event in events})
        self.assertIn("ticket.closed", {event["event_type"] for event in events})

    def test_build_gate_unlocks_when_every_ticket_holder_is_waiting(self) -> None:
        bus = AgentBus()
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": "edit server"})
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-b", "message": "edit server"})

        cold = bus.build_gate({"workspace_root": "/repo"})
        one_waiting = bus.build_gate({"workspace_root": "/repo", "agent_id": "agent-a"})
        all_waiting = bus.build_gate({"workspace_root": "/repo", "agent_id": "agent-b"})

        self.assertFalse(cold["unlocked"])
        self.assertEqual(cold["reason"], "active_tickets")
        self.assertFalse(one_waiting["unlocked"])
        self.assertTrue(all_waiting["unlocked"])
        self.assertEqual(all_waiting["reason"], "all_waiting")

    def test_new_ticket_clears_stale_build_wait_state_for_agent(self) -> None:
        bus = AgentBus()
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": "old ticket"})
        self.assertTrue(bus.build_gate({"workspace_root": "/repo", "agent_id": "agent-a"})["unlocked"])

        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": "new ticket"})
        gate = bus.build_gate({"workspace_root": "/repo"})

        self.assertFalse(gate["unlocked"])
        self.assertEqual(gate["waiting"], [])

    def test_reposting_same_ticket_is_idempotent(self) -> None:
        bus = AgentBus()

        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": "same ticket"})
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": "same ticket"})
        journal = bus.journal({"workspace_root": "/repo"})
        events = cast(list[dict[str, object]], journal["events"])

        self.assertEqual([event["event_type"] for event in events], ["ticket.started"])

    def test_build_gate_waiting_rows_only_show_current_holders(self) -> None:
        bus = AgentBus()
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-a", "message": "held"})
        bus.build_gate({"workspace_root": "/repo", "agent_id": "observer"})

        gate = bus.build_gate({"workspace_root": "/repo"})

        self.assertEqual(gate["waiting"], [])

    def test_edit_gate_workgroup_mode_requires_any_active_ticket(self) -> None:
        bus = AgentBus()

        denied = bus.edit_gate({"workspace_root": "/repo", "agent_id": "agent-a"})
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-b", "message": "editing"})
        allowed = bus.edit_gate({"workspace_root": "/repo", "agent_id": "agent-a"})

        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "missing_ticket")
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["reason"], "ticket_active")

    def test_edit_gate_agent_mode_requires_current_agent_ticket(self) -> None:
        bus = AgentBus()
        bus.ticket({"workspace_root": "/repo", "agent_id": "agent-b", "message": "editing"})

        denied = bus.edit_gate({"workspace_root": "/repo", "agent_id": "agent-a", "mode": "agent"})
        allowed = bus.edit_gate({"workspace_root": "/repo", "agent_id": "agent-b", "mode": "agent"})

        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "missing_ticket")
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["reason"], "ticket_held")

    def test_chat_reply_closes_question_and_surfaces_in_journal(self) -> None:
        bus = AgentBus()
        opened = bus.ask({
            "workspace_root": "/repo",
            "agent_id": "agent-a",
            "message": "build now?",
            "timeout": "30s",
        })
        qid = cast(str, cast(dict[str, object], opened["question"])["question_id"])

        result = bus.chat({
            "workspace_root": "/repo",
            "agent_id": "agent-b",
            "id": qid,
            "message": "all holders waiting",
        })
        question = cast(dict[str, object], result["question"])
        journal = cast(dict[str, object], result["journal"])
        events = cast(list[dict[str, object]], journal["events"])

        self.assertIsNotNone(question["closed_at"])
        self.assertEqual(cast(dict[str, object], result["event"])["event_type"], "bus.reply")
        self.assertIn("bus.reply", {event["event_type"] for event in events})


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
            self.assertEqual(params["files"], ["src/hsp/server.py"])
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
                        files="src/hsp/server.py",
                    ))

        self.assertIn("logged E7 note.posted coordinating", text)


if __name__ == "__main__":
    unittest.main()
