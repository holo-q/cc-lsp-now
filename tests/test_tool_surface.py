import unittest

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


if __name__ == "__main__":
    unittest.main()
