# lsp_path

`lsp_path` is the bridge operator for agent navigation. It answers:

```text
Show a bounded witness path from A to B over one explicit semantic edge family.
```

The intent is hop collapse. An agent often knows two anchors in a codebase and
then spends several turns expanding calls, references, and type edges by hand.
`lsp_path` should compress that traversal into one auditable result while
preserving the evidence for each hop.

This is not generic codebase graph reasoning. A source tree contains multiple
overlaid graphs with incompatible meanings: call graph, type graph, reference
graph, file containment, imports, diagnostics, tests, git history, and text
matches. A path that silently mixes those edges can look meaningful while
proving very little. The first contract is therefore narrow: one query, one edge
family, bounded budget, explicit provenance.

## Public Shape

Target signature:

```python
async def lsp_path(
    from_target: str = "",
    to_target: str = "",
    via: str = "calls",             # "calls" first; "types" and "refs" next
    direction: str = "out",         # "out" | "in" | "any"
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_hops: int = 4,
    max_edges: int = 200,
    max_paths: int = 3,
    exclude: str = "",
) -> str: ...
```

`from_target` and `to_target` should accept the same target language as the
other graph tools:

- graph indices from the last result, such as `[3]`;
- bare `L42` resolved against the last semantic graph;
- explicit `file:L42`;
- `file_path` plus `symbol` or `line`;
- full paths, relative paths, and unique basenames where applicable.

`file_path`, `symbol`, and `line` are compatibility fields for the source
anchor. They should not replace `from_target` and `to_target` once both
endpoints are known.

## Edge Families

`via` names the semantic edge family. It should not default to a mixed graph.

| `via` | Meaning | First status |
|-------|---------|--------------|
| `calls` | Call hierarchy edges from `lsp_calls`. | Ship first. |
| `types` | Type hierarchy edges from `lsp_types`. | Ship after the edge oracle is factored. |
| `refs` | Reference edges from `lsp_refs`. | Ship after reference rows are graph-addressable. |

`direction` is evaluated inside the selected family:

- `out`: walk outgoing edges from the source anchor.
- `in`: walk incoming edges toward the source anchor.
- `any`: allow either direction, but print the direction of every hop.

`direction="any"` is useful when the agent wants connection evidence, but it
must be explicit in output because it is weaker than a directed reachability
claim.

Mixed paths are not a v1 feature. If they are added later, every hop must show
its edge kind and provenance, and the result must read as "possible bridge",
not as proof of runtime flow.

## Relationship To Existing Tools

`lsp_calls`, `lsp_types`, and `lsp_refs` are fanout tools. They answer "what is
near this node?" `lsp_path` coordinates those expansions for known endpoints.

`lsp_neighborhood` remains the better primitive when the agent does not know the
destination and wants blast radius. `lsp_route` can come later for multiple
anchors, bug-report spines, or test-to-production trails. `lsp_connect` should
not ship as a separate v1 verb; the tree-shaped version is harder to make
honest than pairwise paths and neighborhoods.

## Search Policy

The first implementation should be a lazy bounded search, not a prebuilt
workspace index.

Recommended v1:

1. Resolve both endpoints to semantic nodes.
2. Use a typed edge oracle for one family, initially calls only.
3. Run bidirectional BFS or Dijkstra with zero heuristic.
4. Stop at `max_hops`, `max_edges`, and an internal branch cap.
5. Return up to `max_paths` shortest witness paths.

A* is not a v1 requirement. There is no reliable universal spatial heuristic
for code graphs yet; the valuable part is not the star, it is the edge oracle,
budgeting, and rendering.

Hub handling is mandatory. High-degree nodes such as loggers, dependency
containers, framework bases, and common interfaces can poison results. The
search should cap branch expansion per node, mark pruned hubs, and avoid
silently routing through a high-degree node just because it connects everything.

No result should claim global absence. The correct empty state is:

```text
No path from A to B within max_hops=4, max_edges=200 via calls.
```

That means "not found in this bounded semantic search," not "no runtime path
exists."

## Output Contract

Output follows `docs/rendering.md`: compact, line-oriented, graph-addressable,
and honest about truncation.

Example success:

