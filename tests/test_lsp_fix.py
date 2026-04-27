"""Wave 2 unit coverage for ``lsp_fix``.

``docs/tool-surface.md`` defines ``lsp_fix`` as the Wave 2 verifier-lane
preview-and-stage operator that folds the raw ``lsp_code_actions`` tool
into the agent-first surface. The doc pins both the public signature and
the agent-facing contract:

```python
async def lsp_fix(
    target: str = "",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    diagnostic_index: int = -1,      # -1 = all diagnostics on the line
    kind: str = "",                  # filter, e.g. "quickfix" / "refactor.extract"
) -> str: ...
```

Sample output (per docs):

```text
(d0) err.foo — undefined name 'banana'
(d1) warn.foo — unused import 'sys'
[0] Add import banana [quickfix] (1 file(s))
[1] Remove unused import 'sys' [quickfix] (1 file(s))
[-] Run banana on workspace (command-only; not staged)

Staged 2 edit action(s). Call lsp_confirm(N) to apply.
```

What we pin without a live LSP chain:

1. Public signature — argument names, defaults, and the agent-first ordering
   (``target`` first, then file/symbol/line fallbacks, then the
   ``diagnostic_index`` and ``kind`` knobs). Drift here breaks every agent
   that calls ``lsp_fix("[3]", kind="refactor.extract")``.
2. Async coroutine — ``_wrap_with_header`` only wraps coroutines.
3. Registry hygiene — ``fix`` is registered, capability-gated against
   ``codeActionProvider``, and the raw ``code_actions`` is cut from both
   registries ("no aliases, no shims" per the Raw Tool Cut Map).
4. Graph-index target shape — ``lsp_fix`` is documented to accept the same
   target shapes as the rest of Wave 1/2. We can't drive the full code
   action round-trip without a real LSP, but we can sanity-check the
   resolver wiring: a ``"[0]"`` target with no prior semantic graph must
   surface the shared ``_graph_target_from_index`` error string.
5. Pending-staging contract — when source ships helpers like
   ``_format_code_action_row`` / ``_filter_actions_by_kind`` /
   ``_actions_for_diagnostic`` we exercise them in isolation; otherwise
   leave a punch-list reminder. End-to-end behaviour (real
   ``textDocument/codeAction`` round-trip, edit application) belongs in
   live smoke per the docs.

Tests gate on ``hasattr(server, "lsp_fix")`` and ``"fix" in _ALL_TOOLS``;
when the source hook is missing they skip with a message that names the
exact gap, doubling as a punch list. This matches the Wave 2 pattern set
by ``test_lsp_calls.py`` / ``test_lsp_session.py`` and
``test_tool_surface.py``'s ``test_fix_replaces_code_actions``.
"""
import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any

from cc_lsp_now.candidate import Candidate
from cc_lsp_now.candidate_kind import CandidateKind
from cc_lsp_now import server as _server
from cc_lsp_now.server import _ALL_TOOLS, TOOL_CAPABILITIES


def _fix_attr() -> Any:
    # Resolved via getattr so ty doesn't choke on the still-missing
    # attribute when the source hook hasn't landed yet — once lsp_fix
    # ships the runtime check (_has_fix) gates whether it actually gets
    # called, but the static lookup must stay forgiving.
    return getattr(_server, "lsp_fix", None)


_FIX_HOOK_MSG = (
    "MISSING SOURCE HOOK: lsp_fix not yet defined on cc_lsp_now.server "
    "(Wave 2 verifier lane). docs/tool-surface.md expects "
    "`async def lsp_fix(target='', file_path='', symbol='', line=0, "
    "diagnostic_index=-1, kind='') -> str`, replacing lsp_code_actions "
    "with a graph-aware preview-and-stage surface that lists diagnostics "
    "as (dN), numbers edit-backed actions as [N], and stages them into "
    "_pending for lsp_confirm(N)."
)

_FIX_REGISTRY_MSG = (
    "MISSING SOURCE HOOK: 'fix' not yet registered in _ALL_TOOLS. "
    "docs/tool-surface.md Raw Tool Cut Map: `fix` → codeActionProvider."
)


def _has_fix() -> bool:
    return hasattr(_server, "lsp_fix")


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_fix returned non-str: {type(result)!r}"
    return result


