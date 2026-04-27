# Agent-First LSP Tool Surface

`cc-lsp-now` started as a direct MCP bridge over LSP methods. That was the
right bootstrap, but it is not the final agent interface. The stable surface
should expose semantic graph operators and keep raw LSP verbs as internal
plumbing.

The working rule is:

```text
Find semantic nodes -> inspect nodes -> expand graph edges -> stage mutations -> verify.
```

## Target Tools

| Tool | Purpose |
|------|---------|
| `lsp_grep` | Find semantic symbol buckets from identifier text candidates. |
| `lsp_symbols_at` | Expand every semantic symbol on a line, with last-graph navigation. |
| `lsp_symbol` | Inspect one semantic node: kind, type, hover/docs, definition, scope, signature, references summary. |
| `lsp_goto` | Resolve destinations for a node: definition, declaration, type definition, implementation. |
| `lsp_refs` | Expand references for a known node or graph index. |
| `lsp_outline` | Show compact file/workspace breadcrumbs. |
| `lsp_calls` | Show incoming and/or outgoing call graph edges. |
| `lsp_diagnostics` | Report diagnostics as the primary verifier surface. |
| `lsp_fix` | Preview and stage code actions/refactors for a location or diagnostic. |
| `lsp_rename` | Preview symbol rename with final-line edits and confirmation. |
| `lsp_move` | Preview file/symbol moves with import/update edits and confirmation. |
| `lsp_format` | Preview document or range formatting as staged edits. |
| `lsp_session` | Inspect, add, and warm workspaces and LSP sessions. |
| `lsp_confirm` | Commit the currently staged edit transaction. |

`lsp_grep`, `lsp_symbols_at`, `lsp_symbol`, `lsp_goto`, and `lsp_refs` are the
first implemented pieces of this surface. They preserve semantic graph context
between calls, which is the pattern the rest of the tools should follow.

## Raw Tool Cut Map

Direction is one-way: raw protocol-shaped tool → workflow replacement. Once the
workflow tool ships, the raw entry is removed from the public MCP registry — no
aliases, no shims, no fallback names. The raw verbs survive only as internal
plumbing inside the workflow tools.

| Raw tool | Replacement |
|----------|-------------|
| `lsp_hover` | `lsp_symbol` |
| `lsp_signature_help` | `lsp_symbol` |
| `lsp_definition` | `lsp_goto` |
| `lsp_declaration` | `lsp_goto` |
| `lsp_type_definition` | `lsp_goto` |
| `lsp_implementation` | `lsp_goto` |
| `lsp_references` | `lsp_refs` |
| `lsp_document_symbols` | `lsp_outline` |
| `lsp_call_hierarchy_incoming` | `lsp_calls` |
| `lsp_call_hierarchy_outgoing` | `lsp_calls` |
| `lsp_code_actions` | `lsp_fix` |
| `lsp_formatting` | `lsp_format` |
| `lsp_range_formatting` | `lsp_format` |
| `lsp_move_file` | `lsp_move` |
| `lsp_move_files` | `lsp_move` |
| `lsp_info` | `lsp_session` |
| `lsp_workspaces` | `lsp_session` |
| `lsp_add_workspace` | `lsp_session` |

`lsp_completion`, `lsp_inlay_hint`, `lsp_folding_range`, and `lsp_code_lens`
should be cut unless repeated agent workflows prove a need for a higher-level
operator around them.

## Interface Defaults

- Every target-taking tool should accept graph indices (`[N]` from the last
  `lsp_grep`/`lsp_symbols_at`), bare `Lxx` (resolved against the last graph),
  `file:Lx`, `file_path+line`, `file_path+symbol`, full paths, relative paths,
  and unique basenames where applicable.
- Outputs should stay compact, line-oriented, and breadcrumbed: one symbol per
  line. Sample lists are non-exhaustive — a trailing `...` means more exist;
  callers unfold with `lsp_refs` or by raising `max_hits` / `max_groups`.
- Mutation tools should preview and stage edits. `lsp_confirm` is the only
  commit operator.
- Capability gating should apply to workflow tools based on the backend methods
  they need, not based on their public names.

