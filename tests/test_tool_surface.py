import unittest

from cc_lsp_now.server import _ALL_TOOLS


class ToolSurfaceTests(unittest.TestCase):
    def test_wave_one_graph_tools_are_public(self) -> None:
        for name in ["symbol", "goto", "refs"]:
            self.assertIn(name, _ALL_TOOLS)

    def test_replaced_raw_tools_are_not_public(self) -> None:
        for name in [
            "hover",
            "signature_help",
            "definition",
            "declaration",
            "type_definition",
            "implementation",
            "references",
        ]:
            self.assertNotIn(name, _ALL_TOOLS)


if __name__ == "__main__":
    unittest.main()