class LspFixSignatureTests(unittest.TestCase):
    """Pin the public signature so the agent-first one-line contract from
    docs/tool-surface.md can't drift. Argument names are agent-visible —
    renaming ``diagnostic_index`` → ``diag`` would silently break every
    existing call site since MCP tools dispatch by kwargs.
    """

    def test_lsp_fix_is_async_callable(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        self.assertTrue(
            inspect.iscoroutinefunction(_fix_attr()),
            "lsp_fix must be `async def` — _wrap_with_header only wraps coroutines",
        )

    def test_lsp_fix_signature_matches_docs(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        sig = inspect.signature(_fix_attr())
        # docs/tool-surface.md pins exactly these six kwargs in this order.
        # ``target`` is first (graph-aware); file/symbol/line are the
        # fallback resolver inputs shared with every other Wave 1/2
        # target-taking tool; diagnostic_index and kind are the narrow
        # knobs specific to fix.
        self.assertEqual(
            list(sig.parameters),
            ["target", "file_path", "symbol", "line", "diagnostic_index", "kind"],
            f"lsp_fix signature drifted from docs/tool-surface.md: {sig}",
        )

    def test_lsp_fix_target_defaults_to_empty_string(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        sig = inspect.signature(_fix_attr())
        # Empty default lets file_path/symbol/line fallbacks light up when
        # no graph index / Lxx is supplied, matching every other Wave 1/2
        # target-taking tool (lsp_symbol, lsp_goto, lsp_refs, lsp_calls).
        self.assertEqual(sig.parameters["target"].default, "")

    def test_lsp_fix_file_path_symbol_line_defaults_match_other_wave_tools(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        sig = inspect.signature(_fix_attr())
        # Empty string / zero defaults are the shared "fallback resolver"
        # contract used by lsp_symbol/lsp_goto/lsp_refs/lsp_calls.
        # _resolve_semantic_target treats them as "not supplied"; non-empty
        # defaults would short-circuit graph-index resolution.
        self.assertEqual(sig.parameters["file_path"].default, "")
        self.assertEqual(sig.parameters["symbol"].default, "")
        self.assertEqual(sig.parameters["line"].default, 0)

    def test_lsp_fix_diagnostic_index_defaults_to_minus_one(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        sig = inspect.signature(_fix_attr())
        # docs: "-1 = all diagnostics on the line". Drifting to 0 would
        # silently change "give me actions for everything on this line"
        # into "give me actions for the first diagnostic only" — a much
        # narrower default that breaks any agent relying on the documented
        # all-diagnostics behavior.
        self.assertEqual(
            sig.parameters["diagnostic_index"].default,
            -1,
            "docs/tool-surface.md pins diagnostic_index default to -1 "
            "('all diagnostics on the line'); a different default narrows "
            "the result silently.",
        )

    def test_lsp_fix_kind_defaults_to_empty_string(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        sig = inspect.signature(_fix_attr())
        # docs: "filter, e.g. 'quickfix' / 'refactor.extract'". Empty
        # default = no filter (show all kinds); a non-empty default would
        # silently exclude action kinds the agent didn't know to opt into.
        self.assertEqual(sig.parameters["kind"].default, "")


class LspFixRegistryTests(unittest.TestCase):
    """Wave 2 acceptance for the registry: ``fix`` is registered, the
    function attribute matches the registered tuple, capability gating
    targets ``codeActionProvider`` (not the deprecated raw verb keys), and
    the raw ``code_actions`` is gone.

    Cross-cutting raw cuts also live in tests/test_tool_surface.py; this
    file pins the *positive* registration shape of ``fix`` itself for
    parity with test_lsp_outline.py / test_lsp_calls.py / test_lsp_session.py.
    """

    def test_fix_is_in_all_tools(self) -> None:
        if "fix" not in _ALL_TOOLS:
            self.skipTest(_FIX_REGISTRY_MSG)
        self.assertIn("fix", _ALL_TOOLS)

    def test_fix_registered_function_matches_module_attr(self) -> None:
        if "fix" not in _ALL_TOOLS:
            self.skipTest(_FIX_REGISTRY_MSG)
        func, _method = _ALL_TOOLS["fix"]
        self.assertIs(
            func,
            getattr(_server, "lsp_fix", None),
            "_ALL_TOOLS['fix'] must be the public lsp_fix — a "
            "divergence here means the tool registers a stale alias and "
            "_wrap_with_header attaches the wrong header to results.",
        )

    def test_fix_capability_is_code_action_provider(self) -> None:
        if "fix" not in TOOL_CAPABILITIES:
            self.skipTest(_FIX_REGISTRY_MSG)
        # docs/tool-surface.md Raw Tool Cut Map binds fix to
        # codeActionProvider. None would always-enable the tool against
        # servers that lack code actions; a wrong key would silently
        # disable it everywhere.
        self.assertEqual(
            TOOL_CAPABILITIES["fix"],
            "codeActionProvider",
        )

    def test_fix_method_label_is_non_empty_string(self) -> None:
        if "fix" not in _ALL_TOOLS:
            self.skipTest(_FIX_REGISTRY_MSG)
        _func, method = _ALL_TOOLS["fix"]
        # The header line ([method]) is what the agent sees on every call;
        # an empty label would render as "[]" and lose the orientation
        # signal. We don't pin a specific verb (the implementation may
        # choose textDocument/codeAction or a synthetic cc-lsp-now/fix
        # label) but it must be a non-empty string.
        self.assertIsInstance(method, str)
        self.assertTrue(method, "fix method label must be non-empty")

    def test_raw_code_actions_is_absent_from_registry(self) -> None:
        # Mirror of the surface-level cut so per-tool failure points at
        # this file; ``fix`` shipping must remove the raw entry.
        if "fix" not in _ALL_TOOLS:
            self.skipTest(_FIX_REGISTRY_MSG)
        self.assertNotIn("code_actions", _ALL_TOOLS)

    def test_raw_code_actions_capability_entry_is_dropped(self) -> None:
        if "fix" not in _ALL_TOOLS:
            self.skipTest(_FIX_REGISTRY_MSG)
        # Dead capability paths invite accidental re-introduction; the cut
        # must reach TOOL_CAPABILITIES too.
        self.assertNotIn("code_actions", TOOL_CAPABILITIES)


class LspFixGraphIndexTests(unittest.TestCase):
    """``lsp_fix`` is documented to accept the same target shapes as the
    rest of Wave 1/2. The consume side reuses the existing
    ``_resolve_semantic_target`` plumbing — already pinned in
    tests/test_lsp_grep.py — so we only need to assert lsp_fix actually
    goes through the resolver: a ``"[0]"`` target with no prior graph must
    surface the documented "No previous semantic graph." string, proving
    target resolution hasn't accidentally been bypassed (e.g. by an
    "always require file_path" guard ported from the raw lsp_code_actions
    surface).
    """

    def setUp(self) -> None:
        # Reset semantic nav state so "[0]" fails predictably with the
        # "No previous semantic graph." string rather than reaching into
        # a stale graph from another test.
        if hasattr(_server, "_record_semantic_nav_context"):
            _server._record_semantic_nav_context("", [])

    def test_graph_index_target_routes_through_semantic_resolver(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        # With no prior _record_semantic_nav_context call, "[0]" must hit
        # _graph_target_from_index's "No previous semantic graph." branch.
        # If lsp_fix instead returns a generic error or raises, it has
        # bypassed the shared resolver and the [N]-target contract is
        # broken.
        result = _run(_fix_attr()(target="[0]"))
        self.assertIsInstance(result, str)
        self.assertIn(
            "No previous semantic graph",
            result,
            "lsp_fix('[0]') with no prior graph must surface the shared "
            "_graph_target_from_index error so agents see the same "
            "self-correction path used by lsp_symbol/lsp_goto/lsp_refs/"
            f"lsp_calls; got: {result!r}",
        )

    def test_no_target_and_no_fallbacks_returns_help_string(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)
        # When neither target nor any of file_path/symbol/line is supplied,
        # _resolve_semantic_target returns the
        # "Provide target, or file_path with symbol/line." help string.
        # lsp_fix must surface that as a string — not raise — so an agent
        # that calls lsp_fix() with no args gets a readable nudge instead
        # of an MCP transport break.
        try:
            result = _run(_fix_attr()())
        except (TypeError, ValueError) as e:
            self.fail(
                f"lsp_fix() with no args raised {type(e).__name__}: {e}. "
                f"Missing target must produce a help string, not raise."
            )
        self.assertIsInstance(result, str)
        self.assertTrue(result, "empty result on no-args is uninformative")


class LspFixKindFilterHelperTests(unittest.TestCase):
    """The ``kind`` knob is documented to filter by LSP ``CodeActionKind``
    prefix — so ``kind="refactor.extract"`` must match an action with
    kind ``"refactor.extract.function"`` but reject ``"quickfix"``.
    Prefix-matching (not exact-equality) is what makes the doc example
    "just organize-imports" work, since the actual kind a server emits is
    ``"source.organizeImports"`` — a prefix of ``"source"``.

    If the implementation exposes a unit-testable helper
    (``_filter_actions_by_kind`` / ``_action_kind_matches``), we exercise
    it in isolation; otherwise we leave a punch-list reminder. The helper
    seam is what lets a refactor of the filter logic stay covered without
    requiring a live LSP round-trip.
    """

    def test_filter_helper_is_exposed_for_unit_testing(self) -> None:
        # Punch list: when the implementation introduces a unit-testable
        # kind-prefix matcher (named one of the variants below or similar),
        # delete the skipTest below and add focused cases:
        #   - exact match: kind="quickfix" matches "quickfix"
        #   - prefix match: kind="refactor" matches "refactor.extract.function"
        #   - dotted prefix: kind="refactor.extract" matches
        #     "refactor.extract.function" but NOT "refactor.inline"
        #   - empty filter: kind="" matches every action
        #   - missing kind on action: kind="quickfix" does NOT match an
        #     action with no "kind" field (LSP allows it; spec says such
        #     actions are unkinded and must not be included by kind filters)
        helper = (
            getattr(_server, "_filter_actions_by_kind", None)
            or getattr(_server, "_action_kind_matches", None)
            or getattr(_server, "_code_action_kind_matches", None)
        )
        if helper is None:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_fix kind-filter helper "
                "(_filter_actions_by_kind / _action_kind_matches) not "
                "exposed. docs/tool-surface.md: 'The kind filter narrows "
                "by LSP CodeActionKind prefix' — when the implementation "
                "factors that out into a helper, this test should drive "
                "exact/prefix/dotted-prefix/empty-filter cases without a "
                "live LSP."
            )
        self.assertTrue(callable(helper))
        self.assertTrue(helper("quickfix", "quickfix"))
        self.assertTrue(helper("refactor.extract.function", "refactor"))
        self.assertTrue(helper("refactor.extract.function", "refactor.extract"))
        self.assertTrue(helper("quickfix", ""))
        self.assertFalse(helper("refactor.inline", "refactor.extract"))
        self.assertFalse(helper("", "quickfix"))


class LspFixDiagnosticIndexHelperTests(unittest.TestCase):
    """The ``diagnostic_index`` knob is documented as ``-1 = all diagnostics
    on the line``, with non-negative values selecting a specific ``(dN)``
    listed in the output. The selection has to map a 1-D agent-supplied
    index onto the line's actual diagnostic list (which lives on the
    primary client's ``diagnostics`` cache, keyed by URI), so a helper
    seam — e.g. ``_diagnostics_for_line`` / ``_diagnostic_at_index`` —
    is the natural unit-testable surface.

    If such a helper is exposed we exercise it; otherwise punch-list it.
    """

    def test_diagnostic_index_helper_is_exposed_for_unit_testing(self) -> None:
        helper = (
            getattr(_server, "_diagnostics_for_line", None)
            or getattr(_server, "_diagnostic_at_index", None)
            or getattr(_server, "_diagnostics_at_position", None)
        )
        if helper is None:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_fix diagnostic-index helper "
                "(_diagnostics_for_line / _diagnostic_at_index) not "
                "exposed. docs/tool-surface.md: 'diagnostic_index: int = "
                "-1; -1 = all diagnostics on the line' — when the "
                "implementation factors the (dN)→Diagnostic mapping out "
                "into a helper, this test should drive: -1=all, "
                "0=first, OOB=error string, no-line-diagnostics=empty."
            )
        self.assertTrue(callable(helper))
        diagnostics = [
            {
                "range": {"start": {"line": 2, "character": 7}},
                "message": "second by character",
            },
            {
                "range": {"start": {"line": 1, "character": 0}},
                "message": "wrong line",
            },
            {
                "range": {"start": {"line": 2, "character": 1}},
                "message": "first by character",
            },
        ]
        selected = helper(diagnostics, 2)
        self.assertEqual(
            [d["message"] for d in selected],
            ["first by character", "second by character"],
        )


class LspFixPendingStagingContractTests(unittest.TestCase):
    """``lsp_fix`` is documented to stage edit-backed code actions into
    ``_pending`` so ``lsp_confirm(N)`` can apply them. The raw
    ``lsp_code_actions`` used ``CandidateKind.CODE_ACTION`` for dispatch;
    ``lsp_fix`` keeps that candidate kind while using ``"fix"`` as the
    pending-buffer display kind so the confirm transcript matches the new
    workflow verb.

    These tests pin the *contract shape* (what ``_pending`` looks like
    after a fix preview) using the existing _set_pending plumbing —
    they don't drive a real code-action round-trip, but they assert the
    expected shape an agent would see and the confirm-applies path keeps
    working with that shape. The end-to-end "lsp_fix actually populates
    _pending" check belongs in live smoke per the docs.
    """

    def setUp(self) -> None:
        # Snapshot _pending so a leaked staged action from another test
        # doesn't poison the assertions, and so we restore it after.
        self._prior_pending = _server._pending
        _server._clear_pending()

    def tearDown(self) -> None:
        _server._pending = self._prior_pending

    def test_code_action_candidate_kind_is_stable(self) -> None:
        # CandidateKind.CODE_ACTION is the contract that ``lsp_confirm``
        # dispatches on. If lsp_fix introduces a new CandidateKind or
        # renames this one, the existing confirm flow breaks silently.
        self.assertEqual(CandidateKind.CODE_ACTION.value, "code_action")

    def test_pending_buffer_kind_string_is_fix(self) -> None:
        # The pending kind string is what shows up in ``Applied [fix #N]: ...``
        # after lsp_confirm. lsp_fix should name the workflow verb even though
        # the candidate dispatch kind remains CandidateKind.CODE_ACTION.
        candidate = Candidate(
            kind=CandidateKind.CODE_ACTION,
            title="Add import banana",
            edit={"changes": {"file:///workspace/tmp/x.py": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 0},
                    },
                    "newText": "import banana\n",
                }
            ]}},
        )
        _server._set_pending("fix", [candidate], "1 code action(s)")

        self.assertIsNotNone(_server._pending)
        assert _server._pending is not None  # for ty
        self.assertEqual(_server._pending.kind, "fix")
        self.assertEqual(len(_server._pending.candidates), 1)
        self.assertIs(_server._pending.candidates[0], candidate)

    def test_lsp_confirm_applies_staged_code_action_candidate(self) -> None:
        # This is the round-trip lsp_fix is meant to feed into: stage an
        # edit-backed code action under _pending, then lsp_confirm(0)
        # applies it and clears the buffer. We use a no-op WorkspaceEdit
        # (empty changes) so we don't write to disk, and we assert the
        # confirm response matches the kind/title shape the agent sees.
        candidate = Candidate(
            kind=CandidateKind.CODE_ACTION,
            title="noop fix",
            edit={"changes": {}},
        )
        _server._set_pending("fix", [candidate], "1 code action(s)")

        result = asyncio.run(_server.lsp_confirm(0))

        self.assertIn("Applied", result)
        self.assertIn("fix", result)
        self.assertIn("noop fix", result)
        # _pending is single-shot — confirm clears it so the next preview
        # starts clean. This is what makes "stage → confirm → stage again"
        # safe across consecutive lsp_fix calls.
        self.assertIsNone(_server._pending)


class LspFixLivePathTests(unittest.TestCase):
    """Drive the live ``lsp_fix`` body without spinning up a real LSP, by
    monkey-patching the three resolver/transport seams (
    ``_resolve_semantic_target``, ``_get_client``, ``_request``).

    Why monkey-patch in addition to pure helpers: the helper tests pin the
    small predicates, while this path verifies the whole agent-visible flow:
    diagnostic selection, request context, action filtering, pending staging,
    and stale-buffer clearing. That mirrors the ``test_lsp_session.py``
    lifecycle tests that monkey-patch ``_get_client`` / ``_warmup_folder``.
    """

    def setUp(self) -> None:
        if not _has_fix():
            self.skipTest(_FIX_HOOK_MSG)

        # Snapshot module-level seams so we can restore in tearDown without
        # leaking a fake into other test files.
        self._saved_resolve = _server._resolve_semantic_target
        self._saved_get_client = _server._get_client
        self._saved_request = _server._request
        self._prior_pending = _server._pending
        _server._clear_pending()

        # A fake SemanticTarget anchored at a project-local tmp path.
        self._target_uri = "file:///workspace/tmp/x_lsp_fix_live.py"
        self._target = _server.SemanticTarget(
            uri=self._target_uri,
            pos={"line": 0, "character": 0},
            path="/workspace/tmp/x_lsp_fix_live.py",
            line=1,
            character=0,
            name="x",
        )

        # Two diagnostics on line 0 so diagnostic_index=0/1 select the
        # first/second and =-1 forwards both. Off-line diagnostic on line
        # 5 must be filtered out by the line check.
        self._diagnostics = [
            {
                "range": {"start": {"line": 0, "character": 0},
                          "end": {"line": 0, "character": 1}},
                "severity": 1,
                "message": "undefined name 'banana'",
                "source": "ty",
                "code": "name-defined",
            },
            {
                "range": {"start": {"line": 0, "character": 4},
                          "end": {"line": 0, "character": 7}},
                "severity": 2,
                "message": "unused import 'sys'",
                "source": "ty",
                "code": "unused-import",
            },
            {
                "range": {"start": {"line": 5, "character": 0},
                          "end": {"line": 5, "character": 1}},
                "severity": 1,
                "message": "off-line; must not appear in line diagnostics",
            },
        ]

        # Fake client advertising the diagnostics dict the source reads.
        class _FakeClient:
            def __init__(self, diags: dict[str, list[dict]]) -> None:
                self.diagnostics = diags

        self._fake_client = _FakeClient({self._target_uri: self._diagnostics})

        async def fake_resolve(*_args: Any, **_kwargs: Any) -> Any:
            return self._target

        async def fake_get_client(_idx: int) -> Any:
            return self._fake_client

        # ``_request`` is the seam where textDocument/codeAction is
        # dispatched; the per-test ``_actions`` list controls what the
        # fake server returns.
        self._actions: list[dict] = []
        self._captured_request: dict[str, Any] = {}

        outer = self

        async def fake_request(method: str, params: Any, *, uri: str | None = None) -> Any:
            outer._captured_request = {"method": method, "params": params, "uri": uri}
            return outer._actions

        setattr(_server, "_resolve_semantic_target", fake_resolve)
        setattr(_server, "_get_client", fake_get_client)
        setattr(_server, "_request", fake_request)

    def tearDown(self) -> None:
        setattr(_server, "_resolve_semantic_target", self._saved_resolve)
        setattr(_server, "_get_client", self._saved_get_client)
        setattr(_server, "_request", self._saved_request)
        _server._pending = self._prior_pending

    def test_diagnostic_index_out_of_range_returns_error_and_clears_pending(self) -> None:
        # Pre-stage something so we can prove the OOB path clears _pending.
        _server._set_pending(
            "code_action",
            [Candidate(kind=CandidateKind.CODE_ACTION, title="stale", edit={})],
            "stale",
        )
        # Line has 2 diagnostics; index 2 is OOB.
        result = _run(_fix_attr()(target="dummy", diagnostic_index=2))

        self.assertIn("(d2)", result)
        self.assertIn("out of range", result)
        # 2 is documented as the line's diagnostic count for this fixture.
        self.assertIn("2 diagnostic(s)", result)
        # The OOB path must not stage a stale buffer behind the agent's back.
        self.assertIsNone(_server._pending)

    def test_diagnostic_index_minus_one_forwards_all_line_diagnostics(self) -> None:
        # No actions returned, but we can still verify the codeAction
        # context.diagnostics shape forwarded by lsp_fix when the index
        # is the documented default of -1.
        self._actions = []
        result = _run(_fix_attr()(target="dummy", diagnostic_index=-1))

        params = self._captured_request["params"]
        forwarded = params["context"]["diagnostics"]
        # Both line-0 diagnostics forwarded; the off-line one filtered.
        self.assertEqual(len(forwarded), 2)
        self.assertEqual(forwarded[0]["message"], "undefined name 'banana'")
        self.assertEqual(forwarded[1]["message"], "unused import 'sys'")
        # Diagnostics block rendered in the output, both rows present.
        self.assertIn("(d0)", result)
        self.assertIn("(d1)", result)

    def test_diagnostic_index_specific_forwards_only_that_diagnostic(self) -> None:
        self._actions = []
        _run(_fix_attr()(target="dummy", diagnostic_index=1))

        forwarded = self._captured_request["params"]["context"]["diagnostics"]
        # Only the second line diagnostic was forwarded as context.
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0]["message"], "unused import 'sys'")
        self.assertEqual(
            self._captured_request["params"]["range"],
            forwarded[0]["range"],
            "specific diagnostic fixes should request actions over the diagnostic range",
        )

    def test_kind_filter_excludes_non_matching_action_kinds(self) -> None:
        # Two actions: one quickfix (with edit) and one refactor (with edit).
        # kind="quickfix" must keep the first and drop the second.
        self._actions = [
            {
                "title": "Add import banana",
                "kind": "quickfix",
                "edit": {"changes": {self._target_uri: [
                    {"range": {"start": {"line": 0, "character": 0},
                               "end": {"line": 0, "character": 0}},
                     "newText": "import banana\n"}
                ]}},
            },
            {
                "title": "Extract function",
                "kind": "refactor.extract.function",
                "edit": {"changes": {self._target_uri: []}},
            },
        ]
        result = _run(_fix_attr()(target="dummy", kind="quickfix"))

        self.assertEqual(
            self._captured_request["params"]["context"]["only"],
            ["quickfix"],
        )
        self.assertIn("Add import banana", result)
        self.assertNotIn("Extract function", result)
        self.assertIn("[0]", result)
        # The hidden-by-kind tally is the agent's hint that more actions
        # exist behind the filter.
        self.assertIn("hidden by kind=", result)
        # The single matching action is staged for confirm.
        self.assertIsNotNone(_server._pending)
        assert _server._pending is not None  # for ty
        self.assertEqual(len(_server._pending.candidates), 1)
        self.assertEqual(_server._pending.candidates[0].title, "Add import banana")

    def test_kind_filter_uses_prefix_match_not_exact(self) -> None:
        # docs/tool-surface.md: "filter by LSP CodeActionKind prefix".
        # kind="refactor.extract" must keep "refactor.extract.function"
        # (a strict prefix match) but drop "refactor.inline".
        self._actions = [
            {
                "title": "Extract function",
                "kind": "refactor.extract.function",
                "edit": {"changes": {self._target_uri: []}},
            },
            {
                "title": "Inline variable",
                "kind": "refactor.inline",
                "edit": {"changes": {self._target_uri: []}},
            },
        ]
        result = _run(_fix_attr()(target="dummy", kind="refactor.extract"))

        self.assertIn("Extract function", result)
        self.assertNotIn("Inline variable", result)

    def test_command_only_action_renders_with_dash_marker_and_is_not_staged(self) -> None:
        # docs: "Command-only or no-edit actions render as [-] and are
        # excluded from the index." A command-only action must render
        # with the [-] marker and stay out of _pending.
        self._actions = [
            {
                "title": "Run banana on workspace",
                "kind": "source",
                "command": {"command": "banana.runWorkspace"},
            },
        ]
        result = _run(_fix_attr()(target="dummy"))

        self.assertIn("[-]", result)
        self.assertIn("command-only", result)
        self.assertIn("Run banana on workspace", result)
        # No edit-backed action → buffer cleared, not staged.
        self.assertIsNone(_server._pending)
        self.assertIn("No edit-backed actions to stage", result)

    def test_empty_action_list_clears_pending_and_says_none(self) -> None:
        # Pre-stage to prove the empty-result path clears the buffer.
        _server._set_pending(
            "code_action",
            [Candidate(kind=CandidateKind.CODE_ACTION, title="stale", edit={})],
            "stale",
        )
        self._actions = []
        result = _run(_fix_attr()(target="dummy"))

        self.assertIn("actions: (none)", result)
        self.assertIsNone(_server._pending)


if __name__ == "__main__":
    unittest.main()
