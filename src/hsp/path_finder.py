from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

PathDirection = Literal["out", "in", "any"]


@dataclass(frozen=True, slots=True)
class PathNode:
    key: str
    name: str = ""
    kind: str = ""
    path: str = ""
    line: int = 0
    character: int = 0


@dataclass(frozen=True, slots=True)
class PathEdge:
    source: PathNode
    target: PathNode
    family: str
    direction: str
    label: str = ""
    provenance: str = ""


class EdgeOracle(Protocol):
    async def expand(self, node: PathNode, direction: PathDirection, limit: int) -> Sequence[PathEdge]:
        """Return candidate edges from ``node`` in deterministic or natural order."""


@dataclass(slots=True)
class PathSearchStats:
    explored_edges: int = 0
    pruned_hubs: int = 0
    pruned_branches: int = 0
    budget_exhausted: bool = False
    max_hops: int = 0
    max_edges: int = 0


@dataclass(slots=True)
class PathSearchResult:
    start: PathNode
    goal: PathNode
    paths: list[list[PathEdge]] = field(default_factory=list)
    stats: PathSearchStats = field(default_factory=PathSearchStats)


def _path_node_keys(start: PathNode, path: list[PathEdge]) -> set[str]:
    keys = {start.key}
    for edge in path:
        keys.add(edge.target.key)
    return keys


async def find_paths(
    start: PathNode,
    goal: PathNode,
    oracle: EdgeOracle,
    *,
    direction: PathDirection = "out",
    max_hops: int = 4,
    max_edges: int = 200,
    max_paths: int = 3,
    max_branch: int = 50,
) -> PathSearchResult:
    """Find bounded witness paths between two semantic nodes.

    This intentionally starts as bounded BFS rather than pretending there is a
    universal A* heuristic for source graphs. The useful contract is the budget,
    branch pruning, and evidence-preserving edge sequence.
    """
    max_hops = max(0, max_hops)
    max_edges = max(0, max_edges)
    max_paths = max(1, max_paths)
    max_branch = max(1, max_branch)

    stats = PathSearchStats(max_hops=max_hops, max_edges=max_edges)
    result = PathSearchResult(start=start, goal=goal, stats=stats)

    if start.key == goal.key:
        result.paths.append([])
        return result
    if max_hops == 0 or max_edges == 0:
        stats.budget_exhausted = True
        return result

    queue: deque[tuple[PathNode, list[PathEdge]]] = deque([(start, [])])
    best_depth: dict[str, int] = {start.key: 0}

    while queue and len(result.paths) < max_paths:
        node, path = queue.popleft()
        if len(path) >= max_hops:
            continue

        remaining_edge_budget = max_edges - stats.explored_edges
        if remaining_edge_budget <= 0:
            stats.budget_exhausted = True
            break

        branch_limit = min(max_branch, remaining_edge_budget)
        edges = list(await oracle.expand(node, direction, branch_limit + 1))
        edges.sort(key=lambda edge: (edge.target.key, edge.family, edge.direction, edge.label))
        if len(edges) > branch_limit:
            stats.pruned_hubs += 1
            stats.pruned_branches += len(edges) - branch_limit
            edges = edges[:branch_limit]

        path_keys = _path_node_keys(start, path)
        for edge in edges:
            if stats.explored_edges >= max_edges:
                stats.budget_exhausted = True
                break
            stats.explored_edges += 1

            if edge.target.key in path_keys:
                continue

            next_path = [*path, edge]
            if edge.target.key == goal.key:
                result.paths.append(next_path)
                if len(result.paths) >= max_paths:
                    break
                continue

            if len(next_path) >= max_hops:
                continue

            known_depth = best_depth.get(edge.target.key)
            if known_depth is not None and known_depth < len(next_path):
                continue
            best_depth[edge.target.key] = len(next_path)
            queue.append((edge.target, next_path))

    return result
