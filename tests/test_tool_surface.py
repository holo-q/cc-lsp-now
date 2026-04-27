import unittest

from cc_lsp_now import server as _server
from cc_lsp_now.server import _ALL_TOOLS, DISABLED_BY_DEFAULT, TOOL_CAPABILITIES


# Wave 1 of the agent-first tool surface (see docs/tool-surface.md).
# These are the "first implemented pieces" of the semantic graph operator
# surface and must remain publicly registered.
WAVE_ONE_PUBLIC = ["grep", "symbols_at", "symbol", "goto", "refs"]


# Raw protocol-shaped tools whose replacements have already shipped in Wave 1.
# Per docs/tool-surface.md "Acceptance Checks": "Registry tests or assertions
# prove replaced raw tools are absent from `_ALL_TOOLS`." These map to
# `lsp_symbol`, `lsp_goto`, and `lsp_refs` per the Raw Tool Cut Map.
WAVE_ONE_REPLACED_RAW = [
    "hover",
    "signature_help",
    "definition",
    "declaration",
    "type_definition",
    "implementation",
    "references",
]


# Wave 2 outline+verifier operators per docs/tool-surface.md. These tests
# describe the expected post-Wave-2 registry shape; they self-activate as
# each tool lands so a partial Wave 2 (e.g. only `outline`) still gets the
# surface check it deserves without blocking the suite on the others.
WAVE_TWO_PUBLIC = ["outline", "calls", "fix", "session"]

# Raw tools that Wave 2 replaces. When the matching workflow tool ships
# the raw entry must be cut from _ALL_TOOLS and TOOL_CAPABILITIES.
WAVE_TWO_REPLACEMENTS: dict[str, list[str]] = {
    "outline": ["document_symbols"],
    "calls": ["call_hierarchy_incoming", "call_hierarchy_outgoing"],
    "fix": ["code_actions"],
    "session": ["info", "workspaces", "add_workspace"],
}

FORMAT_TOOLS = ["formatting", "range_formatting"]

CUT_WITHOUT_REPLACEMENT = [
    "completion",
    "inlay_hint",
    "folding_range",
    "code_lens",
    "prepare_rename",
    "create_file",
    "delete_file",
]

CUT_WRAPPER_ATTRS = [
    "lsp_completion",
    "lsp_inlay_hint",
    "lsp_folding_range",
    "lsp_code_lens",
    "lsp_prepare_rename",
    "lsp_create_file",
    "lsp_delete_file",
]


class ToolSurfaceTests(unittest.TestCase):
    def test_wave_one_graph_tools_are_public(self) -> None:
        for name in WAVE_ONE_PUBLIC:
            self.assertIn(name, _ALL_TOOLS)

    def test_replaced_raw_tools_are_not_public(self) -> None:
        for name in WAVE_ONE_REPLACED_RAW:
            self.assertNotIn(name, _ALL_TOOLS)

    def test_wave_one_graph_tools_have_capability_mapping(self) -> None:
        # Capability gating runs by tool name, so every wave-1 graph tool needs
        # an explicit TOOL_CAPABILITIES entry — otherwise gating quietly skips it
        # and a server with no support still gets the tool registered.
        for name in WAVE_ONE_PUBLIC:
            self.assertIn(name, TOOL_CAPABILITIES)

    def test_replaced_raw_tools_are_not_capability_mapped(self) -> None:
        # When a raw tool is cut from _ALL_TOOLS its capability entry should
        # follow it out, otherwise the dotted path lingers as dead config and
        # invites a future re-introduction by accident.
        for name in WAVE_ONE_REPLACED_RAW:
            self.assertNotIn(name, TOOL_CAPABILITIES)

    def test_capability_table_matches_registry(self) -> None:
        # Every registered tool must have a capability entry, and the
        # capability table must not name phantom tools that aren't registered.
        self.assertEqual(set(_ALL_TOOLS), set(TOOL_CAPABILITIES))

    def test_disabled_by_default_tools_exist_in_registry(self) -> None:
        # Off-by-default names that don't actually exist in _ALL_TOOLS would
        # silently no-op the subtraction in the registration block.
        for name in DISABLED_BY_DEFAULT:
            self.assertIn(name, _ALL_TOOLS)

    def test_formatting_tools_are_not_agent_facing(self) -> None:
        # Formatting is intentionally outside the agent-facing surface. It is
        # noisy, low-reasoning-value mutation and belongs in editor/save hooks,
        # pre-commit hooks, CI, or explicit user formatter runs.
        self.assertFalse(
            DISABLED_BY_DEFAULT,
            "off-by-default formatting aliases should not linger; excluded "
            "tools must be cut from the registry entirely",
        )
        for name in FORMAT_TOOLS:
            self.assertNotIn(name, _ALL_TOOLS)
            self.assertNotIn(name, TOOL_CAPABILITIES)

    def test_cut_without_replacement_tools_are_not_public(self) -> None:
        # These raw/editor-shaped tools have no current agent workflow. Keeping
        # them out of both registries keeps the surface focused on semantic
        # graph operators instead of protocol mirroring.
        for name in CUT_WITHOUT_REPLACEMENT:
            self.assertNotIn(name, _ALL_TOOLS)
            self.assertNotIn(name, TOOL_CAPABILITIES)

    def test_cut_without_replacement_wrappers_are_removed(self) -> None:
        # Public wrappers are removed, not merely unregistered, so the source
        # cannot be re-exposed accidentally by a future registry sweep.
        for attr in CUT_WRAPPER_ATTRS:
            self.assertFalse(hasattr(_server, attr), f"{attr} should be removed")