## Implementation Waves

Wave 1 built the core node operators:

- `lsp_grep`
- `lsp_symbols_at`
- `lsp_symbol`
- `lsp_goto`
- `lsp_refs`

Wave 2 builds outline, session, graph, and verifier operators. The intended
landing order is `outline → session → calls → fix`:

1. `lsp_outline` — pure read; reuses `_format_outline_tree` plumbing and shrinks
   the registry by one (`lsp_document_symbols`).
2. `lsp_session` — pure read/admin; collapses three tiny raw tools (`lsp_info`,
   `lsp_workspaces`, `lsp_add_workspace`) into one verb-driven surface with no
   semantic-graph plumbing, dropping the public tool count fast.
3. `lsp_calls` — semantic graph operator; introduces `[N]`-target propagation
   through call hierarchy edges, exercising the same nav-context recorder used
   by `lsp_grep` / `lsp_symbols_at`.
4. `lsp_fix` — preview-and-stage mutation; depends on diagnostic-aware target
   resolution and the `_pending` buffer used by `lsp_rename` / `lsp_move`.

### Public API shapes

Every signature below stays one-line agent-first: the first argument is the
graph-aware `target`, the rest are narrow knobs with safe defaults. Output is
breadcrumbed, one-symbol-per-line, with `...` tails when truncated.

```python
async def lsp_calls(
    target: str = "",
    direction: str = "both",         # "in" | "out" | "both"
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_depth: int = 1,
    max_edges: int = 50,
) -> str: ...
```

`lsp_calls` resolves the target with `_resolve_semantic_target`, runs
`prepareCallHierarchy`, then incoming and/or outgoing per `direction`. Results
are recorded into the semantic nav context so callers can `lsp_symbol([3])` /
`lsp_refs([3])` on any call edge. Sample line:

```text
in:
  [0] src/server.py:L3669::_ALL_TOOLS — function — 1 site
out:
  [3] src/server.py:L744::_symbol_kind_label — function — 1 site
... 4 more; raise max_edges to unfold.
```

```python
async def lsp_session(
    action: str = "status",          # "status" | "add" | "warm" | "restart"
    path: str = "",                  # for add / warm
    server: str = "",                # for restart; "" = whole chain
) -> str: ...
```

`status` is the default and folds `lsp_info` + `lsp_workspaces` into one block:
build SHA, per-server one-line capability summary, then per-folder warmup state.
`add` mirrors today's `lsp_add_workspace` (proactively spawns the chain, warms
the new folder). `warm` re-fires bulk warmup against an existing folder.
`restart` shuts down a chain server and lets the lazy `_get_client` respawn it
on the next request.

```python
async def lsp_fix(
    target: str = "",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    diagnostic_index: int = -1,      # -1 = all diagnostics on the line
    kind: str = "",                  # filter, e.g. "quickfix" / "refactor.extract"
) -> str: ...
```

`lsp_fix` accepts the same target shapes as the rest of Wave 1/2, lists the
line's diagnostics as `[d0]`, `[d1]`, ..., then numbers the edit-backed code
actions as `[0]`, `[1]`, ... and stages them into `_pending` for `lsp_confirm(N)`.
Command-only or no-edit actions render as `[-]` and are excluded from the index.
The `kind` filter narrows by LSP `CodeActionKind` prefix so an agent can ask
for "just organize-imports" without scanning the full menu.

Wave 3 merges mutation utilities and cuts replaced raw tools:

- `lsp_move`
- `lsp_format`
- remove each raw tool from `_ALL_TOOLS` as soon as its replacement is tested.

## Acceptance Checks

- Each new workflow tool has unit coverage for graph index targets, explicit
  `file:Lx`, unique basenames, and symbol disambiguation.
- Registry tests or assertions prove replaced raw tools are absent from
  `_ALL_TOOLS`.
- Existing checks remain green:

```text
uvx ruff check src tests
uv run --frozen ty check src tests
uv run --frozen python -m unittest discover -s tests
```

Live smoke should cover at least `ty` and `csharp-ls` after each implementation
wave.
