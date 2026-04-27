"""Wave 2 unit coverage for ``lsp_session``.

``docs/tool-surface.md`` defines ``lsp_session`` as the Wave 2 admin/read
operator that folds three tiny raw tools — ``lsp_info``, ``lsp_workspaces``,
and ``lsp_add_workspace`` — into one verb-driven surface. The acceptance
contracts pinned here mirror the spec block in the doc:

```python
async def lsp_session(
    action: str = "status",          # "status" | "add" | "warm" | "restart"
    path: str = "",                  # for add / warm
    server: str = "",                # for restart; "" = whole chain
) -> str: ...
```

This file pins the static contracts that don't need a live LSP chain:

1. Public signature shape — argument names, defaults, and the fact that
   ``action`` defaults to ``"status"`` (the doc explicitly says "``status``
   is the default and folds ``lsp_info`` + ``lsp_workspaces`` into one
   block"). A drift here breaks every agent that calls ``lsp_session()``
   with no args expecting a status block.
2. Async coroutine — ``_wrap_with_header`` only wraps coroutines.
3. Registry hygiene specific to ``session`` (capability gating shape).
4. Defensive surface — unknown actions and missing path/server arguments
   must return human-readable strings, not raise. The bare ``lsp_*`` tools
   already obey this convention (``lsp_add_workspace`` returns
   ``"Not a directory: ..."`` instead of raising); ``lsp_session`` must
   inherit that ergonomics so a bad action arg never breaks an agent loop.

Tests gate on ``hasattr(server, "lsp_session")``: if the source hook hasn't
landed yet they skip with a message naming the missing hook, doubling as a
punch list for whoever ships ``lsp_session``. The existing skip-as-punch-list
pattern matches Wave 2 ``test_tool_surface.py``'s
``test_session_replaces_info_workspaces_add_workspace``.

End-to-end behaviour against a real chain (warmup stats text, build SHA
line, per-server capability summary) belongs in live smoke per the docs.
"""
import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any, cast

from cc_lsp_now import server as _server
from cc_lsp_now.chain_server import ChainServer
from cc_lsp_now.server import _ALL_TOOLS, TOOL_CAPABILITIES


_SESSION_HOOK_MSG = (
    "MISSING SOURCE HOOK: lsp_session not yet defined on cc_lsp_now.server "
    "(Wave 2 verifier lane). docs/tool-surface.md expects "
    "`async def lsp_session(action='status', path='', server='') -> str`, "
    "absorbing lsp_info + lsp_workspaces + lsp_add_workspace."
)


def _has_session() -> bool:
    return hasattr(_server, "lsp_session")


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_session returned non-str: {type(result)!r}"
    return result


