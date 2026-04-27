"""Wave 3 unit coverage for ``lsp_move``.

``docs/tool-surface.md`` defines ``lsp_move`` as the Wave 3 mutation-lane
preview-and-stage operator that folds the raw move-file pair
(``lsp_move_file`` and ``lsp_move_files``) into the agent-first surface.
The doc pins the one-line agent contract:

```python
async def lsp_move(
    from_path: str = "",
    to_path: str = "",
    symbol: str = "",
    moves: str = "",
) -> str: ...
```

What we pin without a live LSP chain:

1. Public signature - argument names and defaults. Drift here breaks
   every agent that calls ``lsp_move(symbol="MyClass", to_path=...)``
   since MCP tools dispatch by kwargs.
2. Async coroutine - ``_wrap_with_header`` only wraps coroutines.
3. Routing - ``lsp_move`` must forward to the existing ``_do_move``
   helper with the right list of ``(from, to)`` pairs:

   - single ``from_path`` / ``to_path`` collapses to a single-pair list,
   - ``moves`` parses into multiple pairs (Wave 3 collapses
     ``lsp_move_files`` into ``lsp_move(moves=...)``),
   - ``symbol`` + ``to_path`` runs ``_resolve_symbol_to_file`` first
     and forwards the resolved source as the from-path.

End-to-end behaviour against a real chain (willRenameFiles round-trip,
import-rewriter fallback, candidate staging) belongs in live smoke per
the docs. ``_do_move`` itself is the seam already exercised by the raw
move-file tests, so re-driving it through the new surface would just
duplicate that coverage.

Tests gate on ``hasattr(server, "lsp_move")``: if the source hook hasn't
landed yet they skip with a message naming the missing hook, doubling as
a punch list for whoever ships ``lsp_move``. This mirrors the Wave 2
``test_lsp_session.py`` / ``test_lsp_fix.py`` pattern.
"""
import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any

from cc_lsp_now import server as _server
from cc_lsp_now.server import _ALL_TOOLS, TOOL_CAPABILITIES


_MOVE_HOOK_MSG = (
    "MISSING SOURCE HOOK: lsp_move not yet defined on cc_lsp_now.server "
    "(Wave 3 mutation lane). docs/tool-surface.md expects "
    "`async def lsp_move(from_path='', to_path='', symbol='', moves='') -> str`, "
    "absorbing lsp_move_file + lsp_move_files into one preview-and-stage tool."
)

_MOVE_REGISTRY_MSG = (
    "MISSING SOURCE HOOK: 'move' not yet registered in _ALL_TOOLS. "
    "docs/tool-surface.md Raw Tool Cut Map: `move` collapses "
    "`move_file` + `move_files` and gates on willRenameFiles."
)


def _has_move() -> bool:
    return hasattr(_server, "lsp_move")


def _move_attr() -> Any:
    # Resolved via getattr so ty doesn't choke on the still-missing
    # attribute when the source hook hasn't landed yet - once lsp_move
    # ships the runtime check (_has_move) gates whether it actually
    # gets called, but the static lookup must stay forgiving.
    return getattr(_server, "lsp_move", None)


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_move returned non-str: {type(result)!r}"
    return result


class LspMoveSignatureTests(unittest.TestCase):
    """Pin the public signature so the agent-first one-line contract from
    docs/tool-surface.md can't drift. Argument names are agent-visible -
    renaming ``moves`` -> ``batch`` would silently break every existing
    call site since MCP tools dispatch by kwargs.
    """

    def test_lsp_move_is_async_callable(self) -> None:
        if not _has_move():
            self.skipTest(_MOVE_HOOK_MSG)
        self.assertTrue(
            inspect.iscoroutinefunction(_move_attr()),
            "lsp_move must be `async def` - _wrap_with_header only wraps coroutines",
        )

    def test_lsp_move_signature_matches_docs(self) -> None:
        if not _has_move():
            self.skipTest(_MOVE_HOOK_MSG)
        sig = inspect.signature(_move_attr())
        # docs/tool-surface.md pins exactly these four kwargs in this
        # order: ``from_path`` / ``to_path`` are the single-move shape
        # (the lsp_move_file legacy), ``symbol`` is the workspace/symbol
        # source-resolution path, ``moves`` is the batch shape (the
        # lsp_move_files legacy).
        self.assertEqual(
            list(sig.parameters),
            ["from_path", "to_path", "symbol", "moves"],
            f"lsp_move signature drifted from docs/tool-surface.md: {sig}",
        )

    def test_lsp_move_args_default_to_empty_string(self) -> None:
        if not _has_move():
            self.skipTest(_MOVE_HOOK_MSG)
        sig = inspect.signature(_move_attr())
        # All four args default to "" so an agent can pick any one of
        # the three call shapes (single, batch, symbol) without padding
        # the others. A non-empty default would silently force a shape.
        for name in ("from_path", "to_path", "symbol", "moves"):
            self.assertEqual(
                sig.parameters[name].default,
                "",
                f"lsp_move({name}=...) default should be '' (the empty "
                f"sentinel signaling 'not supplied')",
            )


