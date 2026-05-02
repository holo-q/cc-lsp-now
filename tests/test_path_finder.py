import asyncio
import unittest

from hsp.path_finder import PathDirection, PathEdge, PathNode, find_paths


def _node(key: str) -> PathNode:
    return PathNode(key=key, name=key, kind="method", path=f"/repo/{key}.cs", line=1)


class FakeOracle:
    def __init__(self, graph: dict[str, list[str]]):
        self.graph = graph
        self.calls: list[tuple[str, PathDirection, int]] = []

    async def expand(self, node: PathNode, direction: PathDirection, limit: int) -> list[PathEdge]:
        self.calls.append((node.key, direction, limit))
        edges: list[PathEdge] = []
        for target_key in self.graph.get(node.key, [])[:limit]:
            edges.append(PathEdge(
                source=node,
                target=_node(target_key),
                family="calls",
                direction=direction,
            ))
        return edges


def _run(coro):
    return asyncio.run(coro)


class PathFinderTests(unittest.TestCase):
    def test_linear_chain_finds_witness_path(self) -> None:
        oracle = FakeOracle({"A": ["B"], "B": ["C"]})

        result = _run(find_paths(
            _node("A"),
            _node("C"),
            oracle,
            max_hops=2,
            max_edges=10,
        ))

        self.assertEqual([[edge.target.key for edge in path] for path in result.paths], [["B", "C"]])
        self.assertEqual(result.stats.explored_edges, 2)
        self.assertFalse(result.stats.budget_exhausted)

    def test_no_path_is_bounded_not_global_absence(self) -> None:
        oracle = FakeOracle({"A": ["B"], "B": []})

        result = _run(find_paths(
            _node("A"),
            _node("C"),
            oracle,
            max_hops=2,
            max_edges=10,
        ))

        self.assertEqual(result.paths, [])
        self.assertEqual(result.stats.explored_edges, 1)
        self.assertFalse(result.stats.budget_exhausted)

    def test_branch_cap_marks_hub_pruning(self) -> None:
        oracle = FakeOracle({"A": ["B", "C", "D", "E"]})

        result = _run(find_paths(
            _node("A"),
            _node("E"),
            oracle,
            max_hops=1,
            max_edges=10,
            max_branch=2,
        ))

        self.assertEqual(result.paths, [])
        self.assertEqual(result.stats.pruned_hubs, 1)
        self.assertEqual(result.stats.pruned_branches, 1)

    def test_max_edges_budget_stops_search(self) -> None:
        oracle = FakeOracle({"A": ["B", "C"], "B": ["D"], "C": ["D"]})

        result = _run(find_paths(
            _node("A"),
            _node("D"),
            oracle,
            max_hops=2,
            max_edges=1,
        ))

        self.assertEqual(result.paths, [])
        self.assertTrue(result.stats.budget_exhausted)

    def test_same_start_and_goal_returns_zero_hop_path(self) -> None:
        oracle = FakeOracle({})

        result = _run(find_paths(_node("A"), _node("A"), oracle))

        self.assertEqual(result.paths, [[]])
        self.assertEqual(oracle.calls, [])
