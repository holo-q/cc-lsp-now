import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from hsp import server as _server


def _run(coro: Any) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str)
    return result


def _target(name: str, line: int) -> _server.SemanticTarget:
    return _server.SemanticTarget(
        uri="file:///repo/src/Workspace.cs",
        pos={"line": line - 1, "character": 4},
        path="/repo/src/Workspace.cs",
        line=line,
        character=4,
        name=name,
    )


class LspRefsMultiTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        _server._record_semantic_nav_context("", [])

    def test_file_symbol_ambiguity_expands_all_matches_for_read_only_refs(self) -> None:
        targets = [
            _target("SelectArtifact", 338),
            _target("SelectArtifactRelative", 349),
        ]
        sections: dict[str, tuple[list[str], _server.SemanticGrepGroup | None]] = {
            "SelectArtifact": (
                [
                    "match 0 SelectArtifact (/repo/src/Workspace.cs:L338): 2",
                    "  Workspace.cs:L338  public ImageArtifact? SelectArtifact(ArtifactId? artifact)",
                    "  Workspace.cs:L357  return SelectArtifact(entries[0].artifact);",
                ],
                _server.SemanticGrepGroup(
                    key="select",
                    name="SelectArtifact",
                    kind="method",
                    type_text="",
                    definition_path="/repo/src/Workspace.cs",
                    definition_line=338,
                    definition_character=4,
                    hits=[
                        _server.SemanticGrepHit(
                            path="/repo/src/Workspace.cs",
                            line=337,
                            character=4,
                            line_text="public ImageArtifact? SelectArtifact(ArtifactId? artifact)",
                            uri="file:///repo/src/Workspace.cs",
                            pos={"line": 337, "character": 4},
                        )
                    ],
                ),
            ),
            "SelectArtifactRelative": (
                [
                    "match 1 SelectArtifactRelative (/repo/src/Workspace.cs:L349): 1",
                    "  Workspace.cs:L349  public ImageArtifact? SelectArtifactRelative(int step)",
                ],
                None,
            ),
        }

        async def fake_section(
            resolved: _server.SemanticTarget,
            _include_declaration: bool,
            _max_refs: int,
            *,
            heading: str = "",
        ) -> tuple[list[str], _server.SemanticGrepGroup | None]:
            self.assertTrue(heading.startswith("match "))
            return sections[resolved.name]

        with patch.object(_server, "_resolve_symbol_targets", AsyncMock(return_value=targets)):
            with patch.object(_server, "_reference_section_for_target", side_effect=fake_section):
                result = _run(_server.lsp_refs(file_path="Workspace.cs", symbol="SelectArtifact"))

        self.assertIn("References for 2 matches of 'SelectArtifact'", result)
        self.assertIn("match 0 SelectArtifact", result)
        self.assertIn("match 1 SelectArtifactRelative", result)
        self.assertNotIn("Multiple matches — pass line=", result)

        first = _server._graph_target_from_index("0")
        if isinstance(first, str):
            self.fail(first)
        self.assertEqual(first.name, "SelectArtifact")
        second = _server._graph_target_from_index("1")
        if isinstance(second, str):
            self.fail(second)
        self.assertEqual(second.name, "SelectArtifactRelative")


class LspRefsRustWarmupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._last_server = _server._last_server

    def tearDown(self) -> None:
        _server._last_server = self._last_server

    def test_rust_empty_references_retry_until_indexed(self) -> None:
        reference_calls = 0
        resolved = _target("focus_pane_on_socket", 734)
        ref = {
            "uri": "file:///repo/src/backend/kitty.rs",
            "range": {
                "start": {"line": 733, "character": 15},
                "end": {"line": 733, "character": 35},
            },
        }

        async def fake_request(_method: str, _params: dict[str, Any], *, uri: str | None = None) -> list[dict[str, Any]]:
            nonlocal reference_calls
            _server._last_server = "rust-analyzer"
            if _method != "textDocument/references":
                return []
            reference_calls += 1
            return [] if reference_calls < 3 else [ref]

        with patch.object(_server, "_request", side_effect=fake_request):
            with patch.object(_server, "_sleep_for_empty_references", new=AsyncMock()) as sleep:
                lines, group = asyncio.run(_server._reference_section_for_target(resolved, True, 100))

        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(group.reference_locs, [ref])
        self.assertGreaterEqual(reference_calls, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertIn("References for symbol focus_pane_on_socket: 1", lines)
        self.assertIn("waited for rust-analyzer references to warm up", lines)

    def test_rust_empty_references_reports_indexing_notice_after_wait(self) -> None:
        resolved = _target("focus_pane_on_socket", 734)

        async def fake_request(_method: str, _params: dict[str, Any], *, uri: str | None = None) -> list[dict[str, Any]]:
            _server._last_server = "rust-analyzer"
            return []

        with patch.object(_server, "_request", side_effect=fake_request):
            with patch.object(_server, "_sleep_for_empty_references", new=AsyncMock()) as sleep:
                lines, group = asyncio.run(_server._reference_section_for_target(resolved, True, 100))

        self.assertIsNone(group)
        self.assertEqual(sleep.await_count, _server._REFERENCES_EMPTY_RETRIES - 1)
        self.assertIn(
            "rust-analyzer returned no references after warmup wait; try again if indexing is still running.",
            lines,
        )


if __name__ == "__main__":
    unittest.main()
