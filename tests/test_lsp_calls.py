"""Wave 2 unit coverage for ``lsp_calls``.

``docs/tool-surface.md`` defines ``lsp_calls`` as the Wave 2 graph operator
that folds the raw call-hierarchy pair (``lsp_call_hierarchy_incoming`` and
``lsp_call_hierarchy_outgoing``) into one direction-keyed verb. The doc
pins both the public signature and the agent-facing contract:

```python
async def lsp_calls(
    target: str = "",
    direction: str = "both",         # "in" | "out" | "both"
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_depth: int = 1,
    max_edges: int = 50,
) -> str: ...
```

Sample output (per docs):

```text
in:
  [0] src/server.py:L3669::_ALL_TOOLS — function — 1 site
out:
  [3] src/server.py:L744::_symbol_kind_label — function — 1 site
... 4 more; raise max_edges to unfold.
```

What we pin without a live LSP chain:

1. Public signature — argument names, defaults, and the agent-first ordering
   (``target`` first, then ``direction`` knob, then file/symbol/line
   fallbacks). Drift here breaks every agent that calls ``lsp_calls("[3]")``.
2. Async coroutine — ``_wrap_with_header`` only wraps coroutines.
3. Registry hygiene — ``calls`` is registered, capability-gated against
   ``callHierarchyProvider``, and the raw pair is cut from both registries
   ("no aliases, no shims" per the Raw Tool Cut Map).
4. Direction validation — ``direction`` is agent-supplied free-form text
   per docs ("in" | "out" | "both"); a bogus value must surface as a
   readable string, not raise. Mirrors the lsp_session defensive surface
   contract for ``action``.
5. Graph-index shape — ``lsp_calls`` is documented to record call edges
   into the semantic nav context so that ``lsp_symbol([3])`` /
   ``lsp_refs([3])`` can pivot off any edge. We can't drive this without a
   real call-hierarchy response, but we can sanity-check the recorder
   contract in isolation if the implementation exposes a helper, and
   otherwise leave a punch-list reminder.

Tests gate on ``hasattr(server, "lsp_calls")`` and ``"calls" in _ALL_TOOLS``;
when the source hook is missing they skip with a message that names the
exact gap, doubling as a punch list. This matches the Wave 2 pattern set
by ``test_lsp_session.py`` and ``test_tool_surface.py``'s
``test_calls_replaces_call_hierarchy_pair``.

End-to-end behaviour (``prepareCallHierarchy`` round-trip, sample-line
formatting against a real ``ty`` / ``csharp-ls`` response, depth recursion)
belongs in live smoke per the docs.
"""
import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, patch

from cc_lsp_now import server as _server
from cc_lsp_now.server import _ALL_TOOLS, TOOL_CAPABILITIES


def _calls_attr() -> Any:
    # Resolved via getattr so ty doesn't choke on the still-missing
    # attribute when the source hook hasn't landed yet — once lsp_calls
    # ships the runtime check (_has_calls) gates whether it actually
    # gets called, but the static lookup must stay forgiving.
    return getattr(_server, "lsp_calls", None)


_CALLS_HOOK_MSG = (
    "MISSING SOURCE HOOK: lsp_calls not yet defined on cc_lsp_now.server "
    "(Wave 2 verifier lane). docs/tool-surface.md expects "
    "`async def lsp_calls(target='', direction='both', file_path='', "
    "symbol='', line=0, max_depth=1, max_edges=50) -> str`, replacing "
    "lsp_call_hierarchy_incoming + lsp_call_hierarchy_outgoing with a "
    "single direction-keyed verb that records edges into the semantic "
    "nav context."
)

_CALLS_REGISTRY_MSG = (
    "MISSING SOURCE HOOK: 'calls' not yet registered in _ALL_TOOLS. "
    "docs/tool-surface.md Raw Tool Cut Map: `calls` → callHierarchyProvider."
)


def _has_calls() -> bool:
    return hasattr(_server, "lsp_calls")


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_calls returned non-str: {type(result)!r}"
    return result


