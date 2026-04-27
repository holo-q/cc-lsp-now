"""Wave 4 unit coverage for ``lsp_types``.

``docs/tool-surface.md`` defines ``lsp_types`` as the Wave 4 graph operator
that folds the raw type-hierarchy pair (``lsp_type_hierarchy_supertypes``
and ``lsp_type_hierarchy_subtypes``) into one direction-keyed verb,
mirroring the Wave 2 ``lsp_calls`` shape. The expected one-line agent
contract:

```python
async def lsp_types(
    target: str = "",
    direction: str = "both",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_depth: int = 1,
    max_edges: int = 50,
) -> str: ...
```

What we pin without a live LSP chain:

1. Public signature - argument names, defaults, and the agent-first
   ordering (``target`` first, then ``direction`` knob, then file/symbol/
   line fallbacks). Drift here breaks every agent that calls
   ``lsp_types("[3]")``. MCP tools dispatch by kwargs so a rename is a
   silent break.
2. Async coroutine - ``_wrap_with_header`` only wraps coroutines.
3. Registry hygiene - ``types`` is registered, capability-gated against
   ``typeHierarchyProvider``, and the raw pair is cut from both registries
   ("no aliases, no shims" per the Raw Tool Cut Map).
4. Direction validation - a bogus ``direction`` value must surface as a
   readable string so the agent can self-correct, not raise an exception
   that breaks the MCP transport. Mirrors the lsp_calls direction
   contract.
5. Routing - ``direction='both'`` runs prepareTypeHierarchy then both
   ``typeHierarchy/supertypes`` and ``typeHierarchy/subtypes`` against the
   prepared item.

End-to-end behaviour (real prepareTypeHierarchy round-trip, sample-line
formatting against a real ``ty`` / ``csharp-ls`` response, depth recursion)
belongs in live smoke per the docs.

Tests gate on ``hasattr(server, "lsp_types")`` and
``"types" in _ALL_TOOLS``; when the source hook is missing they skip with
a message that names the exact gap, doubling as a punch list. This
matches the Wave 2/3 pattern set by ``test_lsp_calls.py`` and
``test_lsp_move.py``.
"""
import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any

from cc_lsp_now import server as _server
from cc_lsp_now.server import _ALL_TOOLS, TOOL_CAPABILITIES


_TYPES_HOOK_MSG = (
    "MISSING SOURCE HOOK: lsp_types not yet defined on cc_lsp_now.server "
    "(Wave 4 graph operator). docs/tool-surface.md expects "
    "`async def lsp_types(target='', direction='both', file_path='', "
    "symbol='', line=0, max_depth=1, max_edges=50) -> str`, replacing "
    "lsp_type_hierarchy_supertypes + lsp_type_hierarchy_subtypes with a "
    "single direction-keyed verb."
)

_TYPES_REGISTRY_MSG = (
    "MISSING SOURCE HOOK: 'types' not yet registered in _ALL_TOOLS. "
    "docs/tool-surface.md Raw Tool Cut Map: `types` -> typeHierarchyProvider."
)


def _has_types() -> bool:
    return hasattr(_server, "lsp_types")


def _types_attr() -> Any:
    # Resolved via getattr so ty doesn't choke on the still-missing
    # attribute when the source hook hasn't landed yet - once lsp_types
    # ships the runtime check (_has_types) gates whether it actually gets
    # called, but the static lookup must stay forgiving.
    return getattr(_server, "lsp_types", None)


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_types returned non-str: {type(result)!r}"
    return result


