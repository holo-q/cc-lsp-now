"""Wave 1 broker integration tests for the agent bus.

These tests drive ``BrokerDaemon.handle_request`` directly — no sockets,
no LSP processes — so they pin the wire shape of the bus methods that
the broker exposes to MCP clients.  They intentionally complement
``tests/test_agent_bus.py`` (which exercises ``AgentBus`` in process)
and ``tests/test_agent_bus_contract.py`` (Wave 1 cross-cutting contract).

Coverage focus per the broker brief:

- bus.append/event ⇄ bus.recent round-trip;
- bus.ask/reply/settle/weather lifecycle through ``handle_request``;
- workspace scoping across two roots (recent and weather stay separated);
- same root with two different LSP config_hashes shares the bus
  (bus is workspace-scoped, not chain-scoped);
- ``status`` and ``lsp.status`` carry a compact bus summary;
- malformed params surface structured ``invalid_params`` errors.

Bus events are persisted to ``<workspace_root>/tmp/hsp-bus.jsonl``
by the current ``AgentBus`` core, so each test plants its workspace_root
inside a ``TemporaryDirectory`` to avoid leaking JSONL into the runner's
filesystem.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from typing import Any, cast

from hsp.broker import BrokerDaemon


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _result(response: dict[str, object]) -> dict[str, Any]:
    """Pull the ``result`` payload off a response, asserting no error."""
    if "error" in response:
        raise AssertionError(f"unexpected broker error: {response['error']!r}")
    return cast(dict[str, Any], response["result"])


def _error(response: dict[str, object]) -> dict[str, Any]:
    if "error" not in response:
        raise AssertionError(f"expected error response, got {response!r}")
    return cast(dict[str, Any], response["error"])


class BrokerBusAppendRecentTests(unittest.TestCase):
    """``bus.append`` (alias for ``bus.event``) and ``bus.recent``.

    The broker brief lists ``bus.append`` as the workspace event verb;
    the bus core dispatches both ``bus.append`` and ``bus.event`` to the
    same handler so docs/agent-bus.md and the broker brief stay aligned.
    """

    def test_bus_append_alias_dispatches_to_event_handler(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            response = _run(daemon.handle_request({
                "id": "1",
                "method": "bus.append",
                "params": {
                    "workspace_root": root,
                    "event_type": "task.intent",
                    "agent_id": "noesis",
                    "message": "splitting lsp_refs",
                    "files": ["src/server.py"],
                },
            }))
            result = _result(response)
            event = cast(dict[str, Any], result["event"])
            # The event lands as task.intent — bus.append is a verb alias,
            # not a separate event_type override.
            self.assertEqual(event["event_type"], "task.intent")
            self.assertEqual(event["workspace_root"], root)

    def test_recent_returns_appended_events_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            for i in range(3):
                _run(daemon.handle_request({
                    "id": f"a{i}",
                    "method": "bus.append",
                    "params": {
                        "workspace_root": root,
                        "event_type": "file.touched",
                        "message": f"edit {i}",
                        "files": [f"src/file_{i}.py"],
                    },
                }))
            response = _run(daemon.handle_request({
                "id": "r",
                "method": "bus.recent",
                "params": {"workspace_root": root, "limit": 10},
            }))
            result = _result(response)
            events = cast(list[dict[str, Any]], result["events"])
            messages = [cast(str, e["message"]) for e in events]
            self.assertEqual(messages, ["edit 0", "edit 1", "edit 2"])


class BrokerBusLifecycleTests(unittest.TestCase):
    """ask -> reply -> settle -> weather threading through the broker."""

    def test_ask_reply_settle_emits_close_digest(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            opened = _run(daemon.handle_request({
                "id": "1",
                "method": "bus.ask",
                "params": {
                    "workspace_root": root,
                    "agent_id": "noesis",
                    "message": "anyone touching server.py?",
                    "files": ["src/server.py"],
                    # Zero-second timeout so settle closes immediately —
                    # we are testing the digest emission, not the wait.
                    "timeout": 0,
                },
            }))
            qid = cast(str, cast(dict[str, Any], _result(opened)["question"])["question_id"])
            self.assertTrue(qid.startswith("Q"))

            replied = _run(daemon.handle_request({
                "id": "2",
                "method": "bus.reply",
                "params": {
                    "workspace_root": root,
                    "id": qid,
                    "agent_id": "reverie",
                    "message": "lsp_calls handles shifted indices",
                },
            }))
            reply_result = _result(replied)
            self.assertEqual(
                cast(dict[str, Any], reply_result["question"])["question_id"],
                qid,
            )

            settled = _run(daemon.handle_request({
                "id": "3",
                "method": "bus.settle",
                "params": {"workspace_root": root},
            }))
            closed = cast(list[dict[str, Any]], _result(settled)["closed"])
            self.assertEqual(len(closed), 1)
            digest = closed[0]
            replies = cast(list[dict[str, Any]], digest["replies"])
            self.assertEqual(
                {cast(str, r["event_type"]) for r in replies},
                {"bus.reply"},
            )

    def test_weather_after_settle_reports_no_open_questions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            _run(daemon.handle_request({
                "id": "1",
                "method": "bus.ask",
                "params": {
                    "workspace_root": root,
                    "message": "ping",
                    "timeout": 0,
                },
            }))
            response = _run(daemon.handle_request({
                "id": "2",
                "method": "bus.weather",
                "params": {"workspace_root": root},
            }))
            result = _result(response)
            self.assertEqual(result["open_questions"], [])
            self.assertEqual(result["workspace_root"], root)

    def test_ticket_journal_and_build_gate_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            for agent_id in ("agent-a", "agent-b"):
                response = _run(daemon.handle_request({
                    "id": f"ticket-{agent_id}",
                    "method": "bus.ticket",
                    "params": {
                        "workspace_root": root,
                        "agent_id": agent_id,
                        "message": "coordinate build gate",
                    },
                }))
                self.assertIn("ticket", _result(response))

            first_gate = _run(daemon.handle_request({
                "id": "gate-a",
                "method": "bus.build_gate",
                "params": {"workspace_root": root, "agent_id": "agent-a"},
            }))
            second_gate = _run(daemon.handle_request({
                "id": "gate-b",
                "method": "bus.build_gate",
                "params": {"workspace_root": root, "agent_id": "agent-b"},
            }))
            journal = _run(daemon.handle_request({
                "id": "journal",
                "method": "bus.journal",
                "params": {"workspace_root": root, "limit": 25},
            }))

            self.assertFalse(_result(first_gate)["unlocked"])
            self.assertTrue(_result(second_gate)["unlocked"])
            events = cast(list[dict[str, Any]], _result(journal)["events"])
            self.assertEqual(
                [event["event_type"] for event in events],
                ["ticket.started", "ticket.joined"],
            )

    def test_chat_reply_closes_ask_through_broker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            opened = _run(daemon.handle_request({
                "id": "ask",
                "method": "bus.ask",
                "params": {
                    "workspace_root": root,
                    "agent_id": "agent-a",
                    "message": "build?",
                    "timeout": "30s",
                },
            }))
            qid = cast(str, cast(dict[str, Any], _result(opened)["question"])["question_id"])

            replied = _run(daemon.handle_request({
                "id": "chat",
                "method": "bus.chat",
                "params": {
                    "workspace_root": root,
                    "agent_id": "agent-b",
                    "id": qid,
                    "message": "go",
                },
            }))
            status = _run(daemon.handle_request({
                "id": "question",
                "method": "bus.question",
                "params": {"workspace_root": root, "id": qid},
            }))

            chat_question = cast(dict[str, Any], _result(replied)["question"])
            replies = cast(list[dict[str, Any]], _result(status)["replies"])
            self.assertIsNotNone(chat_question["closed_at"])
            self.assertEqual([reply["event_type"] for reply in replies], ["bus.reply"])

    def test_edit_gate_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            denied = _run(daemon.handle_request({
                "id": "denied",
                "method": "bus.edit_gate",
                "params": {"workspace_root": root, "agent_id": "agent-a"},
            }))
            _run(daemon.handle_request({
                "id": "ticket",
                "method": "bus.ticket",
                "params": {
                    "workspace_root": root,
                    "agent_id": "agent-b",
                    "message": "editing",
                },
            }))
            allowed = _run(daemon.handle_request({
                "id": "allowed",
                "method": "bus.edit_gate",
                "params": {"workspace_root": root, "agent_id": "agent-a"},
            }))

            self.assertFalse(_result(denied)["allowed"])
            self.assertTrue(_result(allowed)["allowed"])


class BrokerBusWorkspaceScopingTests(unittest.TestCase):
    """Bus must key by workspace root, not LSP config_hash."""

    def test_two_roots_keep_recent_separate(self) -> None:
        with tempfile.TemporaryDirectory() as root_a, tempfile.TemporaryDirectory() as root_b:
            daemon = BrokerDaemon()
            for root, message in ((root_a, "alpha"), (root_b, "beta")):
                _run(daemon.handle_request({
                    "id": message,
                    "method": "bus.append",
                    "params": {
                        "workspace_root": root,
                        "event_type": "note.posted",
                        "message": message,
                    },
                }))
            response_a = _run(daemon.handle_request({
                "id": "ra",
                "method": "bus.recent",
                "params": {"workspace_root": root_a},
            }))
            events_a = cast(list[dict[str, Any]], _result(response_a)["events"])
            self.assertEqual({cast(str, e["workspace_root"]) for e in events_a}, {root_a})
            self.assertEqual({cast(str, e["message"]) for e in events_a}, {"alpha"})

    def test_same_root_two_config_hashes_share_recent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            # Bus is workspace-scoped — config_hash is metadata only.
            for chash in ("h1", "h2"):
                _run(daemon.handle_request({
                    "id": chash,
                    "method": "bus.append",
                    "params": {
                        "workspace_root": root,
                        "event_type": "task.intent",
                        "message": f"started under {chash}",
                        "metadata": {"config_hash": chash},
                    },
                }))
            response = _run(daemon.handle_request({
                "id": "r",
                "method": "bus.recent",
                "params": {"workspace_root": root},
            }))
            events = cast(list[dict[str, Any]], _result(response)["events"])
            hashes = {
                cast(dict[str, Any], e.get("metadata", {})).get("config_hash")
                for e in events
            }
            self.assertEqual(hashes, {"h1", "h2"})


class BrokerBusStatusTests(unittest.TestCase):
    """``status`` and ``lsp.status`` must surface the bus summary."""

    def test_status_response_carries_bus_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            _run(daemon.handle_request({
                "id": "n",
                "method": "bus.note",
                "params": {"workspace_root": root, "message": "warm"},
            }))
            response = _run(daemon.handle_request({
                "id": "s",
                "method": "status",
                "params": {},
            }))
            result = _result(response)
            bus = cast(dict[str, Any], result["bus"])
            self.assertGreaterEqual(cast(int, bus["event_count"]), 1)
            self.assertIn("open_question_count", bus)

    def test_lsp_status_response_carries_bus_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            daemon = BrokerDaemon()
            _run(daemon.handle_request({
                "id": "a",
                "method": "bus.ask",
                "params": {
                    "workspace_root": root,
                    "message": "weather check",
                    "timeout": 60,
                },
            }))
            response = _run(daemon.handle_request({
                "id": "ls",
                "method": "lsp.status",
                "params": {},
            }))
            result = _result(response)
            bus = cast(dict[str, Any], result["bus"])
            self.assertEqual(cast(int, bus["open_question_count"]), 1)
            open_questions = cast(list[dict[str, Any]], bus["open_questions"])
            self.assertEqual(len(open_questions), 1)


class BrokerBusErrorShapeTests(unittest.TestCase):
    """Malformed params must surface as structured ``invalid_params``."""

    def test_reply_to_unknown_question_returns_invalid_params(self) -> None:
        daemon = BrokerDaemon()
        response = _run(daemon.handle_request({
            "id": "bad",
            "method": "bus.reply",
            "params": {"id": "Q9999", "message": "noop"},
        }))
        error = _error(response)
        self.assertEqual(error["code"], "invalid_params")
        self.assertIn("Q9999", cast(str, error["message"]))

    def test_reply_without_id_returns_invalid_params(self) -> None:
        daemon = BrokerDaemon()
        response = _run(daemon.handle_request({
            "id": "bad2",
            "method": "bus.reply",
            "params": {"message": "noop"},
        }))
        error = _error(response)
        self.assertEqual(error["code"], "invalid_params")

    def test_unknown_bus_method_returns_unknown_method(self) -> None:
        daemon = BrokerDaemon()
        response = _run(daemon.handle_request({
            "id": "bad3",
            "method": "bus.does_not_exist",
            "params": {},
        }))
        error = _error(response)
        self.assertEqual(error["code"], "unknown_method")

    def test_non_object_params_return_invalid_request(self) -> None:
        daemon = BrokerDaemon()
        response = _run(daemon.handle_request({
            "id": "bad4",
            "method": "bus.note",
            "params": "not-a-dict",
        }))
        error = _error(response)
        self.assertEqual(error["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