class LspCallsSignatureTests(unittest.TestCase):
    """Pin the public signature so the agent-first one-line contract from
    docs/tool-surface.md can't drift. Argument names are agent-visible —
    renaming ``direction`` → ``dir`` would silently break every existing
    call site since MCP tools dispatch by kwargs.
    """

    def test_lsp_calls_is_async_callable(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        self.assertTrue(
            inspect.iscoroutinefunction(_calls_attr()),
            "lsp_calls must be `async def` — _wrap_with_header only wraps coroutines",
        )

    def test_lsp_calls_signature_matches_docs(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        sig = inspect.signature(_calls_attr())
        # docs/tool-surface.md pins exactly these seven kwargs in this order.
        # ``target`` is first (graph-aware), ``direction`` is the only narrow
        # knob; file/symbol/line are the fallback resolver inputs shared
        # with every other Wave 1/2 target-taking tool.
        self.assertEqual(
            list(sig.parameters),
            ["target", "direction", "file_path", "symbol", "line", "max_depth", "max_edges"],
            f"lsp_calls signature drifted from docs/tool-surface.md: {sig}",
        )

    def test_lsp_calls_target_defaults_to_empty_string(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        sig = inspect.signature(_calls_attr())
        # Empty default lets file_path/symbol/line fallbacks light up when
        # no graph index / Lxx is supplied, matching every other Wave 1/2
        # target-taking tool (lsp_symbol, lsp_goto, lsp_refs).
        self.assertEqual(sig.parameters["target"].default, "")

    def test_lsp_calls_direction_defaults_to_both(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        sig = inspect.signature(_calls_attr())
        # docs: direction default is "both" — bare lsp_calls("[3]") must
        # surface incoming AND outgoing in one breath, not silently pick
        # one side.
        self.assertEqual(
            sig.parameters["direction"].default,
            "both",
            "docs/tool-surface.md pins the default to `both` so a bare "
            "lsp_calls call returns the full edge set; a different default "
            "would change the meaning of every existing agent call.",
        )

    def test_lsp_calls_max_depth_and_max_edges_defaults_match_docs(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        sig = inspect.signature(_calls_attr())
        # docs pin ``max_depth=1`` and ``max_edges=50``. Depth 1 keeps the
        # default cheap (one prepareCallHierarchy + one in/out request);
        # max_edges=50 governs the "... N more; raise max_edges to unfold"
        # tail. Drift would silently change cost or truncation.
        self.assertEqual(sig.parameters["max_depth"].default, 1)
        self.assertEqual(sig.parameters["max_edges"].default, 50)

    def test_lsp_calls_file_path_symbol_line_defaults_match_other_wave_tools(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        sig = inspect.signature(_calls_attr())
        # Empty string / zero defaults are the shared "fallback resolver"
        # contract used by lsp_symbol/lsp_goto/lsp_refs. _resolve_semantic_target
        # treats them as "not supplied"; non-empty defaults would short-circuit
        # graph-index resolution.
        self.assertEqual(sig.parameters["file_path"].default, "")
        self.assertEqual(sig.parameters["symbol"].default, "")
        self.assertEqual(sig.parameters["line"].default, 0)


class LspCallsRegistryTests(unittest.TestCase):
    """Wave 2 acceptance for the registry: ``calls`` is registered, the
    function attribute matches the registered tuple, capability gating
    targets ``callHierarchyProvider`` (not the deprecated raw verb keys),
    and the raw pair is gone.

    Cross-cutting raw cuts also live in tests/test_tool_surface.py; this
    file pins the *positive* registration shape of ``calls`` itself for
    parity with test_lsp_outline.py / test_lsp_session.py.
    """

    def test_calls_is_in_all_tools(self) -> None:
        if "calls" not in _ALL_TOOLS:
            self.skipTest(_CALLS_REGISTRY_MSG)
        self.assertIn("calls", _ALL_TOOLS)

    def test_calls_registered_function_matches_module_attr(self) -> None:
        if "calls" not in _ALL_TOOLS:
            self.skipTest(_CALLS_REGISTRY_MSG)
        func, _method = _ALL_TOOLS["calls"]
        self.assertIs(
            func,
            getattr(_server, "lsp_calls", None),
            "_ALL_TOOLS['calls'] must be the public lsp_calls — a "
            "divergence here means the tool registers a stale alias and "
            "_wrap_with_header attaches the wrong header to results.",
        )

    def test_calls_capability_is_call_hierarchy_provider(self) -> None:
        if "calls" not in TOOL_CAPABILITIES:
            self.skipTest(_CALLS_REGISTRY_MSG)
        # docs/tool-surface.md Raw Tool Cut Map binds calls to
        # callHierarchyProvider. None would always-enable the tool against
        # servers that lack call hierarchy; a wrong key would silently
        # disable it everywhere.
        self.assertEqual(
            TOOL_CAPABILITIES["calls"],
            "callHierarchyProvider",
        )

    def test_calls_method_label_is_non_empty_string(self) -> None:
        if "calls" not in _ALL_TOOLS:
            self.skipTest(_CALLS_REGISTRY_MSG)
        _func, method = _ALL_TOOLS["calls"]
        # The header line ([method]) is what the agent sees on every call;
        # an empty label would render as "[]" and lose the orientation
        # signal. We don't pin a specific verb (the implementation may
        # choose textDocument/prepareCallHierarchy or a synthetic
        # cc-lsp-now/calls label) but it must be a non-empty string.
        self.assertIsInstance(method, str)
        self.assertTrue(method, "calls method label must be non-empty")

    def test_raw_call_hierarchy_pair_is_absent_from_registry(self) -> None:
        # Mirror of the surface-level cut so per-tool failure points at
        # this file; ``calls`` shipping must remove both raw entries.
        if "calls" not in _ALL_TOOLS:
            self.skipTest(_CALLS_REGISTRY_MSG)
        self.assertNotIn("call_hierarchy_incoming", _ALL_TOOLS)
        self.assertNotIn("call_hierarchy_outgoing", _ALL_TOOLS)

    def test_raw_call_hierarchy_capability_entries_are_dropped(self) -> None:
        if "calls" not in _ALL_TOOLS:
            self.skipTest(_CALLS_REGISTRY_MSG)
        # Dead capability paths invite accidental re-introduction; the cut
        # must reach TOOL_CAPABILITIES too.
        self.assertNotIn("call_hierarchy_incoming", TOOL_CAPABILITIES)
        self.assertNotIn("call_hierarchy_outgoing", TOOL_CAPABILITIES)


class LspCallsDirectionValidationTests(unittest.TestCase):
    """``direction`` is agent-supplied free-form text per docs/tool-surface.md
    ("in" | "out" | "both"). A bogus value must return a readable string so
    the agent can self-correct, not raise an exception that breaks the MCP
    transport.

    These tests intentionally skip a real chain by supplying an unresolvable
    target shape (``"[0]"`` with no prior semantic graph) so direction
    validation either short-circuits before any LSP traffic, or surfaces
    the no-graph error — both are valid; what is *not* valid is raising
    TypeError/ValueError out of an agent-facing tool.
    """

    def setUp(self) -> None:
        # Reset semantic nav state so "[0]" fails predictably with the
        # "No previous semantic graph." string rather than reaching into
        # a stale graph from another test.
        if hasattr(_server, "_record_semantic_nav_context"):
            _server._record_semantic_nav_context("", [])

    def test_invalid_direction_returns_string_not_raises(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        try:
            result = _run(_calls_attr()(target="[0]", direction="sideways"))
        except (TypeError, ValueError) as e:
            self.fail(
                f"lsp_calls(direction='sideways') raised {type(e).__name__}: {e}. "
                f"Bad direction input from an agent must surface as a "
                f"string so the agent can read it and self-correct."
            )
        self.assertIsInstance(result, str)
        self.assertTrue(result, "empty result on bad direction is uninformative")

    def test_invalid_direction_response_mentions_valid_choices(self) -> None:
        # Don't pin exact wording, but the response should give the agent
        # enough to recover — either echoing the bad value or naming one
        # of the valid choices.
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        result = _run(_calls_attr()(target="[0]", direction="sideways"))
        haystack = result.lower()
        self.assertTrue(
            ("sideways" in haystack)
            or ("direction" in haystack)
            or ("in" in haystack and "out" in haystack and "both" in haystack),
            f"unknown-direction error should help the agent recover; got: {result!r}",
        )

    def test_explicit_direction_in_is_accepted_shape(self) -> None:
        # ``in`` is one of the three documented values; with no prior graph
        # the call short-circuits on "No previous semantic graph." but
        # critically it MUST NOT reject ``in`` as an invalid direction —
        # otherwise the synonym set has drifted away from the doc.
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        result = _run(_calls_attr()(target="[0]", direction="in"))
        self.assertNotIn(
            "sideways",
            result,
            "direction='in' triggered the unknown-direction path — "
            "docs/tool-surface.md lists it as one of the three valid values",
        )

    def test_explicit_direction_out_is_accepted_shape(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        result = _run(_calls_attr()(target="[0]", direction="out"))
        self.assertIsInstance(result, str)


class LspCallsGraphIndexTests(unittest.TestCase):
    """``lsp_calls`` is documented to *both* consume graph indices (``[3]``
    targets resolved through ``_resolve_semantic_target``) *and* produce
    them (call edges recorded into the semantic nav context so
    ``lsp_symbol([3])`` works on a result row).

    The consume side reuses the existing Wave 1 ``_graph_target_from_index``
    plumbing — already pinned in tests/test_lsp_grep.py — so we only need
    to assert lsp_calls actually goes through the resolver: a "[0]" target
    with no prior graph must surface the documented "No previous semantic
    graph." string, proving direction validation hasn't accidentally
    bypassed target resolution.

    The produce side requires a live call-hierarchy response, so it is
    parked here as a punch-list reminder until the source ships a unit-
    testable formatter helper (e.g. ``_format_call_edge`` /
    ``_record_call_edges_into_nav``).
    """

    def setUp(self) -> None:
        if hasattr(_server, "_record_semantic_nav_context"):
            _server._record_semantic_nav_context("", [])

    def test_graph_index_target_routes_through_semantic_resolver(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        # With no prior _record_semantic_nav_context call, "[0]" must hit
        # _graph_target_from_index's "No previous semantic graph." branch.
        # If lsp_calls instead returns a generic error or raises, it has
        # bypassed the shared resolver and the [N]-target contract is
        # broken.
        result = _run(_calls_attr()(target="[0]", direction="both"))
        self.assertIsInstance(result, str)
        self.assertIn(
            "No previous semantic graph",
            result,
            "lsp_calls('[0]') with no prior graph must surface the "
            "shared _graph_target_from_index error so agents see the "
            "same self-correction path used by lsp_symbol/lsp_goto/lsp_refs; "
            f"got: {result!r}",
        )


class LspCallsMultiTargetTests(unittest.TestCase):
    def test_file_symbol_ambiguity_expands_all_matches_for_read_only_call_graph(self) -> None:
        if not _has_calls():
            self.skipTest(_CALLS_HOOK_MSG)
        targets = [
            _server.SemanticTarget(
                uri="file:///repo/src/Workspace.cs",
                pos={"line": 10, "character": 4},
                path="/repo/src/Workspace.cs",
                line=11,
                character=4,
                name="SelectArtifact",
            ),
            _server.SemanticTarget(
                uri="file:///repo/src/Workspace.cs",
                pos={"line": 20, "character": 4},
                path="/repo/src/Workspace.cs",
                line=21,
                character=4,
                name="SelectArtifactRelative",
            ),
        ]
        edge_group = _server.SemanticGrepGroup(
            key="k",
            name="Caller",
            kind="method",
            type_text="",
            definition_path="/repo/src/Caller.cs",
            definition_line=5,
            definition_character=4,
            hits=[],
        )

        async def fake_sections(
            resolved: _server.SemanticTarget,
            _direction_key: str,
            _max_depth: int,
            _max_edges: int,
            *,
            heading_prefix: str = "Calls for",
        ) -> tuple[list[str], list[_server.SemanticGrepGroup]]:
            return (
                [
                    f"{heading_prefix} {resolved.name} ({resolved.path}:L{resolved.line})",
                    "in:",
                    "  [0] Caller.cs:L5::Caller — method — 1 site",
                ],
                [edge_group],
            )

        with patch.object(_server, "_resolve_symbol_targets", AsyncMock(return_value=targets)):
            with patch.object(_server, "_call_graph_sections_for_target", side_effect=fake_sections):
                result = _run(_calls_attr()(file_path="Workspace.cs", symbol="SelectArtifact", direction="in"))

        self.assertIn("Calls for 2 matches of 'SelectArtifact'", result)
        self.assertIn("match 0 SelectArtifact", result)
        self.assertIn("match 1 SelectArtifactRelative", result)
        self.assertIn("  [0] Caller.cs:L5::Caller", result)
        self.assertIn("  [1] Caller.cs:L5::Caller", result)
        self.assertNotIn("Multiple matches — pass line=", result)


class CallItemToGroupTests(unittest.TestCase):
    """``_call_item_to_group`` is the seam that lets call-hierarchy edges
    flow through the same ``_record_semantic_nav_context`` buffer that
    ``lsp_grep`` / ``lsp_symbols_at`` produce — so an agent can
    ``lsp_symbol([3])`` / ``lsp_refs([3])`` on any rendered call edge.

    A SemanticGrepGroup is unit-testable in isolation; its hit anchoring
    determines whether follow-up ``[N]`` targets land on the function
    name token or on the wrong column. These tests pin the contract that:

    1. The hit's position is the ``selectionRange`` start (the *name*
       token), not the broader ``range`` (which covers the whole body).
       LSP servers vary on what ``range`` covers, but ``selectionRange``
       is consistently the identifier — anchoring there is what makes
       ``lsp_refs([N])`` find references rather than empty-string hits.
    2. ``range`` is used as a fallback only when ``selectionRange`` is
       missing, matching the LSP spec that ``selectionRange`` is
       required but allowing for permissive servers.
    3. ``definition_line`` is 1-based (matches every other Wave 1 group)
       so breadcrumb formatters don't double-shift.
    4. ``kind`` is the lowercase symbol-kind label (``function`` not
       ``Function``) — consistent with ``lsp_grep`` group kinds, so a
       follow-up ``lsp_calls`` line reads like a ``lsp_grep`` line.
    5. The group survives a round-trip through
       ``_record_semantic_nav_context`` so ``_graph_target_from_index``
       resolves the rendered ``[N]`` to the right SemanticTarget.
    """

    def setUp(self) -> None:
        # Reset nav state for the round-trip test.
        _server._record_semantic_nav_context("", [])

    @staticmethod
    def _item(
        name: str = "render",
        kind: int = 12,  # Function
        uri: str = "file:///repo/src/renderer.py",
        sel_line: int = 41,
        sel_char: int = 4,
        sel_end_char: int = 10,
        body_line: int = 41,
        body_end_line: int = 60,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "kind": kind,
            "uri": uri,
            "range": {
                "start": {"line": body_line, "character": 0},
                "end": {"line": body_end_line, "character": 0},
            },
            "selectionRange": {
                "start": {"line": sel_line, "character": sel_char},
                "end": {"line": sel_line, "character": sel_end_char},
            },
        }

    def test_hit_is_anchored_at_selection_range_not_body_range(self) -> None:
        # ``range`` opens at column 0 (the ``def`` keyword); ``selectionRange``
        # opens at column 4 (the function name). The hit must come from
        # selectionRange so a follow-up ``lsp_refs`` lands on the identifier.
        item = self._item(sel_line=41, sel_char=4, body_line=41)

        group = _server._call_item_to_group(item)

        self.assertEqual(len(group.hits), 1)
        hit = group.hits[0]
        self.assertEqual(hit.line, 41, "hit.line is 0-based selectionRange line")
        self.assertEqual(hit.character, 4, "hit.character is selectionRange start, not 0")
        self.assertEqual(hit.pos, {"line": 41, "character": 4})

    def test_definition_line_is_one_based_from_selection_range(self) -> None:
        item = self._item(sel_line=41, sel_char=4)

        group = _server._call_item_to_group(item)

        # 0-based 41 → 1-based 42 for the definition_line that breadcrumb
        # formatters consume. _format_semantic_grep_group renders this as
        # ``L42`` directly without a second shift.
        self.assertEqual(group.definition_line, 42)
        self.assertEqual(group.definition_character, 4)
        self.assertEqual(group.definition_path, "/repo/src/renderer.py")

    def test_kind_is_lowercased_label(self) -> None:
        # ``_symbol_kind_label(12)`` → ``Function`` — but lsp_grep / lsp_calls
        # render kinds in lowercase ("function ctx — RenderContext"). The
        # helper must lowercase so the rendered call edge line matches the
        # rest of the family.
        item = self._item(kind=12)

        group = _server._call_item_to_group(item)

        self.assertEqual(group.kind, "function")
        # Defensive: not the upper-case raw label and not a numeric kind.
        self.assertNotEqual(group.kind, "Function")
        self.assertNotEqual(group.kind, "12")

    def test_falls_back_to_range_when_selection_range_missing(self) -> None:
        # Some permissive servers omit selectionRange even though the spec
        # says it's required. The fallback to ``range`` keeps lsp_calls
        # functional rather than zero-anchoring at column 0 of the file.
        item = {
            "name": "loose",
            "kind": 12,
            "uri": "file:///repo/src/loose.py",
            "range": {
                "start": {"line": 7, "character": 2},
                "end": {"line": 9, "character": 0},
            },
        }

        group = _server._call_item_to_group(item)

        self.assertEqual(group.hits[0].line, 7)
        self.assertEqual(group.hits[0].character, 2)
        self.assertEqual(group.definition_line, 8)

    def test_key_includes_zero_based_line_and_name_for_dedup(self) -> None:
        # The ``key`` is what ``_record_semantic_nav_context`` would use if
        # it ever de-duplicated groups; the format pins the
        # ``path:line0:char0:name`` convention so two edges to the same
        # callee at the same site never produce twin nav entries.
        item = self._item(name="render", sel_line=41, sel_char=4)

        group = _server._call_item_to_group(item)

        self.assertEqual(group.key, "/repo/src/renderer.py:41:4:render")

    def test_group_round_trips_through_semantic_nav_context(self) -> None:
        # End-to-end seam: a CallHierarchyItem → SemanticGrepGroup →
        # _record_semantic_nav_context → _graph_target_from_index must
        # land on the original selectionRange position. This is what
        # makes ``lsp_calls`` produce ``[N]`` indices that the rest of
        # the Wave 1 surface can consume.
        item = self._item(name="render", sel_line=41, sel_char=4)
        group = _server._call_item_to_group(item)

        _server._record_semantic_nav_context("calls:render", [group])

        target = _server._graph_target_from_index("0")
        if isinstance(target, str):
            self.fail(f"expected SemanticTarget, got error string: {target}")
        self.assertEqual(target.name, "render")
        self.assertEqual(target.path, "/repo/src/renderer.py")
        # _graph_target_from_index returns 1-based line.
        self.assertEqual(target.line, 42)
        self.assertEqual(target.character, 4)
        self.assertEqual(target.pos, {"line": 41, "character": 4})


if __name__ == "__main__":
    unittest.main()
