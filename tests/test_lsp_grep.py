import unittest
from pathlib import Path

from cc_lsp_now.server import (
    SemanticGrepGroup,
    SemanticGrepHit,
    _context_breadcrumb,
    _format_semantic_grep_group,
    _identifier_hits_on_line,
    _record_semantic_nav_context,
    _resolve_line_target,
    _semantic_grep_text_hits,
    _semantic_kind_and_type,
)


class LspGrepTests(unittest.TestCase):
    def test_text_hits_use_identifier_boundaries_and_utf16_columns(self) -> None:
        fixture = Path("tmp/test_lsp_grep_fixture.cs")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text("😀 ctx context ctx2 ctx\n", encoding="utf-8")
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))

        hits = _semantic_grep_text_hits([str(fixture)], "ctx", 10)

        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].line, 0)
        self.assertEqual(hits[0].character, 3)
        self.assertEqual(hits[1].character, 20)

    def test_breadcrumb_abridges_matching_file_and_class_name(self) -> None:
        symbols = [
            {
                "name": "ComfyNodeRenderer",
                "kind": 5,
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 80, "character": 1}},
                "children": [
                    {
                        "name": "Render",
                        "kind": 6,
                        "range": {"start": {"line": 43, "character": 4}, "end": {"line": 70, "character": 5}},
                    }
                ],
            }
        ]

        crumb = _context_breadcrumb("src/ComfyNodeRenderer.cs", 44, 12, "ctx", symbols)

        self.assertEqual(crumb, "ComfyNodeRenderer:44::Render::ctx")

    def test_hover_extracts_argument_type(self) -> None:
        hover = {"contents": {"value": "```csharp\n(parameter) RenderContext ctx\n```"}}

        kind, type_text = _semantic_kind_and_type("ctx", hover)

        self.assertEqual(kind, "arg")
        self.assertEqual(type_text, "RenderContext")

    def test_group_formatter_keeps_one_line_shape(self) -> None:
        hit = SemanticGrepHit(
            path="/repo/src/ComfyNodeRenderer.cs",
            line=43,
            character=12,
            line_text="Render(RenderContext ctx)",
            uri="file:///repo/src/ComfyNodeRenderer.cs",
            pos={"line": 43, "character": 12},
        )
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/ComfyNodeRenderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[hit],
            reference_locs=[
                {
                    "uri": "file:///repo/src/ComfyNodeRenderer.cs",
                    "range": {"start": {"line": 43, "character": 12}, "end": {"line": 43, "character": 15}},
                },
                {
                    "uri": "file:///repo/src/ComfyNodeRenderer.cs",
                    "range": {"start": {"line": 56, "character": 8}, "end": {"line": 56, "character": 11}},
                },
                {
                    "uri": "file:///repo/src/ComfyNodeRenderer.cs",
                    "range": {"start": {"line": 68, "character": 8}, "end": {"line": 68, "character": 11}},
                },
                {
                    "uri": "file:///repo/src/ComfyNodeRenderer.cs",
                    "range": {"start": {"line": 69, "character": 8}, "end": {"line": 69, "character": 11}},
                },
            ],
            context_symbols=[
                {
                    "name": "ComfyNodeRenderer",
                    "kind": 5,
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 80, "character": 1}},
                    "children": [
                        {
                            "name": "Render",
                            "kind": 6,
                            "range": {"start": {"line": 43, "character": 4}, "end": {"line": 70, "character": 5}},
                        }
                    ],
                }
            ],
        )

        line = _format_semantic_grep_group(3, group)

        self.assertEqual(
            line,
            "[3] arg ctx: RenderContext — ComfyNodeRenderer:44::Render::ctx — refs 4 — def L44 — samples L44,L57,L69,...",
        )

    def test_identifier_hits_on_line_include_function_args(self) -> None:
        fixture = Path("tmp/test_lsp_symbols_at_fixture.cs")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text(
            "public void Render(RenderContext ctx, int count) { } // ctx comment\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))

        hits = _identifier_hits_on_line(str(fixture), 1)

        self.assertEqual([name for name, _hit in hits], ["Render", "RenderContext", "ctx", "int", "count"])

    def test_text_hits_ignore_comment_tails(self) -> None:
        fixture = Path("tmp/test_lsp_grep_comment_fixture.py")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text("query = 1  # query in comment\n# query only comment\n", encoding="utf-8")
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))

        hits = _semantic_grep_text_hits([str(fixture)], "query", 10)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].line, 0)

    def test_bare_line_target_resolves_through_last_semantic_context(self) -> None:
        hit = SemanticGrepHit(
            path="/repo/src/ComfyNodeRenderer.cs",
            line=77,
            character=8,
            line_text="ctx.Draw();",
            uri="file:///repo/src/ComfyNodeRenderer.cs",
            pos={"line": 77, "character": 8},
        )
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/ComfyNodeRenderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[hit],
        )
        _record_semantic_nav_context("ctx", [group])

        self.assertEqual(
            _resolve_line_target("L78"),
            ("/repo/src/ComfyNodeRenderer.cs", 78),
        )


if __name__ == "__main__":
    unittest.main()
