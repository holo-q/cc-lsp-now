# Agent Tool Roadmap

This note records the next wave of ideas from the tool-surface audit. It is not
an implementation commitment for every item. The purpose is to keep the product
direction durable: reduce reasoning hops, make semantic state explicit, and
turn "apply then debug" into "predict, stage, verify, then apply."

## Core Model

The current workflow is:

```text
find semantic nodes -> inspect nodes -> expand graph edges -> stage mutations -> confirm
```

The next workflow should be:

```text
find -> inspect -> expand -> stage -> predict -> confirm -> witness
```

Staging means an LSP operation has produced a `WorkspaceEdit` and `cc-lsp-now`
has rendered a preview, but disk has not changed yet. `lsp_confirm` is the
commit operator.

Current limitation: staging and graph memory are single-slot process globals.
Parallel agents can overwrite each other's pending edit or graph context.

## Pending Edits

A pending edit is a staged candidate waiting for confirmation:

```text
lsp_rename(GetOutputTexture -> GetArtifactTexture)
# preview rendered, WorkspaceEdit stored
lsp_confirm(0)
# edit applied to disk
```

The current buffer is global. If one agent stages a rename and another stages a
fix before the first confirms, the second preview replaces the first. This is
fine for one agent, but wrong for multi-agent work.

Planned shape:

```text
lsp_rename(..., stage="history-ui-rename")
lsp_fix(..., stage="missing-using")
lsp_confirm(stage="history-ui-rename", index=0)
```

Minimal implementation:

- keep `default` stage behavior for compatibility;
- store pending buffers by name or caller id;
- list/drop pending buffers with `lsp_stage`;
- refuse confirmation when the touched files drift from the preview snapshot.

## What-If Queries

`lsp_what_if` means: run a read-only tool as if a staged edit had already been
applied, without writing the edit to disk.

Example:

```text
lsp_rename(GetOutputTexture -> GetArtifactTexture)
lsp_what_if(edit_source="pending", tool="diagnostics")
```

Expected answer:

```text
diagnostics after pending rename:
  resolved 2
  added 0
  unchanged 4
```

Implementation sketch:

1. Build the edited text from the pending `WorkspaceEdit`.
2. Send `didChange` overlays to the language server.
3. Run the requested read-only tool.
4. Restore the original document contents in the language-server session.

This is a prediction primitive. It lets an agent reject a bad rename, move, or
fix before touching disk. Server support will vary; the output must say when a
server cannot answer reliably from unsaved overlays.

## Witnessed Confirmation

`lsp_witness` is the verifier form of `lsp_confirm`.

```text
lsp_witness(index=0, signals="diagnostics,refs,calls")
```

It should:

1. capture selected signals before applying the pending edit;
2. apply the candidate;
3. re-run the same signals on touched files or symbols;
4. report the delta.

Example:

```text
Applied [rename 0]: GetOutputTexture -> GetArtifactTexture
diagnostics: +0 -0 unchanged 3
refs: GetArtifactTexture refs 4 (was GetOutputTexture refs 4)
calls: unchanged
```

This makes the reward signal explicit. `lsp_confirm` can stay as the raw commit
operator; `lsp_witness` is the agent-safe default for non-trivial mutations.

## Diagnostic Repair Flow

`lsp_fix` is the correct tool name for code actions and repairs. The repair path
should still start from diagnostics, because that is where the agent discovers
the problem and should see the available next hop.

Near-term direction:

- `lsp_diagnostics` renders `(dN)` handles.
- Diagnostic rows show whether code actions exist.
- `lsp_fix((d0))` uses that diagnostic handle directly.
- `lsp_fix` renders real edit previews, not just action titles.

Higher-level repair tools:

- `lsp_root_cause`: cluster diagnostics by code, message template, and
  referenced identifier.
- `lsp_bisect_fix`: try each code action in an overlay and report which one
  clears diagnostics without adding new ones.
- `lsp_diag_baseline` / `lsp_diag_diff`: store and compare diagnostic snapshots.

The goal is not to replace `lsp_fix`. The goal is to make `lsp_fix` visible at
the moment the agent sees the diagnostic.

## Multi-Agent Durability

Several current globals need namespacing or durability before parallel agent
work is safe:

- `_pending`: one global pending edit buffer.
- `_last_semantic_nav`: one global graph.
- `_last_semantic_groups`: one global semantic group set.
- process-local logs and warm state with no durable trace.

The coordination layer should be ambient first. Agents need a compressed signal
when they catch the next bus, not a permission fight before every edit. See
`docs/agent-bus.md` for the bus-window design: append-only events, timed
questions, hook-fed notices, and warn-only digests.

Planned primitives:

- `lsp_stage`: list, name, drop, and confirm staged edits.
- `lsp_pin` / `lsp_recall`: name the current semantic graph and restore it
  later; allow `[pin/3]` addressing.
