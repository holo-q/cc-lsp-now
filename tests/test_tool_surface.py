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


if __name__ == "__main__":
    unittest.main()