class WaveTwoSurfaceTests(unittest.TestCase):
    """Wave 2 outline+verifier operators per docs/tool-surface.md.

    Each Wave 2 tool ships independently. The tests gate on the tool's
    presence in ``_ALL_TOOLS`` so that a partial Wave 2 (e.g. only
    ``outline`` shipped) still gets full acceptance coverage on the live
    pieces without blocking the suite on the unlanded ones. Skipped tests
    double as a punch list of remaining Wave 2 source hooks.
    """

    def _assert_wave_two_tool(self, name: str, replaces: list[str]) -> None:
        self.assertIn(name, _ALL_TOOLS, f"{name} not registered in _ALL_TOOLS")
        self.assertIn(
            name,
            TOOL_CAPABILITIES,
            f"{name} missing TOOL_CAPABILITIES entry — capability gating "
            f"quietly skips tools without one",
        )
        for raw in replaces:
            self.assertNotIn(
                raw,
                _ALL_TOOLS,
                f"{name} shipped but raw {raw} still in _ALL_TOOLS — "
                f"docs/tool-surface.md says no aliases, no shims",
            )
            self.assertNotIn(
                raw,
                TOOL_CAPABILITIES,
                f"{name} shipped but raw {raw} still in TOOL_CAPABILITIES — "
                f"dead capability paths invite accidental re-introduction",
            )

    def test_outline_replaces_document_symbols(self) -> None:
        if "outline" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_outline not yet registered "
                "(Wave 2 outline lane). docs/tool-surface.md expects "
                "`outline` → documentSymbolProvider with raw "
                "`document_symbols` cut from both registries."
            )
        self._assert_wave_two_tool("outline", ["document_symbols"])

    def test_calls_replaces_call_hierarchy_pair(self) -> None:
        if "calls" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_calls not yet registered "
                "(Wave 2 verifier lane). docs/tool-surface.md expects "
                "`calls` → callHierarchyProvider with both raw "
                "`call_hierarchy_incoming` and `call_hierarchy_outgoing` "
                "cut from both registries."
            )
        self._assert_wave_two_tool(
            "calls",
            ["call_hierarchy_incoming", "call_hierarchy_outgoing"],
        )

    def test_calls_capability_is_call_hierarchy_provider(self) -> None:
        # Self-activating: as soon as ``calls`` lands in TOOL_CAPABILITIES the
        # value must specifically be ``callHierarchyProvider`` (the same
        # provider the raw incoming/outgoing pair gated on). The generic
        # _assert_wave_two_tool only checks for *presence* of a capability
        # entry — a None or wrong-key value would slip through it and
        # silently disable gating for the whole calls surface, so the value
        # itself needs its own pin.
        if "calls" not in TOOL_CAPABILITIES:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_calls capability not yet wired. "
                "docs/tool-surface.md Raw Tool Cut Map binds `calls` to "
                "`callHierarchyProvider`."
            )
        self.assertEqual(
            TOOL_CAPABILITIES["calls"],
            "callHierarchyProvider",
            "calls must gate on callHierarchyProvider — anything else "
            "(None, definitionProvider, etc.) silently breaks capability "
            "gating for servers that don't advertise call hierarchy.",
        )

    def test_fix_replaces_code_actions(self) -> None:
        if "fix" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_fix not yet registered "
                "(Wave 2 verifier lane). docs/tool-surface.md expects "
                "`fix` → codeActionProvider with `code_actions` cut "
                "from both registries."
            )
        self._assert_wave_two_tool("fix", ["code_actions"])

    def test_fix_capability_is_code_action_provider(self) -> None:
        # Self-activating: as soon as ``fix`` lands in TOOL_CAPABILITIES the
        # value must specifically be ``codeActionProvider`` — the same
        # provider the raw ``code_actions`` tool gated on. The generic
        # _assert_wave_two_tool only checks for *presence* of a capability
        # entry — a None or wrong-key value would slip through it and
        # silently disable gating for the whole fix surface, so the value
        # itself needs its own pin (mirrors the calls capability pin).
        if "fix" not in TOOL_CAPABILITIES:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_fix capability not yet wired. "
                "docs/tool-surface.md Raw Tool Cut Map binds `fix` to "
                "`codeActionProvider`."
            )
        self.assertEqual(
            TOOL_CAPABILITIES["fix"],
            "codeActionProvider",
            "fix must gate on codeActionProvider — anything else "
            "(None, definitionProvider, etc.) silently breaks capability "
            "gating for servers that don't advertise code actions.",
        )

    def test_session_replaces_info_workspaces_add_workspace(self) -> None:
        if "session" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_session not yet registered "
                "(Wave 2 verifier lane). docs/tool-surface.md expects "
                "`session` to absorb `info`, `workspaces`, and "
                "`add_workspace`, all cut from both registries."
            )
        self._assert_wave_two_tool(
            "session",
            ["info", "workspaces", "add_workspace"],
        )


