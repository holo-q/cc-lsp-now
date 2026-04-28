# Agent-First LSP Tool Surface

`cc-lsp-now` started as a direct MCP bridge over LSP methods. That was the
right bootstrap, but it is not the final agent interface. The stable surface
should expose semantic graph operators and keep raw LSP verbs as internal
plumbing.

The working rule is:

```text
Find semantic nodes -> inspect nodes -> expand graph edges -> stage mutations -> verify.
```

Renderer details are part of this public contract. See
`docs/rendering.md` for the canonical row shapes, graph-handle expectations,
diagnostic repair flow, and preview rules. See `docs/agent-tool-roadmap.md` for
the pending-edit, what-if, witness, multi-agent, and verifier tool roadmap that
extends this surface beyond raw LSP orchestration. See `docs/lsp-path.md` for
the bounded pathfinding operator that connects two known semantic anchors
through one explicit edge family.

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
| `lsp_types` | Show super and/or sub type hierarchy edges. |
| `lsp_path` | Find bounded witness paths between two known anchors. |
| `lsp_diagnostics` | Report diagnostics as the primary verifier surface. |
| `lsp_fix` | Preview and stage code actions/refactors for a location or diagnostic. |
| `lsp_rename` | Preview symbol rename with final-line edits and confirmation. |
| `lsp_move` | Preview file/symbol moves with import/update edits and confirmation. |
| `lsp_session` | Inspect, add, and warm workspaces and LSP sessions. |
| `lsp_confirm` | Commit the currently staged edit transaction. |

`lsp_grep`, `lsp_symbols_at`, `lsp_symbol`, `lsp_goto`, `lsp_refs`,
`lsp_outline`, `lsp_calls`, `lsp_types`, `lsp_path`, `lsp_session`, `lsp_fix`,
`lsp_rename`, and `lsp_move` are the implemented pieces of this surface today.
The graph-aware tools preserve semantic graph context between calls, which is
the pattern the rest of the tools should follow.

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
| `lsp_type_hierarchy_supertypes` | `lsp_types` |
| `lsp_type_hierarchy_subtypes` | `lsp_types` |
| `lsp_code_actions` | `lsp_fix` |
| `lsp_move_file` | `lsp_move` |
| `lsp_move_files` | `lsp_move` |
| `lsp_info` | `lsp_session` |
| `lsp_workspaces` | `lsp_session` |
| `lsp_add_workspace` | `lsp_session` |

Cut without replacement:
- `lsp_completion` — agents do not autocomplete; semantic questions should go
  through `lsp_symbol`, `lsp_goto`, `lsp_refs`, or `lsp_grep`.
- `lsp_inlay_hint` — an editor affordance; type and scope context belongs in
  compact semantic node output.
- `lsp_folding_range` — an editor affordance with no current agent workflow.
- `lsp_code_lens` — an editor affordance; actionable repair belongs in
  `lsp_fix`.
- `lsp_prepare_rename` — folded into `lsp_rename` preview and trace output.
- `lsp_create_file` / `lsp_delete_file` — direct file creation and deletion
  belong to normal file tools. Internal workspace-edit file operations stay
  supported for refactors and confirmations.

Formatting is intentionally excluded from the agent-facing surface. It is
distracting context for agents, creates noisy staged diffs, and is better
handled by editor/save hooks, pre-commit hooks, CI, or occasional direct user
formatter runs. Raw `lsp_formatting` and `lsp_range_formatting` stay out of the
public MCP registry rather than being replaced by a workflow tool.

## Interface Defaults

- Every target-taking tool should accept graph indices (`[N]` from the last
  `lsp_grep`/`lsp_symbols_at`), bare `Lxx` (resolved against the last graph),
  `file:Lx`, `file_path+line`, `file_path+symbol`, full paths, relative paths,
  and unique basenames where applicable.
- Outputs should stay compact, line-oriented, and breadcrumbed: one symbol per
  line. Sample lists are non-exhaustive — a trailing `...` means more exist;
  callers unfold with `lsp_refs` or by raising `max_hits` / `max_groups`.
- Printed rows should generally be navigable. `outline`, `refs`, `goto`, and
  diagnostics should seed the same follow-up context as `grep`, `symbols_at`,
  `calls`, and `types`; diagnostics use `(dN)` handles because repairs target
  diagnostic ranges.
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
landing order is `outline → session → calls → types → fix`:

1. `lsp_outline` *(landed)* — pure read; reuses `_format_outline_tree` plumbing
   and shrinks the registry by one (`lsp_document_symbols`).
2. `lsp_session` *(landed)* — pure read/admin; collapses three tiny raw tools
   (`lsp_info`, `lsp_workspaces`, `lsp_add_workspace`) into one verb-driven
   surface with no semantic-graph plumbing, dropping the public tool count fast.
