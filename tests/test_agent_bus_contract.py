"""Wave 1 cross-cutting contract tests for the agent bus and ``lsp_log``.

The agent bus is described in ``docs/agent-bus.md``. Wave 1 has three
moving parts that other workers ship in parallel:

- ``hsp.agent_bus`` — the workspace-scoped event log + question
  table (already landed as ``AgentBus``).
- ``BrokerDaemon`` bus methods (``bus.event|note|ask|reply|recent|settle|
  precommit|postcommit|weather``) — already wired in ``broker.py``.
- ``lsp_log`` — the public MCP coroutine that exposes the bus to agents
  (in flight; this test file is skip-tolerant of a missing source hook).

This file pins the agreed contracts so the moving pieces don't drift. It
deliberately overlaps with neither ``test_broker.py`` (broker protocol)
nor a future ``test_agent_bus.py`` (in-process AgentBus unit tests) —
this is the *cross-cutting acceptance surface* called out by the QA brief
for Wave 1:

- bus is workspace-scoped: same root + different config hashes share
  recent events; different roots stay separate;
- presence policy: active <60s, asleep >=60s, pruned hidden after 600s,
  ``prompt_count >= 2`` pinned (skip-tolerant — presence may land later);
- ``lsp_log`` shape: action default ``weather``, timeout default ``3m``,
  registered method label ``hsp/log``, capability ``None``;
- broker bus method names exist through ``BrokerDaemon.handle_request``.

Skipped tests double as a punch-list pointing the implementation worker
at the missing hook, in the same style as Wave 2/3/4 tool-surface tests.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any, cast

from hsp import server as _server
from hsp.agent_bus import AgentBus
from hsp.broker import BrokerDaemon
from hsp.server import _ALL_TOOLS, TOOL_CAPABILITIES


_LSP_LOG_HOOK_MSG = (
    "MISSING SOURCE HOOK: lsp_log not yet defined on hsp.server "
    "(Wave 1 agent-bus surface). docs/agent-bus.md and "
    "docs/tool-surface.md expect a coroutine "
    "`async def lsp_log(action='weather', message='', files='', "
    "symbols='', timeout='3m', id='') -> str`, registered as "
    "`hsp/log` with TOOL_CAPABILITIES[name] is None."
)

_PRESENCE_HOOK_MSG = (
    "MISSING SOURCE HOOK: presence policy not yet wired on AgentBus "
    "(Wave 1 agent-bus surface). docs/agent-bus.md presence rules "
    "expect active<60s / asleep>=60s / pruned after 600s, with "
    "prompt_count>=2 pinning the main thread. Look for AgentBus."
    "presence(...) or BrokerDaemon `bus.presence` once the source "
    "hook lands."
)


def _has_lsp_log() -> bool:
    return hasattr(_server, "lsp_log")


def _has_presence() -> bool:
    # Presence may surface either as a bare AgentBus method or as a
    # broker `bus.presence` method. Either is enough to run the policy
    # checks; absence of both is the skip case.
    return hasattr(AgentBus, "presence") or _broker_has_method("bus.presence")


def _broker_has_method(method: str) -> bool:
    daemon = BrokerDaemon()
    response = asyncio.run(
        daemon.handle_request({"id": "probe", "method": method, "params": {}})
    )
    error = response.get("error")
    if not isinstance(error, dict):
        return True
    return error.get("code") != "unknown_method"


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


class WorkspaceScopingTests(unittest.TestCase):
    """``docs/agent-bus.md``: 'workspace-scoped append-only JSONL log'.

    The bus must:
    - merge events from the same ``workspace_root`` regardless of the
      caller's LSP ``config_hash`` (two agents on different chains in the
      same repo share the weather report);
    - keep events from a *different* ``workspace_root`` strictly separate
      (the bus is per-repo, not user-global).
    """

    def test_same_root_different_config_hashes_share_recent_events(self) -> None:
        bus = AgentBus()
        # Two appends against the same workspace_root from two distinct
        # callers (modeled as differing agent_id + metadata config_hash).
        # config_hash must NOT shard the bus — workspace_root is the only
        # discriminator per the design doc.
        bus.event({
            "workspace_root": "/repo-a",
            "agent_id": "agent-x",
            "metadata": {"config_hash": "h1"},
            "event_type": "task.intent",
            "message": "x is starting",
        })
        bus.event({
            "workspace_root": "/repo-a",
            "agent_id": "agent-y",
            "metadata": {"config_hash": "h2"},
            "event_type": "task.intent",
            "message": "y is starting",
        })
        recent = bus.recent({"workspace_root": "/repo-a"})
        events = cast(list[dict[str, Any]], recent["events"])
        agent_ids = sorted({cast(str, e["agent_id"]) for e in events})
        self.assertEqual(agent_ids, ["agent-x", "agent-y"])

    def test_different_roots_do_not_leak_recent_events(self) -> None:
        bus = AgentBus()
        bus.event({
            "workspace_root": "/repo-a",
            "agent_id": "agent-x",
            "event_type": "file.touched",
            "message": "edit in repo-a",
        })
        bus.event({
            "workspace_root": "/repo-b",
            "agent_id": "agent-y",
            "event_type": "file.touched",
            "message": "edit in repo-b",
        })
        recent_a = bus.recent({"workspace_root": "/repo-a"})
        events_a = cast(list[dict[str, Any]], recent_a["events"])
        roots = {cast(str, e["workspace_root"]) for e in events_a}
        self.assertEqual(roots, {"/repo-a"})

    def test_workspace_id_is_derived_from_root(self) -> None:
        # docs/agent-bus.md `workspace_id` is the stable digest of the
        # workspace_root. Two agents auto-detecting the same repo path
        # must produce the same workspace_id so digest-frontier state
        # shared across the broker doesn't fragment.
        bus = AgentBus()
        result = bus.event({
            "workspace_root": "/some/repo",
            "event_type": "agent.started",
        })
        event_a = cast(dict[str, Any], result["event"])
        result = bus.event({
            "workspace_root": "/some/repo",
            "event_type": "agent.started",
        })
        event_b = cast(dict[str, Any], result["event"])
        self.assertEqual(event_a["workspace_id"], event_b["workspace_id"])
        self.assertNotEqual(event_a["workspace_id"], "")


class PresencePolicyTests(unittest.TestCase):
    """``docs/agent-bus.md`` presence rules.

    Until the presence hook lands these tests skip with a punch-list
    message. Once an ``AgentBus.presence`` method (or broker
    ``bus.presence`` method) appears, the assertions self-activate.
    """

    def test_active_threshold_under_60s(self) -> None:
        if not _has_presence():
            self.skipTest(_PRESENCE_HOOK_MSG)
        bus = AgentBus()
        # Heartbeat now → "active". Implementations may accept a `now`
        # kwarg or read the wall clock — guard for both shapes.
        bus.event({
            "workspace_root": "/repo",
            "agent_id": "agent-now",
            "event_type": "agent.started",
        })
        result = _call_presence(bus, workspace_root="/repo")
        agents = _agents_by_id(result)
        self.assertIn("agent-now", agents)
        self.assertEqual(agents["agent-now"]["state"], "active")

    def test_asleep_threshold_at_60s(self) -> None:
        if not _has_presence():
            self.skipTest(_PRESENCE_HOOK_MSG)
        bus = AgentBus()
        bus.event({
            "workspace_root": "/repo",
            "agent_id": "agent-stale",
            "event_type": "agent.started",
        })
        # 60s+ since heartbeat → asleep. Shifting the bus clock requires
        # the presence API to accept `now=`; if it doesn't, mark XFAIL
        # rather than passing falsely.
        try:
            result = _call_presence(bus, workspace_root="/repo", now_offset=120.0)
        except TypeError:
            self.skipTest(
                "presence API does not accept a now/now_offset kwarg; "
                "add it so policy thresholds are testable without "
                "real-time sleeps"
            )
        agents = _agents_by_id(result)
        self.assertEqual(agents["agent-stale"]["state"], "asleep")

    def test_pruned_after_600s(self) -> None:
        if not _has_presence():
            self.skipTest(_PRESENCE_HOOK_MSG)
        bus = AgentBus()
        bus.event({
            "workspace_root": "/repo",
            "agent_id": "agent-gone",
            "event_type": "agent.started",
        })
        try:
            result = _call_presence(bus, workspace_root="/repo", now_offset=900.0)
        except TypeError:
            self.skipTest("presence API needs now/now_offset to test pruning")
        agents = _agents_by_id(result)
        # Pruned == hidden from default presence output. Either omit the
        # row entirely, or surface it under a `pruned`/`hidden` key.
        if "agent-gone" in agents:
            self.assertEqual(agents["agent-gone"]["state"], "pruned")

    def test_prompt_count_two_pins_main_thread(self) -> None:
        if not _has_presence():
            self.skipTest(_PRESENCE_HOOK_MSG)
        bus = AgentBus()
        bus.event({
            "workspace_root": "/repo",
            "agent_id": "main-thread",
            "event_type": "user.prompt",
            "metadata": {"prompt_count": 2},
        })
        try:
            # Simulate enough time elapsed that this would normally prune,
            # then assert the pin keeps it visible.
            result = _call_presence(bus, workspace_root="/repo", now_offset=900.0)
        except TypeError:
            self.skipTest("presence API needs now/now_offset to test pinning")
        agents = _agents_by_id(result)
        self.assertIn(
            "main-thread",
            agents,
            "prompt_count>=2 must pin the main thread visible past the "
            "prune threshold (docs/agent-bus.md)",
        )
        self.assertTrue(agents["main-thread"].get("pinned", False))


class LspLogToolSurfaceTests(unittest.TestCase):
    """Wave 1 acceptance for the ``lsp_log`` MCP coroutine.

    docs/agent-bus.md "Tool Shape" pins the action set and defaults.
    docs/tool-surface.md adds it under "Planned coordination surface" as
    a non-LSP tool that must register with capability ``None`` (no LSP
    method gates it).

    Until ``lsp_log`` lands these tests skip with a punch-list message.
    Once the source hook ships, they self-activate.
    """

    def test_lsp_log_is_async_callable(self) -> None:
        if not _has_lsp_log():
            self.skipTest(_LSP_LOG_HOOK_MSG)
        self.assertTrue(
            inspect.iscoroutinefunction(_server.lsp_log),
            "lsp_log must be async — _wrap_with_header only wraps coroutines",
        )

    def test_lsp_log_action_defaults_to_weather(self) -> None:
        if not _has_lsp_log():
            self.skipTest(_LSP_LOG_HOOK_MSG)
        sig = inspect.signature(_server.lsp_log)
        self.assertIn(
            "action",
            sig.parameters,
            "lsp_log signature must accept `action` per docs/agent-bus.md",
        )
        self.assertEqual(
            sig.parameters["action"].default,
            "weather",
            "docs/agent-bus.md `weather` is the compact status default — "
            "bare lsp_log() must produce a weather report, not raise",
        )

    def test_lsp_log_timeout_defaults_to_3m(self) -> None:
        if not _has_lsp_log():
            self.skipTest(_LSP_LOG_HOOK_MSG)
        sig = inspect.signature(_server.lsp_log)
        self.assertIn(
            "timeout",
            sig.parameters,
            "lsp_log must accept `timeout` for action='ask' per "
            "docs/agent-bus.md example",
        )
        self.assertEqual(
            sig.parameters["timeout"].default,
            "3m",
            "docs/agent-bus.md example uses timeout='3m'; an empty or "
            "0 default would silently change ask-window semantics",
        )

    def test_log_registered_with_hsp_namespace(self) -> None:
        if "log" not in _ALL_TOOLS:
            self.skipTest(_LSP_LOG_HOOK_MSG)
        _func, method = _ALL_TOOLS["log"]
        self.assertEqual(
            method,
            "hsp/log",
            f"log method label must be `hsp/log` — got {method!r}. "
            "docs/agent-bus.md treats lsp_log as broker-shaped agent "
            "layer, so it lives under the hsp/ namespace alongside "
            "the other internal verbs (info, workspaces, confirm, ...).",
        )

    def test_log_capability_is_none(self) -> None:
        if "log" not in TOOL_CAPABILITIES:
            self.skipTest(_LSP_LOG_HOOK_MSG)
        # docs/tool-surface.md: "lsp_log is not a raw LSP verb. It belongs
        # to the broker-shaped agent layer". A non-None capability would
        # silently drop the bus from servers that don't advertise that
        # capability — lsp_log must always register.
        self.assertIsNone(
            TOOL_CAPABILITIES["log"],
            "log has no LSP capability to gate on — mirror confirm/session/"
            "memory which all use None",
        )

    def test_log_registered_function_matches_module_attr(self) -> None:
        if "log" not in _ALL_TOOLS:
            self.skipTest(_LSP_LOG_HOOK_MSG)
        func, _method = _ALL_TOOLS["log"]
        self.assertIs(
            func,
            getattr(_server, "lsp_log", None),
            "_ALL_TOOLS['log'] must be the public lsp_log — a divergence "
            "here means the registry maps to a stale alias",
        )


class BrokerBusMethodSurfaceTests(unittest.TestCase):
    """``BrokerDaemon.handle_request`` must accept the bus method names
    spelled out in ``docs/agent-bus.md``.

    A typo here (``bus.event`` vs ``bus.events``, etc.) silently breaks
    the broker socket since the bus is the only public coordination
    primitive Wave 1 ships. We assert by *absence* of the
    ``unknown_method`` error code — handlers can still surface
    ``invalid_params`` or pass-through results, but ``unknown_method``
    means the dispatcher doesn't know about the verb.
    """

    BUS_METHODS = [
        "bus.status",
        "bus.event",
        "bus.note",
        "bus.ask",
        "bus.reply",
        "bus.recent",
        "bus.settle",
        "bus.precommit",
        "bus.postcommit",
        "bus.weather",
    ]

    def test_all_bus_methods_dispatch(self) -> None:
        daemon = BrokerDaemon()
        for method in self.BUS_METHODS:
            with self.subTest(method=method):
                response = _run(
                    daemon.handle_request(
                        {
                            "id": method,
                            "method": method,
                            # workspace_root keeps the bus from defaulting
                            # to os.getcwd() inside the test runner.
                            "params": {
                                "workspace_root": "/tmp/hsp-bus-test",
                                "message": "probe",
                            },
                        }
                    )
                )
                error = response.get("error")
                if isinstance(error, dict):
                    self.assertNotEqual(
                        error.get("code"),
                        "unknown_method",
                        f"{method} not dispatched by BrokerDaemon — "
                        f"docs/agent-bus.md owns the method name",
                    )

    def test_bus_recent_is_workspace_scoped_through_broker(self) -> None:
        daemon = BrokerDaemon()
        _run(
            daemon.handle_request(
                {
                    "id": "1",
                    "method": "bus.event",
                    "params": {
                        "workspace_root": "/repo-x",
                        "agent_id": "agent-x",
                        "event_type": "task.intent",
                        "message": "x intent",
                    },
                }
            )
        )
        _run(
            daemon.handle_request(
                {
                    "id": "2",
                    "method": "bus.event",
                    "params": {
                        "workspace_root": "/repo-y",
                        "agent_id": "agent-y",
                        "event_type": "task.intent",
                        "message": "y intent",
                    },
                }
            )
        )
        recent = _run(
            daemon.handle_request(
                {
                    "id": "3",
                    "method": "bus.recent",
                    "params": {"workspace_root": "/repo-x"},
                }
            )
        )
        result = cast(dict[str, Any], recent["result"])
        events = cast(list[dict[str, Any]], result["events"])
        roots = {cast(str, e["workspace_root"]) for e in events}
        self.assertEqual(roots, {"/repo-x"})

    def test_bus_ask_returns_question_id_for_subsequent_reply(self) -> None:
        daemon = BrokerDaemon()
        opened = _run(
            daemon.handle_request(
                {
                    "id": "1",
                    "method": "bus.ask",
                    "params": {
                        "workspace_root": "/repo",
                        "agent_id": "noesis",
                        "message": "split lsp_refs?",
                        "files": "src/server.py",
                        # Honor docs/agent-bus.md "3m" notation through
                        # the broker boundary, not just the in-process
                        # AgentBus.
                        "timeout": "3m",
                    },
                }
            )
        )
        result = cast(dict[str, Any], opened["result"])
        question = cast(dict[str, Any], result["question"])
        qid = cast(str, question["question_id"])
        self.assertTrue(
            qid.startswith("Q"),
            f"question_id should be Q-prefixed per docs example; got {qid!r}",
        )
        # Reply round-trip: the question_id from `ask` must be accepted
        # by `reply` without further plumbing.
        replied = _run(
            daemon.handle_request(
                {
                    "id": "2",
                    "method": "bus.reply",
                    "params": {
                        "workspace_root": "/repo",
                        "id": qid,
                        "agent_id": "reverie",
                        "message": "lsp_calls already handles shifted indices",
                    },
                }
            )
        )
        reply_result = cast(dict[str, Any], replied["result"])
        self.assertEqual(
            cast(dict[str, Any], reply_result["question"])["question_id"],
            qid,
        )


# --- Helpers -----------------------------------------------------------------


def _call_presence(bus: AgentBus, *, workspace_root: str, now_offset: float = 0.0) -> Any:
    """Best-effort presence call across two possible API shapes.

    The presence API is in flight (per the QA brief). It may land as
    ``AgentBus.presence(params)`` or as a method on the broker bus
    surface. This helper hides the shape difference so the policy
    assertions stay readable; if neither shape exists, the caller has
    already skipped via ``_has_presence``.
    """
    fn = getattr(bus, "presence", None)
    if fn is None:
        # Routed through broker bus.presence instead.
        daemon = BrokerDaemon()
        # share the same bus instance so events are visible
        daemon.bus = bus
        params: dict[str, Any] = {"workspace_root": workspace_root}
        if now_offset:
            params["now_offset"] = now_offset
        response = asyncio.run(
            daemon.handle_request(
                {"id": "p", "method": "bus.presence", "params": params}
            )
        )
        return response.get("result", {})
    params = {"workspace_root": workspace_root}
    if now_offset:
        params["now_offset"] = now_offset
    return fn(params)


def _agents_by_id(result: Any) -> dict[str, dict[str, Any]]:
    """Normalize presence response shape so tests can index by agent_id.

    The expected shape is a list of agent records, each with at least
    ``agent_id`` and ``state`` keys. Implementations may return the list
    under ``agents``, ``presence``, or at the top level — accept any.
    """
    if isinstance(result, dict):
        agents = result.get("agents") or result.get("presence") or result.get("active")
    else:
        agents = result
    if not isinstance(agents, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for record in agents:
        if isinstance(record, dict):
            agent_id = record.get("agent_id")
            if isinstance(agent_id, str):
                out[agent_id] = record
    return out


if __name__ == "__main__":
    unittest.main()
