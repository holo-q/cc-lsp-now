import unittest
import asyncio
from pathlib import Path

from hsp.candidate import Candidate
from hsp.candidate_kind import CandidateKind
from hsp.server import (
    _clear_pending,
    _apply_text_edits,
    _apply_workspace_edit,
    _format_text_edit_preview,
    _set_pending,
    lsp_confirm,
)


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

    def test_invalid_text_edit_range_fails_instead_of_skipping(self) -> None:
        with self.assertRaises(ValueError):
            _apply_text_edits(
                "hello\n",
                [
                    {
                        "range": {
                            "start": {"line": 99, "character": 0},
                            "end": {"line": 99, "character": 1},
                        },
                        "newText": "x",
                    }
                ],
            )

    def test_workspace_edit_applies_resource_operations(self) -> None:
        root = Path("tmp/test_workspace_resource_ops")
        root.mkdir(parents=True, exist_ok=True)
        old = root / "old.txt"
        new = root / "new.txt"
        created = root / "created.txt"
        old.write_text("old", encoding="utf-8")
        self.addCleanup(lambda: root.rmdir() if root.exists() else None)
        self.addCleanup(lambda: created.unlink(missing_ok=True))
        self.addCleanup(lambda: new.unlink(missing_ok=True))
        self.addCleanup(lambda: old.unlink(missing_ok=True))

        result = _apply_workspace_edit({
            "documentChanges": [
                {"kind": "create", "uri": created.resolve().as_uri()},
                {"kind": "rename", "oldUri": old.resolve().as_uri(), "newUri": new.resolve().as_uri()},
                {"kind": "delete", "uri": created.resolve().as_uri()},
            ]
        })

        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertFalse(created.exists())
        self.assertEqual(result.renamed, [(str(old.resolve()), str(new.resolve()))])
        self.assertEqual(result.created, [str(created.resolve())])
        self.assertEqual(result.deleted, [str(created.resolve())])

    def test_confirm_rejects_negative_index(self) -> None:
        self.addCleanup(_clear_pending)
        _set_pending(
            "code_action",
            [Candidate(kind=CandidateKind.CODE_ACTION, title="noop", edit={})],
            "test pending",
        )

        self.assertEqual(
            asyncio.run(lsp_confirm(-1)),
            "Invalid index -1, only 1 candidates available.",
        )


if __name__ == "__main__":
    unittest.main()