```text
Paths from [0] Renderer:44::Render to [4] Store:21::Persist via calls direction=out
[P0] cost 3 hops 3 verified
  [0] L44 ::Renderer:: method Render
   --calls--> [1] L88 ::Renderer:: method Update
   --calls--> [2] L37 ::Pipeline:: method Flush
   --calls--> [4] L21 ::Store:: method Persist
Pruned: 43 edges, 2 hubs, 7 branches; raise max_edges to unfold.
```

Example bounded miss:

```text
No path from [0] Renderer:44::Render to [4] Store:21::Persist within max_hops=4 via calls.
Explored 113 edges; pruned 2 hubs. This is not proof no runtime path exists.
```

Path handles use their own namespace:

- `[N]` remains a semantic node handle.
- `[P0]` is a path handle.
- Edge handles can be added later as `(e0)` if edge inspection becomes useful.

Every printed semantic node should be navigable through the same graph context
as `lsp_calls` and `lsp_types`: `lsp_symbol([N])`, `lsp_refs([N])`,
`lsp_calls([N])`, and follow-up `lsp_path` should work without re-resolving the
file by hand.

## Provenance

Each path is a witness, so every hop needs a provenance story:

- edge family: `calls`, `types`, or `refs`;
- direction used for that hop;
- language-server method behind the edge when trace output is enabled;
- server label and snapshot stamp once snapshots exist;
- explicit degradation marker for any future text or heuristic fallback.

Unresolved text hits must never render like semantic edges. If a future path
uses fallback evidence, the edge label should make that plain, for example:

```text
--hits?--> [9] L77 ::Config:: string "Foo"
```

## Guardrails

`lsp_path` should refuse or degrade loudly when the query is underspecified.

Guardrails:

- require both endpoints;
- require one explicit edge family;
- keep hop and edge budgets small by default;
- cap branch expansion and report pruning;
- preserve direction in output;
- preserve semantic provenance;
- distinguish "no bounded path" from "no path";
- avoid formatting or source pretty-printing concerns;
- do not use mixed edge search in v1.

Anti-goals:

- not a runtime tracer;
- not a dataflow analyzer;
- not a dependency analyzer;
- not an architecture reasoner;
- not a replacement for `lsp_neighborhood`, `lsp_refs`, or `lsp_calls`;
- not a proof engine for dynamic dispatch, reflection, generated code, or DI.

## Implementation Slice

Build in layers:

1. Document this contract and add renderer golden cases.
2. Add a pure pathfinder over an `EdgeOracle` protocol and fake graphs.
3. Implement calls-only `LspEdgeOracle` using call hierarchy expansion.
4. Expose `lsp_path` with calls-only support and bounded rendering.
5. Factor `lsp_types` and `lsp_refs` into the same oracle once their rows seed
   graph context consistently.
6. Add optional path handles, snapshots, and broker-backed caches later.

Suggested internal modules:

- `path_finder.py`: pure BFS/Dijkstra, budgets, hub pruning, path ranking.
- `edge_oracle.py`: protocol for typed semantic edge expansion.
- `lsp_edge_oracle.py`: LSP-backed implementation for calls, then types/refs.

The direct MCP server should stay useful without the broker. The broker becomes
valuable later because pathfinding benefits from warm sessions, shared indexes,
snapshot provenance, and cached edge expansions.

## QA

Synthetic graph fixtures should cover the real failure modes before live LSP
smoke:

- linear chain;
- diamond with multiple shortest paths;
- cycle;
- disconnected graph;
- hub trap;
- asymmetric incoming/outgoing graph;
- budget exhaustion;
- branch cap truncation;
- duplicate symbols and basename target resolution;
- unresolved endpoint;
- multiple edge families with mixed search disabled.

Optional live smoke can use the Dapper C# fixture after the pure tests are
stable. Live tests should assert shape and provenance more than exact graph
contents, because language-server output varies by version and project load
state.

## Open Questions

- Should `max_paths` default to `1` for compactness or `3` for comparison?
- Should path handles `[P0]` be inspectable immediately, or only printed?
- Do edge handles `(e0)` carry enough value to justify another namespace?
- Should `via="refs"` walk symbol references as undirected by default, or should
  it expose separate `definition -> refsite` and `refsite -> symbol` edges?
- Which hub thresholds should be global defaults versus language-specific
  policy?
