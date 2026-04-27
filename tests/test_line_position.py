from pathlib import Path
import unittest

from cc_lsp_now.server import _fallback_position_on_line, _symbols_on_line


class LinePositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Path("tmp/test_line_position_fixture.cs")
        self.fixture.parent.mkdir(exist_ok=True)
        self.fixture.write_text(
            "\n".join(
                [
                    "public sealed class HistoryUI",
                    "{",
                    "    private Texture? GetOutputTexture(ImageArtifact entry)",
                    "    {",
                    "        return null;",
                    "    }",
                    "",
                    "    private readonly struct OutputMediaEntry : IMediaEntry",
                    "    {",
                    "    }",
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.fixture.unlink(missing_ok=True)

    def test_line_fallback_prefers_method_name_over_modifiers_and_return_type(self) -> None:
        pos = _fallback_position_on_line(str(self.fixture), 2)

        self.assertEqual(pos, {"line": 2, "character": 21})

    def test_line_fallback_prefers_type_name_after_struct_keyword(self) -> None:
        pos = _fallback_position_on_line(str(self.fixture), 7)

        self.assertEqual(pos, {"line": 7, "character": 28})

    def test_symbols_on_line_prefers_selection_range_on_target_line(self) -> None:
        symbols = [
            {
                "name": "HistoryUI",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 20},
                    "end": {"line": 9, "character": 5},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 20},
                    "end": {"line": 0, "character": 29},
                },
                "children": [
                    {
                        "name": "GetOutputTexture",
                        "kind": 6,
                        "range": {
                            "start": {"line": 2, "character": 21},
                            "end": {"line": 5, "character": 5},
                        },
                        "selectionRange": {
                            "start": {"line": 2, "character": 21},
                            "end": {"line": 2, "character": 37},
                        },
                    }
                ],
            }
        ]

        hits = _symbols_on_line(symbols, 2)

        self.assertEqual(hits[0][1], {"line": 2, "character": 21})
        self.assertEqual(hits[0][3], "GetOutputTexture")


if __name__ == "__main__":
    unittest.main()
