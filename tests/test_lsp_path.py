import asyncio
import inspect
import unittest
from collections.abc import Coroutine
from typing import Any

from hsp import server as _server
from hsp.server import _ALL_TOOLS, TOOL_CAPABILITIES


def _path_attr() -> Any:
    return getattr(_server, "lsp_path", None)


def _run(coro: Coroutine[Any, Any, str]) -> str:
    result = asyncio.run(coro)
    assert isinstance(result, str), f"lsp_path returned non-str: {type(result)!r}"
    return result


def _item(name: str, uri: str, line: int, char: int = 4) -> dict[str, Any]:
    return {
        "name": name,
        "kind": 6,
        "uri": uri,
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": line + 5, "character": 0},
        },
        "selectionRange": {
            "start": {"line": line, "character": char},
            "end": {"line": line, "character": char + len(name)},
        },
    }


class LspPathSignatureTests(unittest.TestCase):
    def test_lsp_path_is_async_callable(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(_path_attr()))

    def test_lsp_path_signature_matches_docs(self) -> None:
        sig = inspect.signature(_path_attr())
        self.assertEqual(
            list(sig.parameters),
            [
                "from_target",
                "to_target",
                "via",
                "direction",
                "file_path",
                "symbol",
                "line",
                "max_hops",
                "max_edges",
                "max_paths",
                "exclude",
            ],
            f"lsp_path signature drifted from docs/lsp-path.md: {sig}",
        )

    def test_lsp_path_defaults_are_bounded_and_calls_only(self) -> None:
        sig = inspect.signature(_path_attr())
        self.assertEqual(sig.parameters["via"].default, "calls")
        self.assertEqual(sig.parameters["direction"].default, "out")
        self.assertEqual(sig.parameters["max_hops"].default, 4)
        self.assertEqual(sig.parameters["max_edges"].default, 200)
        self.assertEqual(sig.parameters["max_paths"].default, 3)


class LspPathRegistryTests(unittest.TestCase):
    def test_path_is_in_all_tools(self) -> None:
        self.assertIn("path", _ALL_TOOLS)

    def test_path_registered_function_matches_module_attr(self) -> None:
        func, _method = _ALL_TOOLS["path"]
        self.assertIs(func, getattr(_server, "lsp_path", None))

    def test_path_capability_is_call_hierarchy_provider(self) -> None:
        self.assertEqual(TOOL_CAPABILITIES["path"], "callHierarchyProvider")

    def test_path_method_label_is_non_empty_string(self) -> None:
        _func, method = _ALL_TOOLS["path"]
        self.assertIsInstance(method, str)
        self.assertTrue(method)


class LspPathValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        _server._record_semantic_nav_context("", [])

    def test_invalid_via_returns_string_without_resolving(self) -> None:
        result = _run(_path_attr()(from_target="[0]", to_target="[1]", via="mixed"))
        self.assertIn("via must be 'calls'", result)

    def test_invalid_direction_returns_valid_choices(self) -> None:
        result = _run(_path_attr()(from_target="[0]", to_target="[1]", direction="sideways"))
        self.assertIn("direction must be one of", result)

    def test_missing_destination_is_explicit(self) -> None:
        result = _run(_path_attr()(from_target="[0]"))
        self.assertIn("Provide to_target", result)

    def test_graph_index_routes_through_semantic_resolver(self) -> None:
        result = _run(_path_attr()(from_target="[0]", to_target="[1]"))
        self.assertIn("No previous semantic graph", result)


class LspPathRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_resolve: Any = getattr(_server, "_resolve_semantic_target", None)
        self._saved_request: Any = getattr(_server, "_request", None)
        self._saved_nav = list(getattr(_server, "_last_semantic_groups", []))
        _server._record_semantic_nav_context("", [])

        self.a = _item("A", "file:///repo/A.cs", 0)
        self.b = _item("B", "file:///repo/B.cs", 1)
        self.c = _item("C", "file:///repo/C.cs", 2)

        async def fake_resolve(target: str = "", *_args: Any, **_kwargs: Any) -> Any:
            if target == "from":
                return _server.SemanticTarget(
                    uri="file:///repo/A.cs",
                    pos={"line": 0, "character": 4},
                    path="/repo/A.cs",
                    line=1,
                    character=4,
                    name="A",
                )
            if target == "to":
                return _server.SemanticTarget(
                    uri="file:///repo/C.cs",
                    pos={"line": 2, "character": 4},
                    path="/repo/C.cs",
                    line=3,
                    character=4,
                    name="C",
                )
            return "unexpected target"

        self._method_calls: list[str] = []

        async def fake_request(method: str, params: dict | None, **_kwargs: Any) -> Any:
            self._method_calls.append(method)
            if method == "textDocument/prepareCallHierarchy":
                uri = params["textDocument"]["uri"] if params else ""
                return [self.a] if uri.endswith("A.cs") else [self.c]
            if method == "callHierarchy/outgoingCalls":
                item = params["item"] if params else {}
                if item["name"] == "A":
                    return [{"to": self.b, "fromRanges": [{"start": {"line": 0, "character": 10}}]}]
                if item["name"] == "B":
                    return [{"to": self.c, "fromRanges": [{"start": {"line": 1, "character": 10}}]}]
                return []
            return []

        setattr(_server, "_resolve_semantic_target", fake_resolve)
        setattr(_server, "_request", fake_request)

    def tearDown(self) -> None:
        setattr(_server, "_resolve_semantic_target", self._saved_resolve)
        setattr(_server, "_request", self._saved_request)
        _server._record_semantic_nav_context("", self._saved_nav)

    def test_calls_path_renders_witness_and_records_graph_nodes(self) -> None:
        result = _run(_path_attr()(from_target="from", to_target="to", max_hops=2, max_edges=10))

        self.assertIn("[P0] cost 2 hops 2 verified", result)
        self.assertIn("--calls-->", result)
        self.assertIn("::A:: method A", result)
        self.assertIn("::C:: method C", result)
        self.assertIn("callHierarchy/outgoingCalls", self._method_calls)
        self.assertGreaterEqual(len(_server._last_semantic_groups), 3)

    def test_bounded_miss_says_not_runtime_proof(self) -> None:
        result = _run(_path_attr()(from_target="from", to_target="to", max_hops=1, max_edges=10))

        self.assertIn("No path", result)
        self.assertIn("not proof no runtime path exists", result)