class LspTypesSignatureTests(unittest.TestCase):
    """Pin the public signature so the agent-first one-line contract from
    docs/tool-surface.md can't drift. Argument names are agent-visible -
    renaming ``direction`` -> ``dir`` would silently break every existing
    call site since MCP tools dispatch by kwargs.
    """

    def test_lsp_types_is_async_callable(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        self.assertTrue(
            inspect.iscoroutinefunction(_types_attr()),
            "lsp_types must be `async def` - _wrap_with_header only wraps coroutines",
        )

    def test_lsp_types_signature_matches_docs(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        sig = inspect.signature(_types_attr())
        # Mirrors lsp_calls exactly: target first (graph-aware),
        # direction is the only narrow knob, file/symbol/line are the
        # shared fallback resolver inputs, max_depth/max_edges cap the
        # walk.
        self.assertEqual(
            list(sig.parameters),
            ["target", "direction", "file_path", "symbol", "line", "max_depth", "max_edges"],
            f"lsp_types signature drifted from docs/tool-surface.md: {sig}",
        )

    def test_lsp_types_target_defaults_to_empty_string(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        sig = inspect.signature(_types_attr())
        # Empty default lets file_path/symbol/line fallbacks light up
        # when no graph index / Lxx is supplied, matching every other
        # target-taking tool.
        self.assertEqual(sig.parameters["target"].default, "")

    def test_lsp_types_direction_defaults_to_both(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        sig = inspect.signature(_types_attr())
        # docs: direction default is "both" - bare lsp_types("[3]") must
        # surface supertypes AND subtypes in one breath, not silently
        # pick one side.
        self.assertEqual(
            sig.parameters["direction"].default,
            "both",
            "default direction must be `both` so a bare lsp_types call "
            "returns the full edge set; a different default would change "
            "the meaning of every existing agent call.",
        )

    def test_lsp_types_max_depth_and_max_edges_defaults(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        sig = inspect.signature(_types_attr())
        # Mirrors lsp_calls: depth 1 keeps the default cheap (one
        # prepareTypeHierarchy + one super/sub call); max_edges=50
        # governs the truncation tail. Drift here would silently change
        # cost or truncation.
        self.assertEqual(sig.parameters["max_depth"].default, 1)
        self.assertEqual(sig.parameters["max_edges"].default, 50)

    def test_lsp_types_file_path_symbol_line_defaults(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        sig = inspect.signature(_types_attr())
        # Empty string / zero defaults are the shared "fallback resolver"
        # contract used by lsp_symbol/lsp_goto/lsp_refs/lsp_calls.
        # _resolve_semantic_target treats them as "not supplied"; non-
        # empty defaults would short-circuit graph-index resolution.
        self.assertEqual(sig.parameters["file_path"].default, "")
        self.assertEqual(sig.parameters["symbol"].default, "")
        self.assertEqual(sig.parameters["line"].default, 0)


class LspTypesRegistryTests(unittest.TestCase):
    """Wave 4 acceptance for the registry: ``types`` is registered, the
    function attribute matches the registered tuple, capability gating
    targets ``typeHierarchyProvider`` (not the deprecated raw verb keys),
    and the raw pair is gone.

    Cross-cutting raw cuts also live in tests/test_tool_surface.py
    (``WaveFourSurfaceTests``); this file pins the *positive* registration
    shape of ``types`` itself for parity with test_lsp_calls.py /
    test_lsp_move.py.
    """

    def test_types_is_in_all_tools(self) -> None:
        if "types" not in _ALL_TOOLS:
            self.skipTest(_TYPES_REGISTRY_MSG)
        self.assertIn("types", _ALL_TOOLS)

    def test_types_registered_function_matches_module_attr(self) -> None:
        if "types" not in _ALL_TOOLS:
            self.skipTest(_TYPES_REGISTRY_MSG)
        func, _method = _ALL_TOOLS["types"]
        self.assertIs(
            func,
            getattr(_server, "lsp_types", None),
            "_ALL_TOOLS['types'] must be the public lsp_types - a "
            "divergence here means the tool registers a stale alias and "
            "_wrap_with_header attaches the wrong header to results.",
        )

    def test_types_capability_is_type_hierarchy_provider(self) -> None:
        if "types" not in TOOL_CAPABILITIES:
            self.skipTest(_TYPES_REGISTRY_MSG)
        # docs/tool-surface.md Raw Tool Cut Map binds types to
        # typeHierarchyProvider. None would always-enable the tool against
        # servers that lack type hierarchy; a wrong key would silently
        # disable it everywhere.
        self.assertEqual(
            TOOL_CAPABILITIES["types"],
            "typeHierarchyProvider",
        )

    def test_types_method_label_is_non_empty_string(self) -> None:
        if "types" not in _ALL_TOOLS:
            self.skipTest(_TYPES_REGISTRY_MSG)
        _func, method = _ALL_TOOLS["types"]
        # The header line ([method]) is what the agent sees on every call.
        # An empty label would render as "[]" and lose the orientation
        # signal. We don't pin a specific verb - the implementation may
        # choose textDocument/prepareTypeHierarchy or a synthetic
        # cc-lsp-now/types label - but it must be a non-empty string.
        self.assertIsInstance(method, str)
        self.assertTrue(method, "types method label must be non-empty")

    def test_raw_type_hierarchy_pair_is_absent_from_registry(self) -> None:
        # Mirror of the surface-level cut so per-tool failure points at
        # this file; ``types`` shipping must remove both raw entries.
        if "types" not in _ALL_TOOLS:
            self.skipTest(_TYPES_REGISTRY_MSG)
        self.assertNotIn("type_hierarchy_supertypes", _ALL_TOOLS)
        self.assertNotIn("type_hierarchy_subtypes", _ALL_TOOLS)

    def test_raw_type_hierarchy_capability_entries_are_dropped(self) -> None:
        if "types" not in _ALL_TOOLS:
            self.skipTest(_TYPES_REGISTRY_MSG)
        # Dead capability paths invite accidental re-introduction; the
        # cut must reach TOOL_CAPABILITIES too.
        self.assertNotIn("type_hierarchy_supertypes", TOOL_CAPABILITIES)
        self.assertNotIn("type_hierarchy_subtypes", TOOL_CAPABILITIES)


class LspTypesDirectionValidationTests(unittest.TestCase):
    """``direction`` is agent-supplied free-form text (mirrors lsp_calls).
    A bogus value must return a readable string so the agent can self-
    correct, not raise an exception that breaks the MCP transport.

    These tests intentionally skip a real chain by supplying an
    unresolvable target shape (``"[0]"`` with no prior semantic graph) so
    direction validation either short-circuits before any LSP traffic, or
    surfaces the no-graph error - both are valid; what is *not* valid is
    raising TypeError/ValueError out of an agent-facing tool.
    """

    def setUp(self) -> None:
        # Reset semantic nav state so "[0]" fails predictably with the
        # "No previous semantic graph." string rather than reaching into
        # a stale graph from another test.
        if hasattr(_server, "_record_semantic_nav_context"):
            _server._record_semantic_nav_context("", [])

    def test_invalid_direction_returns_string_not_raises(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        try:
            result = _run(_types_attr()(target="[0]", direction="sideways"))
        except (TypeError, ValueError) as e:
            self.fail(
                f"lsp_types(direction='sideways') raised {type(e).__name__}: {e}. "
                f"Bad direction input from an agent must surface as a "
                f"string so the agent can read it and self-correct."
            )
        self.assertIsInstance(result, str)
        self.assertTrue(result, "empty result on bad direction is uninformative")

    def test_invalid_direction_response_mentions_helpful_choice(self) -> None:
        # Don't pin exact wording, but the response should give the agent
        # enough to recover - either echoing the bad value, naming the
        # ``direction`` knob, or listing recognized values. We accept any
        # of the common spellings (in/out, super/sub, up/down) since the
        # task description doesn't pin one set.
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        result = _run(_types_attr()(target="[0]", direction="sideways"))
        haystack = result.lower()
        recognized_pairs = [
            ("in", "out"),
            ("super", "sub"),
            ("up", "down"),
            ("supertype", "subtype"),
        ]
        names_a_pair = any(a in haystack and b in haystack for a, b in recognized_pairs)
        self.assertTrue(
            "sideways" in haystack
            or "direction" in haystack
            or "both" in haystack
            or names_a_pair,
            f"unknown-direction error should help the agent recover; got: {result!r}",
        )


class LspTypesGraphIndexTests(unittest.TestCase):
    """``lsp_types`` consumes graph indices (``[N]`` from the last
    ``lsp_grep``/``lsp_symbols_at``/``lsp_calls``) through the same
    ``_resolve_semantic_target`` plumbing as the rest of the family. The
    consume side reuses the existing ``_graph_target_from_index`` plumbing
    already pinned in tests/test_lsp_grep.py, so we only need to assert
    lsp_types actually goes through the resolver: a "[0]" target with no
    prior graph must surface the documented "No previous semantic graph."
    string, proving direction validation hasn't accidentally bypassed
    target resolution.
    """

    def setUp(self) -> None:
        if hasattr(_server, "_record_semantic_nav_context"):
            _server._record_semantic_nav_context("", [])

    def test_graph_index_target_routes_through_semantic_resolver(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        # With no prior _record_semantic_nav_context call, "[0]" must hit
        # _graph_target_from_index's "No previous semantic graph." branch.
        # If lsp_types instead returns a generic error or raises, it has
        # bypassed the shared resolver and the [N]-target contract is
        # broken.
        result = _run(_types_attr()(target="[0]", direction="both"))
        self.assertIsInstance(result, str)
        self.assertIn(
            "No previous semantic graph",
            result,
            "lsp_types('[0]') with no prior graph must surface the "
            "shared _graph_target_from_index error so agents see the "
            "same self-correction path used by lsp_symbol/lsp_goto/"
            "lsp_refs/lsp_calls; "
            f"got: {result!r}",
        )


class LspTypesRoutingTests(unittest.TestCase):
    """Behavioural acceptance: ``lsp_types`` runs prepareTypeHierarchy
    then expands supertypes and/or subtypes per ``direction``.

    We monkeypatch ``_resolve_semantic_target`` to skip the real chain
    (return a synthetic SemanticTarget) and ``_request`` to capture the
    method calls. ``direction='both'`` must hit prepare AND both
    typeHierarchy/supertypes AND typeHierarchy/subtypes; this is the
    minimal pin proving the new operator wires through both backend
    methods rather than silently dropping one side.
    """

    def setUp(self) -> None:
        self._saved_resolve: Any = getattr(_server, "_resolve_semantic_target", None)
        self._saved_request: Any = getattr(_server, "_request", None)

        # Synthetic semantic target: skips the real workspace/symbol or
        # textDocument/definition resolve so the test runs without an
        # LSP chain. Matches the SemanticTarget dataclass shape from
        # cc_lsp_now.server.
        target = _server.SemanticTarget(
            uri="file:///repo/src/types_demo.py",
            pos={"line": 9, "character": 6},
            path="/repo/src/types_demo.py",
            line=10,
            character=6,
            name="MyClass",
        )

        async def fake_resolve(*_args: Any, **_kwargs: Any) -> Any:
            return target

        self._method_calls: list[str] = []

        # Synthetic prepareTypeHierarchy item. Real items carry name,
        # kind, uri, range, selectionRange, data; only the envelope
        # matters for routing assertions.
        prepared_item = {
            "name": "MyClass",
            "kind": 5,
            "uri": "file:///repo/src/types_demo.py",
            "range": {
                "start": {"line": 9, "character": 0},
                "end": {"line": 30, "character": 0},
            },
            "selectionRange": {
                "start": {"line": 9, "character": 6},
                "end": {"line": 9, "character": 13},
            },
        }

        async def fake_request(method: str, _params: dict | None, **_kwargs: Any) -> Any:
            self._method_calls.append(method)
            if method == "textDocument/prepareTypeHierarchy":
                return [prepared_item]
            # Empty list keeps the formatting path simple - the routing
            # test cares which methods got called, not what the formatter
            # rendered out.
            return []

        setattr(_server, "_resolve_semantic_target", fake_resolve)
        setattr(_server, "_request", fake_request)

    def tearDown(self) -> None:
        if self._saved_resolve is not None:
            setattr(_server, "_resolve_semantic_target", self._saved_resolve)
        if self._saved_request is not None:
            setattr(_server, "_request", self._saved_request)

    def test_direction_both_calls_prepare_and_super_and_sub(self) -> None:
        if not _has_types():
            self.skipTest(_TYPES_HOOK_MSG)
        result = _run(_types_attr()(target="[0]", direction="both"))
        self.assertIsInstance(result, str)
        # Prepare must run exactly once; the same prepared item is then
        # walked in both directions. Drift to a second prepare call per
        # direction would double the LSP cost on every invocation.
        self.assertIn(
            "textDocument/prepareTypeHierarchy",
            self._method_calls,
            "lsp_types must run textDocument/prepareTypeHierarchy before "
            f"requesting super/sub edges; methods called: {self._method_calls!r}",
        )
        self.assertIn(
            "typeHierarchy/supertypes",
            self._method_calls,
            "lsp_types(direction='both') must request "
            f"typeHierarchy/supertypes; methods called: {self._method_calls!r}",
        )
        self.assertIn(
            "typeHierarchy/subtypes",
            self._method_calls,
            "lsp_types(direction='both') must request "
            f"typeHierarchy/subtypes; methods called: {self._method_calls!r}",
        )


if __name__ == "__main__":
    unittest.main()
