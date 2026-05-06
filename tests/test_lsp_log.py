"""Wave 1 unit coverage for ``lsp_log`` (the agent-bus MCP surface).

``docs/agent-bus.md`` and ``docs/tool-surface.md`` define ``lsp_log`` as the
public coordination tool: nine actions on a single signature, broker-first
with a local in-process fallback so solo agents and broker-down recoveries
still get useful weather. The acceptance contracts pinned here mirror the
spec block in the prompt:

```python
async def lsp_log(
    action: str = "weather",
    message: str = "",
    files: str = "",
    symbols: str = "",
    aliases: str = "",
    id: str = "",
    timeout: str = "3m",
    kind: str = "",
    status: str = "",
    targets: str = "",
    commit: str = "",
) -> str: ...
```

Tests stay safe to run without a live broker because the in-process
:class:`~hsp.agent_bus.AgentBus` answers every action when
``HSP_BROKER=off`` is set in the environment.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import unittest
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, patch

from hsp import server as _server
from hsp.agent_bus import AgentBus
from hsp.broker import BrokerError
from hsp.server import _ALL_TOOLS, TOOL_CAPABILITIES, _BUS_ACTIONS


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_log returned non-str: {type(result)!r}"
    return result


class _LocalBusFixture(unittest.TestCase):
    """Reset the in-process bus and force broker=off for deterministic tests.

    The broker fallback policy is "auto": only env that explicitly disables
    the broker keeps these tests off the socket. Each test clears the
    module-level ``_local_bus`` singleton so questions/events do not leak
    between cases.
    """

    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ,
            {"HSP_BROKER": "off", "LSP_ROOT": os.getcwd()},
            clear=False,
        )
        self._env_patch.start()
        self._prev_local_bus = _server._local_bus
        _server._local_bus = AgentBus()

    def tearDown(self) -> None:
        _server._local_bus = self._prev_local_bus
        self._env_patch.stop()


class LspLogSignatureTests(unittest.TestCase):
    """Pin the public signature so the agent-first one-line contract from
    docs/agent-bus.md cannot drift. Argument names are agent-visible — MCP
    tools dispatch by kwargs, so renaming ``kind`` → ``event_type`` would
    silently break every existing call site.
    """

    def test_lsp_log_is_async_callable(self) -> None:
        self.assertTrue(
            inspect.iscoroutinefunction(_server.lsp_log),
            "lsp_log must be `async def` — _wrap_with_header only wraps coroutines",
        )

    def test_lsp_log_signature_matches_spec(self) -> None:
        sig = inspect.signature(_server.lsp_log)
        self.assertEqual(
            list(sig.parameters),
            [
                "action",
                "message",
                "files",
                "symbols",
                "aliases",
                "id",
                "timeout",
                "kind",
                "status",
                "targets",
                "commit",
            ],
            f"lsp_log signature drifted from the Wave 1 spec: {sig}",
        )

    def test_lsp_log_defaults_match_spec(self) -> None:
        params = inspect.signature(_server.lsp_log).parameters
        # Default action is the agent-friendly resume-state hop; bare
        # ``lsp_log()`` must produce a weather report rather than erroring.
        self.assertEqual(params["action"].default, "weather")
        # docs/agent-bus.md: "3m" matches the example bus question; the
        # default is load-bearing for ask without an explicit timeout.
        self.assertEqual(params["timeout"].default, "3m")
        for name in ("message", "files", "symbols", "aliases", "id", "kind",
                     "status", "targets", "commit"):
            self.assertEqual(
                params[name].default,
                "",
                f"{name} default drifted; agent kwargs must default to empty",
            )

    def test_lsp_log_returns_str(self) -> None:
        # ``from __future__ import annotations`` keeps the return annotation
        # as the literal string "str", so compare textually rather than to
        # the type — both forms guarantee the MCP contract that the wrapper
        # always sees a string body to prefix.
        sig = inspect.signature(_server.lsp_log)
        self.assertEqual(sig.return_annotation, "str")


class LspLogRegistryTests(unittest.TestCase):
    """The bus surface must register under ``log`` with capability ``None``
    (the bus has no single LSP backend method to gate on, mirroring the
    ``confirm`` / ``session`` / ``memory`` admin tools).
    """

    def test_log_is_registered_in_all_tools(self) -> None:
        self.assertIn("log", _ALL_TOOLS, "log not registered in _ALL_TOOLS")

    def test_log_method_label_uses_hsp_namespace(self) -> None:
        _func, method = _ALL_TOOLS["log"]
        self.assertTrue(
            method.startswith("hsp/"),
            f"log method label {method!r} should live under hsp/",
        )
        # Pin the exact label — agents can grep for it in [header] lines.
        self.assertEqual(method, "hsp/log")

    def test_log_capability_is_none(self) -> None:
        self.assertIn("log", TOOL_CAPABILITIES)
        self.assertIsNone(
            TOOL_CAPABILITIES["log"],
            "log has no single LSP capability to gate on; capability "
            "gating must not drop the whole bus surface when a server "
            "happens to lack one provider",
        )

    def test_log_registry_function_matches_module_attr(self) -> None:
        func, _method = _ALL_TOOLS["log"]
        self.assertIs(
            func,
            getattr(_server, "lsp_log", None),
            "_ALL_TOOLS['log'] must be the public lsp_log — divergence "
            "means the tool registers a stale alias",
        )

    def test_team_tools_register_as_hsp_capability_free_verbs(self) -> None:
        expected = {
            "ticket": "hsp/ticket",
            "journal": "hsp/journal",
            "ask": "hsp/ask",
            "chat": "hsp/chat",
        }
        for name, method in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, _ALL_TOOLS)
                func, registered = _ALL_TOOLS[name]
                self.assertIs(func, getattr(_server, name))
                self.assertEqual(registered, method)
                self.assertIn(name, TOOL_CAPABILITIES)
                self.assertIsNone(TOOL_CAPABILITIES[name])

    def test_build_gate_is_implicit_not_agent_facing_mcp(self) -> None:
        self.assertNotIn("build_gate", _server._BUS_ACTIONS)
        self.assertNotIn("build_gate", _ALL_TOOLS)
        self.assertNotIn("build_gate", TOOL_CAPABILITIES)

    def test_edit_gate_is_lsp_log_action_but_not_short_tool(self) -> None:
        self.assertIn("edit_gate", _server._BUS_ACTIONS)
        self.assertNotIn("edit_gate", _ALL_TOOLS)


class LspLogDefensiveTests(_LocalBusFixture):
    """Bad input from an agent (or hook) must surface as a string so the
    agent loop can read it and self-correct. Raising into the MCP transport
    here would break the entire conversation, not just the tool call.
    """

    def test_unknown_action_returns_string(self) -> None:
        try:
            result = _run(_server.lsp_log(action="bogus"))
        except (TypeError, ValueError) as e:
            self.fail(
                f"lsp_log(action='bogus') raised {type(e).__name__}: {e}; "
                f"unknown actions must echo back as strings"
            )
        self.assertIn("bogus", result)
        self.assertIn("Unknown action", result)
        # The hint must list the canonical action set so the agent has a
        # path to self-correction without reading the source.
        for action in _BUS_ACTIONS:
            self.assertIn(action, result)

    def test_bad_timeout_returns_string(self) -> None:
        result = _run(_server.lsp_log(action="ask", message="ping?", timeout="nope"))
        self.assertIn("nope", result)
        self.assertIn("timeout", result.lower())

    def test_ask_without_message_returns_string(self) -> None:
        result = _run(_server.lsp_log(action="ask"))
        self.assertIn("ask", result)
        self.assertIn("message", result)

    def test_reply_without_id_returns_string(self) -> None:
        result = _run(_server.lsp_log(action="reply", message="here"))
        self.assertIn("reply", result)
        self.assertIn("id", result)


class LspLogEmptyStateTests(_LocalBusFixture):
    """A pristine bus must still return human-readable lines — agents read
    "no bus activity" / "no expired questions" as a green light to proceed,
    not silence them as failures.
    """

    def test_weather_on_pristine_bus_lists_no_activity(self) -> None:
        result = _run(_server.lsp_log(action="weather"))
        # workspace breadcrumb + zero open questions + zero recent events
        self.assertIn("workspace:", result)
        self.assertIn("open questions: 0", result)
        self.assertIn("recent: 0", result)

    def test_recent_on_pristine_bus_returns_empty_marker(self) -> None:
        result = _run(_server.lsp_log(action="recent"))
        self.assertIn("recent", result.lower())
        self.assertIn("none", result.lower())

    def test_settle_with_no_questions_returns_empty_marker(self) -> None:
        result = _run(_server.lsp_log(action="settle"))
        self.assertIn("settle", result.lower())
        self.assertIn("expired", result.lower())


class LspLogAskFlowTests(_LocalBusFixture):
    """Asking a question must surface a Q-handle and a copy-pasteable
    reply hint so an agent never has to invent the call shape itself.
    """

    def test_ask_returns_question_handle_and_reply_hint(self) -> None:
        assert _server._local_bus is not None
        _server._local_bus.ticket({
            "workspace_root": os.getcwd(),
            "agent_id": "agent-b",
            "message": "editing server",
        })
        result = _run(_server.lsp_log(
            action="ask",
            message="Anyone touching server.py?",
            files="src/server.py",
            timeout="30s",
        ))
        # Q-handle is visible
        self.assertRegex(result, r"Q\d+")
        # Reply instruction tells the agent exactly how to respond
        self.assertIn("reply", result.lower())
        self.assertIn("lsp_log", result)
        # Question text is preserved verbatim
        self.assertIn("Anyone touching server.py?", result)

    def test_ask_without_busy_agents_returns_no_replier_notice(self) -> None:
        result = _run(_server.lsp_log(
            action="ask",
            message="Anyone touching server.py?",
            files="src/server.py",
            timeout="30s",
        ))

        self.assertIn("not waiting", result)
        self.assertIn("no agents can reply", result)
        self.assertNotIn("reply: lsp_log", result)

    def test_ask_then_reply_links_back_to_question(self) -> None:
        assert _server._local_bus is not None
        _server._local_bus.ticket({
            "workspace_root": os.getcwd(),
            "agent_id": "agent-b",
            "message": "editing server",
        })
        ask_result = _run(_server.lsp_log(
            action="ask",
            message="proceed?",
            timeout="30s",
        ))
        # Pull the question id from the rendered output — that is the
        # contract the agent will follow.
        match = next(
            (token for token in ask_result.split() if token.startswith("Q")),
            "",
        )
        # Strip trailing punctuation if the renderer wrapped Q0 in parens.
        qid = match.strip("():,")
        self.assertTrue(qid, f"no Q-handle in ask output: {ask_result!r}")
        reply_result = _run(_server.lsp_log(
            action="reply",
            id=qid,
            message="go ahead",
        ))
        self.assertIn(qid, reply_result)

    def test_reply_to_unknown_question_returns_string(self) -> None:
        result = _run(_server.lsp_log(
            action="reply",
            id="QNOPE",
            message="hi",
        ))
        # Bubbles ValueError("unknown question: ...") from AgentBus.reply.
        self.assertIn("QNOPE", result)


class TeamToolFlowTests(_LocalBusFixture):
    """The short MCP names are the agent treadmill surface.

    ``ticket`` announces work, ``journal`` is the compact board, and
    ``chat(id=Qn)`` is the reply that wakes a waiting ask. Build gates are
    ambient hook/wrapper behavior, not an MCP verb agents should call.
    """

    def test_ticket_and_journal_short_tools(self) -> None:
        ticket = _run(_server.ticket("wire build gate", files="src/hsp/server.py"))
        journal = _run(_server.journal(limit=10))

        self.assertIn("ticket T1", ticket)
        self.assertIn("active tickets: 1", ticket)
        self.assertIn("journal:", journal)
        self.assertIn("ticket.started", journal)

    def test_lsp_log_ticket_action_releases_current_ticket(self) -> None:
        _run(_server.lsp_log(action="ticket", message="wire release"))

        released = _run(_server.lsp_log(action="ticket", message=""))

        self.assertIn("ticket released", released)
        self.assertIn("ticket.closed", released)

    def test_chat_short_tool_unlocks_question(self) -> None:
        assert _server._local_bus is not None
        _server._local_bus.ticket({
            "workspace_root": os.getcwd(),
            "agent_id": "agent-b",
            "message": "editing server",
        })
        opened = _run(_server.lsp_log(action="ask", message="Proceed?", timeout="30s"))
        qid = next(token.strip("():,") for token in opened.split() if token.startswith("Q"))

        reply = _run(_server.chat("go", id=qid))

        self.assertIn(f"unlocked {qid}", reply)
        self.assertIn("bus.reply", reply)

    def test_ask_short_tool_times_out_with_latest_journal(self) -> None:
        assert _server._local_bus is not None
        _server._local_bus.ticket({
            "workspace_root": os.getcwd(),
            "agent_id": "agent-b",
            "message": "editing server",
        })
        result = _run(_server.ask("Anybody still editing?", timeout="1ms"))

        self.assertIn("timed out", result)
        self.assertIn("journal:", result)

    def test_ask_short_tool_does_not_wait_without_busy_agents(self) -> None:
        result = _run(_server.ask("Anybody still editing?", timeout="30s"))

        self.assertIn("not waiting", result)
        self.assertIn("no agents can reply", result)
        self.assertIn("journal:", result)

    def test_build_gate_times_out_when_ticket_holders_are_still_editing(self) -> None:
        assert _server._local_bus is not None
        _server._local_bus.ticket({
            "workspace_root": os.getcwd(),
            "agent_id": "agent-a",
            "message": "edit a",
        })
        _server._local_bus.ticket({
            "workspace_root": os.getcwd(),
            "agent_id": "agent-b",
            "message": "edit b",
        })

        result = _run(_server.implicit_build_gate("cargo test", timeout="1ms"))

        self.assertIn("timed out", result)
        self.assertIn("build gate: waiting", result)
        self.assertIn("active_tickets", result)


class LspLogBrokerRoutingTests(unittest.TestCase):
    """The fallback policy is the load-bearing contract: ``HSP_BROKER=on``
    surfaces broker errors directly so a misconfigured deployment is loud,
    while ``auto`` lets the agent keep coordinating against the in-process
    bus when the broker socket is gone.
    """

    def setUp(self) -> None:
        self._prev_local_bus = _server._local_bus
        _server._local_bus = AgentBus()

    def tearDown(self) -> None:
        _server._local_bus = self._prev_local_bus

    def test_broker_on_failure_surfaces_error_string(self) -> None:
        with patch.dict(
            os.environ,
            {"HSP_BROKER": "on", "LSP_SERVERS": "fake-ls", "LSP_ROOT": os.getcwd()},
            clear=False,
        ):
            with patch.object(
                _server,
                "_broker_bus_call",
                AsyncMock(side_effect=BrokerError("transport", "boom")),
            ):
                result = _run(_server.lsp_log(action="weather"))
        # In on-mode the failure must be visible — agents need to see the
        # misconfiguration rather than silently fall back to in-process state.
        self.assertIn("broker", result.lower())
        self.assertIn("transport", result)

    def test_broker_auto_falls_back_to_local_bus(self) -> None:
        with patch.dict(
            os.environ,
            {"HSP_BROKER": "auto", "LSP_SERVERS": "fake-ls", "LSP_ROOT": os.getcwd()},
            clear=False,
        ):
            with patch.object(
                _server,
                "_broker_bus_call",
                AsyncMock(side_effect=BrokerError("broker_unreachable", "no socket")),
            ):
                # Local bus has nothing, so the empty-state weather lines
                # are the proof that fallback succeeded (no broker error).
                result = _run(_server.lsp_log(action="weather"))
        self.assertIn("workspace:", result)
        self.assertIn("open questions: 0", result)

    def test_broker_off_uses_local_bus_directly(self) -> None:
        with patch.dict(
            os.environ,
            {"HSP_BROKER": "off", "LSP_SERVERS": "fake-ls", "LSP_ROOT": os.getcwd()},
            clear=False,
        ):
            with patch.object(
                _server,
                "_broker_bus_call",
                AsyncMock(side_effect=AssertionError("broker must not be called")),
            ):
                result = _run(_server.lsp_log(action="weather"))
        self.assertIn("workspace:", result)


if __name__ == "__main__":
    unittest.main()
