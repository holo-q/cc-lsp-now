import unittest
from pathlib import Path

from cc_lsp_now.server import _apply_text_edits, _format_text_edit_preview


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class WorkspaceEditTests(unittest.TestCase):
    def test_roslyn_minimal_rename_edit_reconstructs_full_symbol(self) -> None:
        text = "    public Func<ArtifactId, TextureRef?> GetOutputTexture { get; }\n"
        start = text.index("Outpu")
        end = start + len("Outpu")

        result = _apply_text_edits(
            text,
            [
                {
                    "range": {
                        "start": {"line": 0, "character": start},
                        "end": {"line": 0, "character": end},
                    },
                    "newText": "Artifac",
                }
            ],
        )

        self.assertIn("GetArtifactTexture", result)
        self.assertNotIn("GetOutputTexture", result)

    def test_lsp_utf16_character_offsets_are_converted_before_slicing(self) -> None:
        text = "😀GetOutputTexture();\n"
        prefix_units = _utf16_units("😀")
        old_name_units = _utf16_units("GetOutputTexture")

        result = _apply_text_edits(
            text,
            [
                {
                    "range": {
                        "start": {"line": 0, "character": prefix_units},
                        "end": {"line": 0, "character": prefix_units + old_name_units},
                    },
                    "newText": "GetArtifactTexture",
                }
            ],
        )

        self.assertEqual(result, "😀GetArtifactTexture();\n")

    def test_preview_shows_final_line_not_just_minimal_lsp_span(self) -> None:
        fixture = Path("tmp/test_workspace_edit_preview.cs")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text(
            "    public Func<ArtifactId, TextureRef?> GetOutputTexture { get; }\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))
        text = fixture.read_text(encoding="utf-8")
        start = text.index("Outpu")
        end = start + len("Outpu")

        lines = _format_text_edit_preview(
            str(fixture),
            [
                {
                    "range": {
                        "start": {"line": 0, "character": start},
                        "end": {"line": 0, "character": end},
                    },
                    "newText": "Artifac",
                }
            ],
        )

        preview = "\n".join(lines)
        self.assertIn("GetArtifactTexture", preview)
        self.assertIn("L1:45-50", preview)


if __name__ == "__main__":
    unittest.main()