- `lsp_snapshot`: stamp results with git revision, dirty hash, server label,
  and touched-file hashes.
- `lsp_journal` / `lsp_revert`: append every confirmed mutation and allow
  safe inverse edits when touched content has not drifted.
- `lsp_trace`: write JSONL request/response summaries for debugging and replay.
- `lsp_log`: append coordination events, notes, timed questions, replies, and
  hook digests.

Broker-era primitives:

- broker-backed event log: one workspace journal shared across agents.
- bus windows: timed questions with scoped event/reply aggregation.
- hook weather: session/edit/test/commit notices that print only when useful.
- `lsp_predict_conflict`: compare staged edits across callers.
- `lsp_who`: active sessions, callers, stages, open questions, recent confirms.
- `lsp_subscribe`: long-poll diagnostics, confirms, bus events, and file changes.

The direct MCP server can implement named buffers, pins, snapshots, journals,
traces, and a local `lsp_log` first. Claims and leases are not the primary
model; if they ever exist, they should be opt-in hard policy layered behind the
warn-only bus. Conflict prediction becomes much more useful once a broker owns
multiple callers.

## High-Leverage Tool Ideas

These tools are not simple LSP wrappers. They combine semantic data with text
search, git, diagnostics, tests, and staged edits.

### `lsp_impact`

Classify the blast radius of a planned refactor.

Input:

```text
lsp_impact(target="[0]")
```

Output buckets:

```text
12 callsites pass (a,b)
3 callsites await the result
1 callsite ignores return
2 callsites use keyword timeout=
```

This compresses "read every reference" into callsite-shape clusters.

### `lsp_test_targets`

Select tests affected by a symbol or diff.

Implementation starts with incoming call graph edges, walks toward files
matching `LSP_TEST_PATTERNS`, and ranks tests by graph distance:

```text
tests/test_renderer.py::test_texture_update - depth 2 via Render -> UpdateTexture
```

### `lsp_history`

Symbol-scoped git history.

Use LSP ranges to derive the symbol span, then run git history for that span.
Output should summarize introduction, signature changes, churn, and last touch.

### `lsp_dead`

Find unreferenced or test-only symbols under a file, module, or diff.

Use outline plus refs, with text-search warnings for string/reflection usage:

```text
[0] Helper.parse_old - 0 cross-file refs, 0 text mentions
[1] LegacyMode - tests-only refs
```

### `lsp_migrate`

Plan structural migrations beyond rename.

Given an old/new example or patch, infer an `ast-grep` style rewrite, scope it
through semantic references, preview edits, and stage them as one transaction.

### `lsp_flow`

Summarize actual argument values passed to a parameter:

```text
param mode receives "fast" (4), "slow" (1), variable Literal["fast","slow"] (2)
```

This makes default changes and signature migrations evidence-driven.

### `lsp_neighborhood`

One-call blast radius graph: refs, calls, and type edges from a target out to N
hops, capped and indexed.

### `lsp_path`

Bounded bridge search between two known semantic anchors over one explicit edge
family. This is the hop-collapser for "how does A reach B?" without turning the
entire codebase into a vague mixed graph. See `docs/lsp-path.md` for the public
contract, rendering shape, guardrails, and implementation slice.

### `lsp_assert`

Small semantic guardrail predicates:

```text
lsp_assert(target="[0]", predicate="refs == 0")
lsp_assert(target="[0]", predicate="diagnostics_clean")
```

Agents should not have to parse a count from prose when the desired operation
needs a boolean gate.

## QA And Trace Roadmap

The Dapper fixture gives one real C# smoke layer. The next QA layers should make
rendering and server behavior auditable:

- renderer golden tests for output contracts;
- `LSP_TRACE=tmp/traces/run.jsonl` to record method, params digest, response
  digest, latency, server, and fallback use;
- synthetic micro-fixtures per language for rename, references, diagnostics,
  quick fixes, and moves;
- cross-LSP conformance matrix for supported server plugins;
- roundtrip tests: rename A -> B -> A and move A -> B -> A leave a clean diff;
- replay tests from captured traces so renderer regressions do not require a
  live language server.

The trace should say when fallback paths trigger. If the server returns null and
the tool uses `LSP_EMPTY_FALLBACK`, that is not invisible success; it is
provenance that belongs in logs and test artifacts.

## Suggested Implementation Order

1. Renderer contract and diagnostics-as-handles.
2. `lsp_move` / `lsp_fix` preview parity with rename.
3. `lsp_grep` lineage honesty: `refs` versus `hits (unresolved)`.
4. Named pending buffers and graph pins.
5. `lsp_path` calls-only bridge search and path rendering.
6. Snapshot stamps and mutation journal.
7. `lsp_what_if` overlays.
8. `lsp_witness` and diagnostic baselines.
9. `lsp_root_cause`, `lsp_impact`, and `lsp_test_targets`.

This order stabilizes the visible interface first, then makes staged state
safe, then adds prediction and verification tools on top.