# Wave 3 mutation operators per docs/tool-surface.md. Wave 3 collapses the
# raw move-file pair (`lsp_move_file`, `lsp_move_files`) into a single
# preview-and-stage tool `lsp_move` with the documented signature
# `lsp_move(from_path='', to_path='', symbol='', moves='') -> str`.
WAVE_THREE_PUBLIC = ["move"]

WAVE_THREE_REPLACEMENTS: dict[str, list[str]] = {
    "move": ["move_file", "move_files"],
}

# Once `lsp_move` lands, the public wrapper attrs for the raw pair must be
# removed from the module — not just unregistered — so a future registry
# sweep cannot re-expose them. Mirrors Wave 1's CUT_WRAPPER_ATTRS pattern.
WAVE_THREE_CUT_WRAPPER_ATTRS = ["lsp_move_file", "lsp_move_files"]


class WaveThreeSurfaceTests(unittest.TestCase):
    """Wave 3 mutation operators per docs/tool-surface.md.

    Wave 3 collapses `lsp_move_file` + `lsp_move_files` into a single
    `lsp_move` workflow tool. As with Wave 2, the test gates on the new
    tool's presence so a partial Wave 3 still gets coverage on the live
    pieces; a skipped test doubles as a punch-list reminder for the
    implementation worker.
    """

    def test_move_replaces_move_file_and_move_files(self) -> None:
        if "move" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_move not yet registered "
                "(Wave 3 mutation lane). docs/tool-surface.md expects "
                "`move` to absorb both `move_file` and `move_files`, "
                "with the raw entries cut from both registries — no "
                "aliases, no shims."
            )
        self.assertIn("move", _ALL_TOOLS, "move not registered in _ALL_TOOLS")
        self.assertIn(
            "move",
            TOOL_CAPABILITIES,
            "move missing TOOL_CAPABILITIES entry — capability gating "
            "quietly skips tools without one",
        )
        for raw in WAVE_THREE_REPLACEMENTS["move"]:
            self.assertNotIn(
                raw,
                _ALL_TOOLS,
                f"move shipped but raw {raw} still in _ALL_TOOLS — "
                f"docs/tool-surface.md says no aliases, no shims",
            )
            self.assertNotIn(
                raw,
                TOOL_CAPABILITIES,
                f"move shipped but raw {raw} still in TOOL_CAPABILITIES — "
                f"dead capability paths invite accidental re-introduction",
            )

    def test_move_capability_is_will_rename_files(self) -> None:
        # Self-activating: as soon as `move` lands in TOOL_CAPABILITIES
        # the value must specifically be `workspace.fileOperations.willRename`
        # — the same provider both raw `move_file` and `move_files` gated
        # on. A None or wrong-key value would slip through generic
        # "is in TOOL_CAPABILITIES" assertions and silently disable
        # capability gating for the whole move surface, letting `move`
        # register against servers that don't advertise willRename support.
        if "move" not in TOOL_CAPABILITIES:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_move capability not yet wired. "
                "docs/tool-surface.md Raw Tool Cut Map binds `move` to "
                "the willRenameFiles backend (the same provider the raw "
                "move_file / move_files gated on)."
            )
        self.assertEqual(
            TOOL_CAPABILITIES["move"],
            "workspace.fileOperations.willRename",
            "move must gate on workspace.fileOperations.willRename — "
            "anything else (None, definitionProvider, etc.) silently "
            "breaks capability gating for servers that don't advertise "
            "willRename.",
        )

    def test_lsp_move_attr_exists(self) -> None:
        # `move` is registered against the public `lsp_move` coroutine.
        # Pinning the module attr (not just the registry entry) catches
        # the case where the registry maps to a stale alias.
        if "move" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_move not yet defined on "
                "cc_lsp_now.server. docs/tool-surface.md expects "
                "`async def lsp_move(from_path='', to_path='', "
                "symbol='', moves='') -> str`."
            )
        self.assertTrue(
            hasattr(_server, "lsp_move"),
            "lsp_move public wrapper missing — Wave 3 mutation lane "
            "expects a coroutine attr matching the registry entry",
        )

    def test_raw_move_wrapper_attrs_are_removed(self) -> None:
        # Public wrappers are removed, not merely unregistered, so the
        # source cannot be re-exposed accidentally by a future registry
        # sweep. Gate on `move` shipping so a half-built Wave 3 doesn't
        # produce a false failure here while the implementation worker
        # is still mid-edit on server.py.
        if "move" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_move not yet registered; the "
                "raw lsp_move_file / lsp_move_files wrapper cuts only "
                "land once `move` has shipped."
            )
        for attr in WAVE_THREE_CUT_WRAPPER_ATTRS:
            self.assertFalse(
                hasattr(_server, attr),
                f"{attr} should be removed once lsp_move ships — "
                f"docs/tool-surface.md: no aliases, no shims",
            )


