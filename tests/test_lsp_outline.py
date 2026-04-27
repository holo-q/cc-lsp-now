"""Wave 2 unit coverage for ``lsp_outline``.

``docs/tool-surface.md`` lists ``lsp_outline`` as the Wave 2 replacement for
the raw ``lsp_document_symbols`` tool. Acceptance covers two distinct
contracts:

1. Compact, breadcrumbed output. Each row is ``L<line>  <indent><Kind> <name>``
   so an agent can pivot straight into ``lsp_symbols_at("Lxx")`` without
   re-grepping the file. Indentation tracks nesting; the leading ``Lxx`` is
   always 1-based regardless of which document-symbol shape (DocumentSymbol
   with ``range`` or SymbolInformation with ``location.range``) the server
   returns.

2. Registry hygiene — Wave 2 must cut the raw ``document_symbols`` entry
   from ``_ALL_TOOLS`` and ``TOOL_CAPABILITIES`` ("no aliases, no shims, no
   fallback names" per the Raw Tool Cut Map).

The shared helpers ``_format_outline_tree``, ``_symbols_on_line``, and
``_context_breadcrumb`` are unit-testable without spinning up an LSP server,
so this file pins them directly. ``lsp_outline`` itself is exercised through
its top-level surface (registry presence, capability mapping, async shape);
end-to-end behaviour with a real server stays in live smoke per the docs.
"""
import inspect
import unittest

from cc_lsp_now import server as _server
from cc_lsp_now.server import (
    _ALL_TOOLS,
    TOOL_CAPABILITIES,
    _context_breadcrumb,
    _format_outline_tree,
    _symbols_on_line,
    lsp_outline,
)


class FormatOutlineTreeTests(unittest.TestCase):
    """Row shape contract: ``L<line>  <indent><Kind> <name>`` with two-space
    indent per nesting level. Top-level rows render with no indent; the line
    number is always 1-based.
    """

    def test_top_level_class_uses_one_based_line(self) -> None:
        sym = {
            "name": "Foo",
            "kind": 5,
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 9, "character": 0},
            },
        }

        self.assertEqual(_format_outline_tree(sym), ["L1  Class Foo"])

    def test_nested_children_indent_two_spaces_per_level(self) -> None:
        sym = {
            "name": "Foo",
            "kind": 5,
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 50, "character": 0},
            },
            "children": [
                {
                    "name": "Bar",
                    "kind": 6,
                    "range": {
                        "start": {"line": 4, "character": 4},
                        "end": {"line": 12, "character": 4},
                    },
                    "children": [
                        {
                            "name": "baz",
                            "kind": 13,
                            "range": {
                                "start": {"line": 6, "character": 8},
                                "end": {"line": 6, "character": 20},
                            },
                        }
                    ],
                }
            ],
        }

        self.assertEqual(
            _format_outline_tree(sym),
            [
                "L1  Class Foo",
                "L5    Method Bar",
                "L7      Variable baz",
            ],
        )

    def test_symbol_information_shape_reads_line_from_location_range(self) -> None:
        # SymbolInformation places the range under ``location.uri`` +
        # ``location.range``; the presence of ``uri`` in the resolved loc is
        # the discriminator that picks ``loc.range.start.line`` over
        # ``loc.start.line``.
        sym = {
            "name": "Foo",
            "kind": 12,
            "location": {
                "uri": "file:///repo/foo.py",
                "range": {
                    "start": {"line": 41, "character": 0},
                    "end": {"line": 60, "character": 0},
                },
            },
        }

        self.assertEqual(_format_outline_tree(sym), ["L42  Function Foo"])

    def test_unknown_kind_still_emits_a_row(self) -> None:
        # An unrecognised ``kind`` integer must still surface the symbol so
        # the outline is never silently truncated. ``_symbol_kind_label``
        # falls back to ``Unknown(n)``.
        sym = {
            "name": "WeirdOne",
            "kind": 0,
            "range": {
                "start": {"line": 7, "character": 0},
                "end": {"line": 8, "character": 0},
            },
        }

        rows = _format_outline_tree(sym)

        self.assertEqual(rows, ["L8  Unknown(0) WeirdOne"])


class SymbolsOnLineTests(unittest.TestCase):
    """Outline → ``file:Lx`` jumps rest on ``_symbols_on_line`` resolving a
    line to its declaring/enclosing symbol stack with stable rank ordering.
    """

    def test_declaration_line_outranks_enclosing_range(self) -> None:
        symbols = [
            {
                "name": "Outer",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 30, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 11},
                },
                "children": [
                    {
                        "name": "inner",
                        "kind": 6,
                        "range": {
                            "start": {"line": 5, "character": 4},
                            "end": {"line": 10, "character": 4},
                        },
                        "selectionRange": {
                            "start": {"line": 5, "character": 8},
                            "end": {"line": 5, "character": 13},
                        },
                    }
                ],
            }
        ]

        results = _symbols_on_line(symbols, 5)

        # ``inner`` declares on line 5 (rank 0); ``Outer`` only encloses
        # (rank 1). Rank-0 must come first so a Lx jump anchors at the
        # declaration, not at the surrounding type.
        self.assertEqual(results[0][0], 0)
        self.assertEqual(results[0][2], "Method")
        self.assertEqual(results[0][3], "inner")

    def test_enclosing_range_returned_when_no_declaration_match(self) -> None:
        symbols = [
            {
                "name": "Outer",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 30, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 11},
                },
            }
        ]

        results = _symbols_on_line(symbols, 12)

        self.assertEqual(
            [(rank, name) for rank, _pos, _kind, name in results],
            [(1, "Outer")],
        )

    def test_line_outside_any_range_returns_empty(self) -> None:
        symbols = [
            {
                "name": "Outer",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 5, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 11},
                },
            }
        ]

        self.assertEqual(_symbols_on_line(symbols, 99), [])