class LspMoveRegistryTests(unittest.TestCase):
    """Wave 3 acceptance for the registry: ``move`` is registered, the
    function attribute matches the registered tuple, capability gating
    targets ``workspace.fileOperations.willRename``, and the raw
    ``move_file`` / ``move_files`` are gone.

    Cross-cutting raw cuts also live in tests/test_tool_surface.py
    (``WaveThreeSurfaceTests``); this file pins the *positive*
    registration shape of ``move`` itself for parity with
    test_lsp_outline.py / test_lsp_calls.py / test_lsp_session.py /
    test_lsp_fix.py.
    """

    def test_move_is_in_all_tools(self) -> None:
        if "move" not in _ALL_TOOLS:
            self.skipTest(_MOVE_REGISTRY_MSG)
        self.assertIn("move", _ALL_TOOLS)

    def test_move_registered_function_matches_module_attr(self) -> None:
        if "move" not in _ALL_TOOLS:
            self.skipTest(_MOVE_REGISTRY_MSG)
        func, _method = _ALL_TOOLS["move"]
        self.assertIs(
            func,
            getattr(_server, "lsp_move", None),
            "_ALL_TOOLS['move'] must be the public lsp_move - a "
            "divergence here means the tool registers a stale alias and "
            "_wrap_with_header attaches the wrong header to results.",
        )

    def test_move_capability_is_will_rename_files(self) -> None:
        if "move" not in TOOL_CAPABILITIES:
            self.skipTest(_MOVE_REGISTRY_MSG)
        # Same backend gating both raw tools used. None would always-
        # enable the tool against servers that lack willRename; a wrong
        # key would silently disable it everywhere.
        self.assertEqual(
            TOOL_CAPABILITIES["move"],
            "workspace.fileOperations.willRename",
        )

    def test_move_method_label_is_non_empty_string(self) -> None:
        if "move" not in _ALL_TOOLS:
            self.skipTest(_MOVE_REGISTRY_MSG)
        _func, method = _ALL_TOOLS["move"]
        # The header line ([method]) is what the agent sees on every call;
        # an empty label would render as "[]" and lose the orientation
        # signal. We don't pin a specific verb (the implementation may
        # choose workspace/willRenameFiles or a synthetic cc-lsp-now/move
        # label) but it must be a non-empty string.
        self.assertIsInstance(method, str)
        self.assertTrue(method, "move method label must be non-empty")