# Wave 4 graph operator per docs/tool-surface.md. Wave 4 collapses the raw
# type-hierarchy pair (`lsp_type_hierarchy_supertypes`,
# `lsp_type_hierarchy_subtypes`) into a single direction-keyed graph
# operator `lsp_types`, mirroring how Wave 2 collapsed call hierarchy into
# `lsp_calls`. Same pattern, same backend (`typeHierarchyProvider`).
WAVE_FOUR_PUBLIC = ["types"]

WAVE_FOUR_REPLACEMENTS: dict[str, list[str]] = {
    "types": ["type_hierarchy_supertypes", "type_hierarchy_subtypes"],
}

# Once `lsp_types` lands, the raw wrapper attrs must be removed from the
# module - not just unregistered - so a future registry sweep cannot
# re-expose them. Mirrors the Wave 1 / Wave 3 cut pattern.
WAVE_FOUR_CUT_WRAPPER_ATTRS = [
    "lsp_type_hierarchy_supertypes",
    "lsp_type_hierarchy_subtypes",
]


class WaveFourSurfaceTests(unittest.TestCase):
    """Wave 4 graph operator per docs/tool-surface.md.

    Wave 4 collapses `lsp_type_hierarchy_supertypes` +
    `lsp_type_hierarchy_subtypes` into a single `lsp_types` direction-keyed
    verb, matching the `lsp_calls` shape. Tests gate on `types` being
    registered so a partial wave still gets coverage on the live pieces; a
    skipped test doubles as a punch-list reminder for the implementation
    worker.
    """

    def test_types_replaces_type_hierarchy_pair(self) -> None:
        if "types" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_types not yet registered "
                "(Wave 4 graph operator). docs/tool-surface.md expects "
                "`types` to absorb both `type_hierarchy_supertypes` and "
                "`type_hierarchy_subtypes` with the raw entries cut from "
                "both registries - no aliases, no shims."
            )
        self.assertIn("types", _ALL_TOOLS, "types not registered in _ALL_TOOLS")
        self.assertIn(
            "types",
            TOOL_CAPABILITIES,
            "types missing TOOL_CAPABILITIES entry - capability gating "
            "quietly skips tools without one",
        )
        for raw in WAVE_FOUR_REPLACEMENTS["types"]:
            self.assertNotIn(
                raw,
                _ALL_TOOLS,
                f"types shipped but raw {raw} still in _ALL_TOOLS - "
                f"docs/tool-surface.md says no aliases, no shims",
            )
            self.assertNotIn(
                raw,
                TOOL_CAPABILITIES,
                f"types shipped but raw {raw} still in TOOL_CAPABILITIES - "
                f"dead capability paths invite accidental re-introduction",
            )

    def test_types_capability_is_type_hierarchy_provider(self) -> None:
        # Self-activating: as soon as `types` lands in TOOL_CAPABILITIES the
        # value must specifically be `typeHierarchyProvider` - the same
        # provider both raw super/sub tools gated on. A None or wrong-key
        # value would slip through generic "is in TOOL_CAPABILITIES"
        # assertions and silently disable capability gating for the whole
        # types surface.
        if "types" not in TOOL_CAPABILITIES:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_types capability not yet wired. "
                "docs/tool-surface.md Raw Tool Cut Map binds `types` to "
                "`typeHierarchyProvider` (the same provider the raw "
                "supertypes / subtypes pair gated on)."
            )
        self.assertEqual(
            TOOL_CAPABILITIES["types"],
            "typeHierarchyProvider",
            "types must gate on typeHierarchyProvider - anything else "
            "(None, definitionProvider, etc.) silently breaks capability "
            "gating for servers that don't advertise type hierarchy.",
        )

    def test_lsp_types_attr_exists(self) -> None:
        # `types` is registered against the public `lsp_types` coroutine.
        # Pinning the module attr (not just the registry entry) catches the
        # case where the registry maps to a stale alias.
        if "types" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_types not yet defined on "
                "cc_lsp_now.server. docs/tool-surface.md expects "
                "`async def lsp_types(target='', direction='both', "
                "file_path='', symbol='', line=0, max_depth=1, "
                "max_edges=50) -> str`."
            )
        self.assertTrue(
            hasattr(_server, "lsp_types"),
            "lsp_types public wrapper missing - Wave 4 graph operator "
            "expects a coroutine attr matching the registry entry",
        )

    def test_raw_type_hierarchy_wrapper_attrs_are_removed(self) -> None:
        # Public wrappers are removed, not merely unregistered, so the
        # source cannot be re-exposed accidentally by a future registry
        # sweep. Gate on `types` shipping so a half-built Wave 4 doesn't
        # produce a false failure here while the implementation worker is
        # still mid-edit on server.py.
        if "types" not in _ALL_TOOLS:
            self.skipTest(
                "MISSING SOURCE HOOK: lsp_types not yet registered; the "
                "raw lsp_type_hierarchy_supertypes / "
                "lsp_type_hierarchy_subtypes wrapper cuts only land once "
                "`types` has shipped."
            )
        for attr in WAVE_FOUR_CUT_WRAPPER_ATTRS:
            self.assertFalse(
                hasattr(_server, attr),
                f"{attr} should be removed once lsp_types ships - "
                f"docs/tool-surface.md: no aliases, no shims",
            )


if __name__ == "__main__":
    unittest.main()