class CompactBreadcrumbForOutlineTests(unittest.TestCase):
    """Outline rows share ``_context_breadcrumb`` with ``lsp_grep`` — both
    want ``<file_or_class>:<line>::<callable>`` shape so the agent reads
    them identically. These tests pin the cross-tool contract so a future
    edit can't drift one consumer without the other.
    """

    def test_file_stem_matching_class_collapses_to_stem(self) -> None:
        symbols = [
            {
                "name": "Renderer",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 99, "character": 0},
                },
                "children": [
                    {
                        "name": "Render",
                        "kind": 6,
                        "range": {
                            "start": {"line": 30, "character": 4},
                            "end": {"line": 60, "character": 4},
                        },
                    }
                ],
            }
        ]

        crumb = _context_breadcrumb("/repo/src/Renderer.cs", 31, 8, "Render", symbols)

        self.assertEqual(crumb, "Renderer:31::Render")

    def test_file_stem_mismatch_keeps_filename_with_extension(self) -> None:
        # When the first enclosing type doesn't match the file stem, the
        # breadcrumb keeps ``<filename>::<TypeName>`` as the base. This is
        # the path for files holding multiple top-level types or where the
        # type is named differently from the file (common in Python).
        symbols = [
            {
                "name": "Helper",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 99, "character": 0},
                },
            }
        ]

        crumb = _context_breadcrumb("/repo/src/utils.cs", 1, 0, "Helper", symbols)

        self.assertEqual(crumb, "utils.cs::Helper:1::Helper")

    def test_constructor_renders_as_dot_ctor_and_dedups_query(self) -> None:
        symbols = [
            {
                "name": "Foo",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 30, "character": 0},
                },
                "children": [
                    {
                        "name": "Foo",
                        "kind": 9,
                        "range": {
                            "start": {"line": 3, "character": 4},
                            "end": {"line": 8, "character": 4},
                        },
                    }
                ],
            }
        ]

        # Query "ctor" matches the ``.ctor`` suffix → the breadcrumb dedup
        # rule (``pieces[-1].endswith(query)``) suppresses the trailing
        # duplicate piece.
        crumb = _context_breadcrumb("/repo/src/Foo.cs", 4, 8, "ctor", symbols)
        self.assertEqual(crumb, "Foo:4::.ctor")

    def test_query_appends_when_breadcrumb_does_not_end_with_it(self) -> None:
        # With no enclosing scope the breadcrumb base ends with ``:<line>``
        # so the query is tacked on as the final piece — outline rows must
        # surface the symbol the user asked for even at file scope.
        crumb = _context_breadcrumb("/repo/src/loose.py", 12, 0, "thing", [])
        self.assertEqual(crumb, "loose.py:12::thing")


class OutlineRegistryTests(unittest.TestCase):
    """Wave 2 acceptance: ``lsp_outline`` is registered, capability-gated
    against ``documentSymbolProvider``, and the raw ``document_symbols``
    entry is fully cut from both registries.
    """

    def test_outline_is_in_all_tools(self) -> None:
        self.assertIn("outline", _ALL_TOOLS)

    def test_outline_method_is_document_symbol_request(self) -> None:
        # Capability gating works on the ``initialize`` response, but the
        # underlying LSP method that the tool dispatches must remain
        # ``textDocument/documentSymbol`` — the public name changed, the
        # protocol verb didn't.
        _func, method = _ALL_TOOLS["outline"]
        self.assertEqual(method, "textDocument/documentSymbol")

    def test_outline_capability_is_document_symbol_provider(self) -> None:
        self.assertEqual(
            TOOL_CAPABILITIES.get("outline"),
            "documentSymbolProvider",
        )

    def test_raw_document_symbols_is_absent_from_registry(self) -> None:
        self.assertNotIn("document_symbols", _ALL_TOOLS)

    def test_raw_document_symbols_capability_entry_is_dropped(self) -> None:
        self.assertNotIn("document_symbols", TOOL_CAPABILITIES)

    def test_lsp_outline_is_async_callable(self) -> None:
        # Every public MCP tool wraps a coroutine — a stray sync function
        # would break the ``_wrap_with_header`` registration shim silently.
        self.assertTrue(inspect.iscoroutinefunction(lsp_outline))
        self.assertTrue(inspect.iscoroutinefunction(getattr(_server, "lsp_outline")))


if __name__ == "__main__":
    unittest.main()