class LspMoveRoutingTests(unittest.TestCase):
    """Behavioural acceptance: ``lsp_move`` forwards to ``_do_move`` with
    the right list of ``(from_path, to_path)`` pairs.

    ``_do_move`` is the existing helper that performs the actual
    ``willRenameFiles`` request, runs the language-aware import rewriter,
    and stages the candidate; it's already exercised by the raw move-file
    plumbing. Re-driving it through the new public surface would just
    duplicate that coverage, so we monkeypatch it to capture the pairs
    forwarded by ``lsp_move`` and assert the routing layer alone.

    ``_resolve_file_path`` is also patched to identity - it raises
    ``ValueError`` on missing files, but the routing contract should
    forward the (resolved) path tuples regardless of whether the path
    happens to exist on the test machine.
    """

    def setUp(self) -> None:
        self._saved_do_move: Any = getattr(_server, "_do_move", None)
        self._saved_resolve_file_path: Any = getattr(_server, "_resolve_file_path", None)
        self._saved_resolve_symbol_to_file: Any = getattr(
            _server, "_resolve_symbol_to_file", None
        )

        self._captured_pairs: list[tuple[str, str]] | None = None

        async def fake_do_move(files: list[tuple[str, str]]) -> str:
            self._captured_pairs = list(files)
            return "Preview: 0 file(s), 0 edit(s). (test stub)"

        # Identity resolver - bypass on-disk existence check so we can
        # assert the routing layer with synthetic paths.
        def fake_resolve_file_path(path: str, **_kwargs: Any) -> str:
            return path

        setattr(_server, "_do_move", fake_do_move)
        setattr(_server, "_resolve_file_path", fake_resolve_file_path)

    def tearDown(self) -> None:
        if self._saved_do_move is not None:
            setattr(_server, "_do_move", self._saved_do_move)
        if self._saved_resolve_file_path is not None:
            setattr(_server, "_resolve_file_path", self._saved_resolve_file_path)
        if self._saved_resolve_symbol_to_file is not None:
            setattr(
                _server,
                "_resolve_symbol_to_file",
                self._saved_resolve_symbol_to_file,
            )

    def test_single_from_to_routes_one_pair_to_do_move(self) -> None:
        if not _has_move():
            self.skipTest(_MOVE_HOOK_MSG)
        result = _run(
            _move_attr()(from_path="src/old.py", to_path="src/new.py")
        )
        self.assertEqual(
            self._captured_pairs,
            [("src/old.py", "src/new.py")],
            "lsp_move(from_path=..., to_path=...) must call _do_move "
            f"with a one-pair list; got {self._captured_pairs!r} "
            f"(_do_move return: {result!r})",
        )

    def test_batch_moves_string_parses_into_multiple_pairs(self) -> None:
        if not _has_move():
            self.skipTest(_MOVE_HOOK_MSG)
        # Agent-facing notation: ``=>`` between from/to, comma between
        # pairs (chosen over ``->`` because it's harder to confuse with
        # method-arrow syntax in code paths that already contain
        # arrows, and the error string the source emits names ``=>``
        # explicitly: "expected 'from=>to'"). The error message is the
        # contract surface here - if it drifts, agents lose self-correction.
        result = _run(
            _move_attr()(moves="src/a.py=>src/b.py,src/c.py=>src/d.py")
        )
        self.assertIsNotNone(
            self._captured_pairs,
            "lsp_move(moves=...) did not call _do_move at all - batch "
            "parsing missing or short-circuited. Expected '=>'-"
            "separated pairs joined by commas; "
            f"_do_move return: {result!r}",
        )
        self.assertEqual(
            self._captured_pairs,
            [("src/a.py", "src/b.py"), ("src/c.py", "src/d.py")],
            "lsp_move(moves='a=>b,c=>d') must parse into multiple "
            f"(from, to) tuples for _do_move; got {self._captured_pairs!r}",
        )

    def test_symbol_with_to_path_resolves_source_before_moving(self) -> None:
        if not _has_move():
            self.skipTest(_MOVE_HOOK_MSG)

        captured_symbol: list[str] = []

        async def fake_resolve_symbol(name: str) -> str:
            captured_symbol.append(name)
            return f"/abs/resolved/{name}.py"

        setattr(_server, "_resolve_symbol_to_file", fake_resolve_symbol)

        result = _run(
            _move_attr()(symbol="MyClass", to_path="dst/MyClass.py")
        )
        self.assertEqual(
            captured_symbol,
            ["MyClass"],
            "lsp_move(symbol=...) must call _resolve_symbol_to_file "
            "with the symbol name to resolve the source path before "
            f"moving; got calls {captured_symbol!r} (result: {result!r})",
        )
        self.assertEqual(
            self._captured_pairs,
            [("/abs/resolved/MyClass.py", "dst/MyClass.py")],
            "lsp_move(symbol=..., to_path=...) must forward "
            "(resolved_from, to_path) to _do_move; got "
            f"{self._captured_pairs!r}",
        )


if __name__ == "__main__":
    unittest.main()
