import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch

from hsp import server as _server
from hsp.server import (
    SemanticGrepGroup,
    SemanticGrepHit,
    _context_breadcrumb,
    _format_semantic_grep_group,
    _format_semantic_sample_locs,
    _graph_target_from_index,
    _identifier_hits_on_line,
    _local_alias_coordinator,
    _record_semantic_nav_context,
    _resolve_line_target,
    _resolve_semantic_target,
    _semantic_grep_text_hits,
    _semantic_kind_and_type,
    lsp_memory,
)


def _make_hit(
    path: str = "/repo/src/Renderer.cs",
    line: int = 43,
    character: int = 12,
    line_text: str = "Render(RenderContext ctx)",
) -> SemanticGrepHit:
    return SemanticGrepHit(
        path=path,
        line=line,
        character=character,
        line_text=line_text,
        uri=f"file://{path}",
        pos={"line": line, "character": character},
    )


def _make_ref(path: str, line: int, character: int = 8) -> dict:
    return {
        "uri": f"file://{path}",
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": line, "character": character + 3},
        },
    }


class LspGrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict("os.environ", {"HSP_ROUTER": "off", "HSP_BROKER": "off"}, clear=False)
        self._env_patch.start()
        _server._bind_route_runtime("legacy")

    def tearDown(self) -> None:
        # Each test must start from an empty graph so that bare-line and
        # graph-index lookups can't leak state between cases.
        _server._bind_route_runtime("legacy")
        _record_semantic_nav_context("", [])
        _local_alias_coordinator.clear_epoch()
        self._env_patch.stop()

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

    def test_graph_index_resolves_through_last_semantic_context(self) -> None:
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
        )
        _record_semantic_nav_context("ctx", [group])

        target = _graph_target_from_index("0")

        if isinstance(target, str):
            self.fail(target)
        self.assertEqual(target.name, "ctx")
        self.assertEqual(target.path, "/repo/src/ComfyNodeRenderer.cs")
        self.assertEqual(target.line, 44)

    def test_sample_formatter_marks_foreign_file_refs_with_filename(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit()],
            reference_locs=[
                _make_ref("/repo/src/Renderer.cs", 43),
                _make_ref("/repo/src/Other.cs", 17),
                _make_ref("/repo/src/Renderer.cs", 68),
            ],
        )

        self.assertEqual(_format_semantic_sample_locs(group), "L44,Other.cs:L18,L69")

    def test_sample_formatter_omits_ellipsis_when_exactly_three_refs(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit()],
            reference_locs=[
                _make_ref("/repo/src/Renderer.cs", 43),
                _make_ref("/repo/src/Renderer.cs", 55),
                _make_ref("/repo/src/Renderer.cs", 67),
            ],
        )

        samples = _format_semantic_sample_locs(group)
        self.assertEqual(samples, "L44,L56,L68")
        self.assertNotIn("...", samples)

    def test_sample_formatter_falls_back_to_hits_when_no_refs(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[
                _make_hit(line=10),
                _make_hit(line=20),
                _make_hit(line=30),
                _make_hit(line=40),
            ],
            reference_locs=[],
        )

        self.assertEqual(_format_semantic_sample_locs(group), "L11,L21,L31,...")

    def test_group_formatter_omits_type_suffix_when_blank(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="local",
            type_text="",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit()],
            reference_locs=[_make_ref("/repo/src/Renderer.cs", 43)],
        )

        line = _format_semantic_grep_group(0, group)

        self.assertIn("local ctx —", line)
        self.assertNotIn("ctx:", line)

    def test_group_formatter_uses_filename_when_definition_in_other_file(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Contracts.cs",
            definition_line=12,
            definition_character=8,
            hits=[_make_hit(path="/repo/src/Renderer.cs", line=43)],
            reference_locs=[_make_ref("/repo/src/Renderer.cs", 43)],
        )

        line = _format_semantic_grep_group(2, group)

        self.assertIn("def Contracts.cs:L12", line)

    def test_graph_index_rejects_when_no_previous_graph(self) -> None:
        _record_semantic_nav_context("", [])

        result = _graph_target_from_index("0")

        if not isinstance(result, str):
            self.fail("expected error string, got SemanticTarget")
        self.assertIn("No previous semantic graph", result)

    def test_graph_index_rejects_out_of_range(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit()],
        )
        _record_semantic_nav_context("ctx", [group])

        result = _graph_target_from_index("5")

        if not isinstance(result, str):
            self.fail("expected error string, got SemanticTarget")
        self.assertIn("[5] not found", result)
        self.assertIn("'ctx'", result)

    def test_graph_index_rejects_group_with_no_hits(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[],
        )
        _record_semantic_nav_context("ctx", [group])

        result = _graph_target_from_index("0")

        if not isinstance(result, str):
            self.fail("expected error string, got SemanticTarget")
        self.assertIn("no source hits", result)

    def test_resolve_line_target_reports_when_line_missing_from_graph(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit(line=43)],
            reference_locs=[_make_ref("/repo/src/Renderer.cs", 43)],
        )
        _record_semantic_nav_context("ctx", [group])

        result = _resolve_line_target("L999")

        self.assertIsInstance(result, str)
        self.assertIn("L999 was not in the last lsp_grep graph", result)
        self.assertIn("'ctx'", result)

    def test_resolve_line_target_emits_summary_when_line_ambiguous(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit(line=43)],
            reference_locs=[
                _make_ref("/repo/src/Renderer.cs", 77),
                _make_ref("/repo/src/Other.cs", 77),
            ],
        )
        _record_semantic_nav_context("ctx", [group])

        result = _resolve_line_target("L78")

        self.assertIsInstance(result, str)
        self.assertIn("Ambiguous line in last semantic graph", result)
        self.assertIn("Renderer.cs:L78", result)
        self.assertIn("Other.cs:L78", result)

    def test_resolve_line_target_complains_when_no_previous_nav_context(self) -> None:
        _record_semantic_nav_context("", [])

        result = _resolve_line_target("L42")

        self.assertIsInstance(result, str)
        self.assertIn("No previous lsp_grep context", result)

    def test_resolve_line_target_empty_input_explains_shape(self) -> None:
        result = _resolve_line_target("")

        self.assertIsInstance(result, str)
        self.assertIn("Provide target", result)

    def test_identifier_hits_on_line_skips_language_keywords(self) -> None:
        fixture = Path("tmp/test_lsp_symbols_at_keywords_fixture.cs")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text(
            "public static void Render() { return; }\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))

        names = [name for name, _hit in _identifier_hits_on_line(str(fixture), 1)]

        # Only the user-defined identifier survives — keywords like public,
        # static, void, return are filtered through _LINE_POSITION_SKIP_WORDS.
        self.assertEqual(names, ["Render"])

    def test_identifier_hits_on_line_returns_empty_for_blank_line(self) -> None:
        fixture = Path("tmp/test_lsp_symbols_at_blank_fixture.cs")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text("first\n\nthird\n", encoding="utf-8")
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))

        self.assertEqual(_identifier_hits_on_line(str(fixture), 2), [])

    def test_identifier_hits_on_line_handles_missing_file(self) -> None:
        self.assertEqual(_identifier_hits_on_line("tmp/does_not_exist.cs", 1), [])

    def test_identifier_hits_on_line_ignores_comment_tail_identifiers(self) -> None:
        fixture = Path("tmp/test_lsp_symbols_at_comment_fixture.py")
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text("ctx = build()  # commentToken inside\n", encoding="utf-8")
        self.addCleanup(lambda: fixture.unlink(missing_ok=True))

        names = [name for name, _hit in _identifier_hits_on_line(str(fixture), 1)]

        self.assertIn("ctx", names)
        self.assertIn("build", names)
        self.assertNotIn("commentToken", names)

    def test_record_semantic_nav_context_replaces_previous_state(self) -> None:
        first = SemanticGrepGroup(
            key="k1",
            name="alpha",
            kind="arg",
            type_text="",
            definition_path="/repo/src/A.cs",
            definition_line=10,
            definition_character=0,
            hits=[_make_hit(path="/repo/src/A.cs", line=9)],
            reference_locs=[_make_ref("/repo/src/A.cs", 9)],
        )
        _record_semantic_nav_context("alpha", [first])

        # Bare L10 must resolve to the first graph.
        self.assertEqual(_resolve_line_target("L10"), ("/repo/src/A.cs", 10))

        second = SemanticGrepGroup(
            key="k2",
            name="beta",
            kind="local",
            type_text="",
            definition_path="/repo/src/B.cs",
            definition_line=5,
            definition_character=0,
            hits=[_make_hit(path="/repo/src/B.cs", line=4)],
            reference_locs=[_make_ref("/repo/src/B.cs", 4)],
        )
        _record_semantic_nav_context("beta", [second])

        # The earlier alpha graph is gone; L10 from A.cs no longer resolves.
        stale = _resolve_line_target("L10")
        self.assertIsInstance(stale, str)
        self.assertIn("'beta'", stale)

        # The new graph drives bare-line and graph-index lookups.
        self.assertEqual(_resolve_line_target("L5"), ("/repo/src/B.cs", 5))
        target = _graph_target_from_index("0")
        if isinstance(target, str):
            self.fail(target)
        self.assertEqual(target.name, "beta")

    def test_render_memory_alias_survives_graph_replacement(self) -> None:
        first = SemanticGrepGroup(
            key="k1",
            name="alpha",
            kind="arg",
            type_text="",
            definition_path="/repo/src/A.cs",
            definition_line=10,
            definition_character=3,
            hits=[_make_hit(path="/repo/src/A.cs", line=9, character=3)],
            reference_locs=[_make_ref("/repo/src/A.cs", 9, 3)],
        )
        _record_semantic_nav_context("alpha", [first])

        second = SemanticGrepGroup(
            key="k2",
            name="beta",
            kind="local",
            type_text="",
            definition_path="/repo/src/B.cs",
            definition_line=5,
            definition_character=4,
            hits=[_make_hit(path="/repo/src/B.cs", line=4, character=4)],
            reference_locs=[_make_ref("/repo/src/B.cs", 4, 4)],
        )
        _record_semantic_nav_context("beta", [second])

        target = asyncio.run(_resolve_semantic_target("A1"))

        if isinstance(target, str):
            self.fail(target)
        self.assertEqual(target.name, "alpha")
        self.assertEqual(target.path, "/repo/src/A.cs")
        self.assertEqual(target.line, 10)

    def test_lsp_memory_legend_decodes_active_aliases(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit()],
        )
        _record_semantic_nav_context("ctx", [group])

        result = asyncio.run(lsp_memory(action="legend"))

        self.assertIn("legend gen=", result)
        self.assertIn("A=Renderer.cs::Renderer", result)
        self.assertIn("A1=ctx@L44", result)

    def test_bare_numeric_line_resolves_through_last_graph(self) -> None:
        group = SemanticGrepGroup(
            key="k",
            name="ctx",
            kind="arg",
            type_text="RenderContext",
            definition_path="/repo/src/Renderer.cs",
            definition_line=44,
            definition_character=12,
            hits=[_make_hit()],
            reference_locs=[_make_ref("/repo/src/Renderer.cs", 99)],
        )
        _record_semantic_nav_context("ctx", [group])

        # The line-only regex is `L?(\d+)`, so bare digits are accepted too.
        self.assertEqual(_resolve_line_target("100"), ("/repo/src/Renderer.cs", 100))


if __name__ == "__main__":
    unittest.main()