class LspSessionSignatureTests(unittest.TestCase):
    """Pin the public signature so the agent-first one-line contract from
    docs/tool-surface.md can't drift. Argument names are agent-visible —
    renaming ``action`` → ``verb`` would silently break every existing call
    site since MCP tools dispatch by kwargs.
    """

    def test_lsp_session_is_async_callable(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        self.assertTrue(
            inspect.iscoroutinefunction(_server.lsp_session),
            "lsp_session must be `async def` — _wrap_with_header only wraps coroutines",
        )

    def test_lsp_session_signature_matches_docs(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        sig = inspect.signature(_server.lsp_session)
        params = sig.parameters
        # docs/tool-surface.md pins exactly these three kwargs in this order.
        self.assertEqual(
            list(params),
            ["action", "path", "server"],
            f"lsp_session signature drifted from docs/tool-surface.md: {sig}",
        )

    def test_lsp_session_action_defaults_to_status(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        sig = inspect.signature(_server.lsp_session)
        self.assertEqual(
            sig.parameters["action"].default,
            "status",
            "docs/tool-surface.md: '`status` is the default and folds "
            "`lsp_info` + `lsp_workspaces` into one block' — bare "
            "`lsp_session()` must not require an explicit action",
        )

    def test_lsp_session_path_and_server_default_to_empty_string(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        sig = inspect.signature(_server.lsp_session)
        self.assertEqual(sig.parameters["path"].default, "")
        # docs: "for restart; \"\" = whole chain" — the empty default is
        # load-bearing for restart-all.
        self.assertEqual(sig.parameters["server"].default, "")


class LspSessionRegistryTests(unittest.TestCase):
    """Wave 2 acceptance for the registry: ``session`` is registered with
    a ``cc-lsp-now/...`` style internal method label (consistent with the
    other admin tools — ``info``, ``workspaces``, ``add_workspace``,
    ``confirm`` all use that prefix), and capability gating is disabled
    (``None``) since session has no single backend LSP method to gate on.

    The cross-cutting "raw info/workspaces/add_workspace are cut" check
    lives in tests/test_tool_surface.py
    (`test_session_replaces_info_workspaces_add_workspace`); these tests
    pin the *positive* registration shape of ``session`` itself.
    """

    def test_session_method_label_uses_cc_lsp_now_namespace(self) -> None:
        if "session" not in _ALL_TOOLS:
            self.skipTest(_SESSION_HOOK_MSG)
        _func, method = _ALL_TOOLS["session"]
        # Other admin tools register as e.g. "cc-lsp-now/info"; staying in
        # that namespace keeps the [header] line readable across the family.
        self.assertTrue(
            method.startswith("cc-lsp-now/"),
            f"session method label {method!r} should live under "
            f"the cc-lsp-now/ namespace alongside info/workspaces/confirm",
        )

    def test_session_capability_is_none(self) -> None:
        if "session" not in TOOL_CAPABILITIES:
            self.skipTest(_SESSION_HOOK_MSG)
        # docs/tool-surface.md: session "has no semantic-graph plumbing"
        # and is admin/read — like its predecessors info/workspaces/
        # add_workspace, all of which mapped to None. A non-None entry
        # would let capability gating drop the whole session surface
        # whenever the chain happens to lack that one capability.
        self.assertIsNone(
            TOOL_CAPABILITIES["session"],
            "session has no single LSP capability to gate on; mirror "
            "info/workspaces/add_workspace which all used None",
        )

    def test_session_registered_function_matches_module_attr(self) -> None:
        if "session" not in _ALL_TOOLS:
            self.skipTest(_SESSION_HOOK_MSG)
        func, _method = _ALL_TOOLS["session"]
        self.assertIs(
            func,
            getattr(_server, "lsp_session", None),
            "_ALL_TOOLS['session'] must be the public lsp_session — "
            "a divergence here means the tool registers a stale alias",
        )


class LspSessionDefensiveSurfaceTests(unittest.TestCase):
    """The action argument is agent-supplied free-form text; an unknown
    action or a missing required path must return a string (so the agent
    can read the error and self-correct), not raise an exception that
    breaks the MCP transport.

    These tests stay safe to run without a live LSP chain because:

    - ``status`` with no chain configured already works (lsp_workspaces
      returns "No chain configured." today).
    - Unknown actions and "add"/"warm" with no path should short-circuit
      *before* touching the chain.
    """

    def test_unknown_action_returns_string_not_raises(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        try:
            result = _run(_server.lsp_session(action="bogus"))
        except (TypeError, ValueError) as e:
            self.fail(
                f"lsp_session(action='bogus') raised {type(e).__name__}: {e}. "
                f"Bad action input from an agent must surface as a string "
                f"so the agent can read it and self-correct."
            )
        # Don't pin exact wording — just that the response references the
        # bad action so the agent sees what went wrong.
        self.assertIn(
            "bogus",
            result,
            f"unknown-action error should echo the bad input; got: {result!r}",
        )

    def test_add_action_with_empty_path_does_not_raise(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        try:
            result = _run(_server.lsp_session(action="add", path=""))
        except (TypeError, ValueError) as e:
            self.fail(
                f"lsp_session(action='add', path='') raised {type(e).__name__}: {e}. "
                f"A missing required path must produce a help string, "
                f"not raise."
            )
        self.assertIsInstance(result, str)
        self.assertTrue(result, "empty result on missing path is uninformative")

    def test_add_action_rejects_non_directory_like_predecessor(self) -> None:
        # lsp_add_workspace returns "Not a directory: {abs_path}" when the
        # path does not point at a real directory. The "add" sub-action
        # must inherit that gating — silently spawning the chain on a
        # bogus path would warm zero files and confuse the agent.
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        result = _run(
            _server.lsp_session(
                action="add",
                path="/definitely/not/a/real/dir/xyz_cc_lsp_now_test",
            )
        )
        self.assertIsInstance(result, str)
        self.assertIn(
            "Not a directory",
            result,
            "lsp_session(action='add', ...) must preserve the "
            "'Not a directory: ...' guard from lsp_add_workspace; got: "
            f"{result!r}",
        )

    def test_warm_action_rejects_non_directory_before_chain_setup(self) -> None:
        if not _has_session():
            self.skipTest(_SESSION_HOOK_MSG)
        result = _run(
            _server.lsp_session(
                action="warm",
                path="/definitely/not/a/real/dir/xyz_cc_lsp_now_test",
            )
        )
        self.assertIn("Not a directory", result)


class LspSessionLifecycleHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._chain_configs = list(_server._chain_configs)
        self._chain_clients = list(_server._chain_clients)
        self._method_handler = dict(_server._method_handler)
        self._warmed_folders = set(_server._warmed_folders)
        self._folder_warmup_stats = dict(_server._folder_warmup_stats)
        self._get_client: Any = _server._get_client
        self._warmup_folder: Any = _server._warmup_folder

    def tearDown(self) -> None:
        _server._chain_configs[:] = self._chain_configs
        _server._chain_clients[:] = self._chain_clients
        _server._method_handler.clear()
        _server._method_handler.update(self._method_handler)
        _server._warmed_folders.clear()
        _server._warmed_folders.update(self._warmed_folders)
        _server._folder_warmup_stats.clear()
        _server._folder_warmup_stats.update(self._folder_warmup_stats)
        setattr(_server, "_get_client", self._get_client)
        setattr(_server, "_warmup_folder", self._warmup_folder)

    def test_server_filter_accepts_label_command_or_name(self) -> None:
        _server._chain_configs[:] = [
            ChainServer(command="cmd-ls", args=[], name="friendly", label="cmd-ls (fallback)")
        ]

        self.assertEqual(_server._session_resolve_indices("cmd-ls (fallback)"), [0])
        self.assertEqual(_server._session_resolve_indices("cmd-ls"), [0])
        self.assertEqual(_server._session_resolve_indices("friendly"), [0])

    def test_restart_clears_cached_handlers_for_restarted_or_unsupported_methods(self) -> None:
        _server._chain_configs[:] = [
            ChainServer(command="primary", args=[], name="primary", label="primary"),
            ChainServer(command="fallback", args=[], name="fallback", label="fallback"),
        ]
        _server._chain_clients[:] = [None, None]
        _server._method_handler.clear()
        _server._method_handler.update({
            "unsupported": None,
            "primary/method": 0,
            "fallback/method": 1,
        })

        result = _run(_server._session_restart("primary"))

        self.assertIn("not running", result)
        self.assertNotIn("unsupported", _server._method_handler)
        self.assertNotIn("primary/method", _server._method_handler)
        self.assertEqual(_server._method_handler.get("fallback/method"), 1)

    def test_restart_restores_dynamic_workspace_folders(self) -> None:
        class FakeClient:
            def __init__(self, folders: set[str]):
                self._root_path = "/repo"
                self.workspace_folders = set(folders)

            async def stop(self) -> None:
                pass

            def add_workspace_folder(self, folder: str) -> bool:
                if folder in self.workspace_folders:
                    return False
                self.workspace_folders.add(folder)
                return True

        old_client = FakeClient({"/repo", "/repo/extra"})
        new_client = FakeClient({"/repo"})
        _server._chain_configs[:] = [ChainServer(command="primary", args=[], name="primary", label="primary")]
        cast(list[Any], _server._chain_clients)[:] = [old_client]

        async def fake_get_client(idx: int) -> Any:
            cast(list[Any], _server._chain_clients)[idx] = new_client
            return new_client

        async def fake_warmup_folder(_client: Any, _folder: str) -> int:
            return 0

        setattr(_server, "_get_client", fake_get_client)
        setattr(_server, "_warmup_folder", fake_warmup_folder)

        result = _run(_server._session_restart("primary"))

        self.assertIn("restored 1 workspace folder", result)
        self.assertIn("/repo/extra", new_client.workspace_folders)


if __name__ == "__main__":
    unittest.main()