3. `lsp_calls` *(landed)* — semantic graph operator; introduces `[N]`-target
   propagation through call hierarchy edges, exercising the same nav-context
   recorder used by `lsp_grep` / `lsp_symbols_at`. Cuts both
   `lsp_call_hierarchy_incoming` and `lsp_call_hierarchy_outgoing`.
4. `lsp_types` *(landed)* — semantic graph operator paralleling `lsp_calls`,
   walking type hierarchy super/sub edges from a node and recording results
   into the nav context. Cuts both `lsp_type_hierarchy_supertypes` and
   `lsp_type_hierarchy_subtypes`.
5. `lsp_fix` *(landed)* — preview-and-stage mutation; reuses diagnostic-aware
   target resolution and the `_pending` buffer used by `lsp_rename` / `lsp_move`.
   Cuts `lsp_code_actions` from the public registry.

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
`prepareCallHierarchy`, then incoming and/or outgoing per `direction`.
`max_edges` applies per direction. Results are recorded into the semantic nav
context so callers can `lsp_symbol([3])` / `lsp_refs([3])` on any call edge.
Sample:

```text
Calls for Render (/repo/src/Renderer.cs:L44)
in:
  [0] src/server.py:L3669::_ALL_TOOLS — function — 1 site
out:
  [3] src/server.py:L744::_symbol_kind_label — function — 1 site
  ... stopped at 50 out edge(s); raise max_edges to unfold.
```

```python
async def lsp_types(
    target: str = "",
    direction: str = "both",         # "super" | "sub" | "both"
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_depth: int = 1,
    max_edges: int = 50,
) -> str: ...
```

`lsp_types` resolves the target with `_resolve_semantic_target`, runs
`prepareTypeHierarchy`, then supertypes and/or subtypes per `direction`.
`max_edges` applies per direction. Edges are recorded into the semantic nav
context so callers can chain `lsp_symbol([N])`, `lsp_refs([N])`, or
`lsp_goto([N])` on any returned node. Sample:

```text
Types for IRenderer (/repo/src/IRenderer.cs:L9)
super:
  [0] src/IComponent.cs:L4::IComponent — interface
sub:
  [3] src/Renderer.cs:L44::Renderer — class
  ... stopped at 50 sub edge(s); raise max_edges to unfold.
```

```python
async def lsp_session(
    action: str = "status",          # "status" | "add" | "warm" | "restart"
    path: str = "",                  # for add / warm
    server: str = "",                # for restart; "" = whole chain
) -> str: ...
```

Verbs:

- `status` (default) — build SHA, per-server capability summary, per-folder warmup state. Folds `lsp_info` + `lsp_workspaces` into one block.
- `add path` — proactively spawn the chain, attach the folder, bulk-warm it. Replaces `lsp_add_workspace`.
- `warm path` — re-fire bulk warmup against a registered folder.
- `restart [server]` — shut and immediately respawn a chain server (or the whole chain when empty).

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
line's diagnostics as `(d0)`, `(d1)`, ..., then numbers the edit-backed code
actions as `[0]`, `[1]`, ... and stages them into `_pending` for `lsp_confirm(N)`.
Command-only or no-edit actions render as `[-]` and are excluded from the index.
The `kind` filter narrows by LSP `CodeActionKind` prefix so an agent can ask
for "just organize-imports" without scanning the full menu.

Wave 3 merges mutation utilities and cuts replaced raw tools:

- `lsp_move` *(landed)* — preview-and-stage mutation; absorbs both single-file
  and batched file moves through one verb, reusing the `_pending` buffer used
  by `lsp_rename` / `lsp_fix`. Cuts both `lsp_move_file` and `lsp_move_files`
  from the public registry.
- remove each raw tool from `_ALL_TOOLS` as soon as its replacement is tested.

```python
async def lsp_move(
    from_path: str = "",
    to_path: str = "",
    symbol: str = "",                # optional: resolve source file by symbol name
    moves: str = "",                 # batched: newline- or comma-separated `from=>to` pairs
) -> str: ...
```

`lsp_move` is the single public mutation tool for relocation. Pass `from_path`
+ `to_path` for a single move, `symbol` + `to_path` to resolve the source file
through `workspace/symbol`, or `moves` for a batched set expressed as
`from=>to` pairs separated by newlines or commas; the tool runs
`workspace/willRenameFiles` against the chain, previews the resulting import /
reference edits, and stages them into `_pending` for `lsp_confirm`. Sample:

```text
Move src/old.py -> src/new.py
edits:
  src/app.py:L12 import old -> import new
  ... 3 more edit(s); confirm to apply.
```

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
