# Render Memory

Render memory is the context-aware compression layer for agent-facing LSP
output. It lets the tool surface speak in stable short aliases after a symbol
has already been shown, while preserving the full semantic identity behind
every compressed token.

The motivating shape is:

```text
[P0] A3 -> A7 -> J1
```

That output is only acceptable when it is reversible:

```text
legend gen=12:
  A=ComfyNodeRenderer.cs::ComfyNodeRenderer  A3=Render@L44  A7=Update@L88
  J=NodeImageStore.cs::NodeImageStore        J1=Get@L21
```

The goal is not decoration. The goal is to turn repeated semantic structure
into a working-memory protocol: the first response teaches the agent what a
symbol is; later responses can use the short form and spend tokens on new
information.

## Current State

The existing `[N]` handles are graph handles for the most recent semantic
result. They are useful and must remain, but they are not durable aliases.

Current behavior:

- `_record_semantic_nav_context(...)` replaces `_last_semantic_groups` and
  `_last_semantic_nav`.
- `[3]` means row/group 3 in the current last semantic graph.
- A later `lsp_grep`, `lsp_symbols_at`, `lsp_calls`, `lsp_types`, or
  `lsp_path` can replace that graph.

Render memory lives beside this mechanism. It does not change the meaning of
`[N]`. It adds persistent aliases that survive across tool calls within an
epoch.

## Core Contract

Every alias must satisfy five rules:

1. **Issued by the server.** The resolver never guesses aliases from text.
2. **Reversible.** A current legend or recall tool can map the alias back to a
   canonical semantic identity.
3. **Stable within an epoch.** Once `A3` is minted, it must not silently rebind
   to a different symbol until the epoch ends.
4. **Refusable when stale.** If an alias is invalidated by edits, server
   restart, workspace change, or snapshot drift, resolution returns a readable
   stale-alias error.
5. **Layered over truth.** Compression changes display only. It must not hide
   edge family, direction, unresolved text matches, diagnostics, or mutation
   previews.

Unknown aliases hard-fail. There is no fuzzy matching and no "did you mean"
path for aliases.

## Alias Grammar

There are two handle families:

```text
[N]     last-result graph handle, existing behavior
(dN)    diagnostic handle, existing behavior
[P0]    path handle, existing pathfinding behavior
A3      render-memory symbol alias, dense form
[A3]    render-memory symbol alias, bracketed form
[F1]    file alias
[T1]    type alias
```

The dense symbol alias is bucket/member:

```text
A = container bucket
A3 = member 3 in container A
A3:ctx = local or argument ctx inside member A3
```

Bucket identity should be the nearest stable semantic container:

- class or interface for methods and fields;
- module/file for free functions;
- containing method for locals and parameters;
- type identity for type aliases.

Member identity is the semantic node inside that bucket. The identity key should
include at least:

```text
workspace root
server label
symbol kind
symbol name
definition path
definition line
definition character
```

Future snapshot support should add file content hash or mtime. Broker support
should add session/client ownership.

### Why Bucket/Member

Flat aliases such as `S17` are safe but lose topology. Bucket/member aliases
preserve structure:

```text
A3 -> A7
```

means an intra-container hop, while:

```text
A3 -> J1
```

means a cross-container hop. That gives the model useful topology without a
long breadcrumb.

## Legend Grammar

Legend lines are the decode table.

Full legend:

```text
legend gen=12:
  A=ComfyNodeRenderer.cs::ComfyNodeRenderer  A3=Render@L44  A7=Update@L88
  J=NodeImageStore.cs::NodeImageStore        J1=Get@L21
```

Delta legend:

```text
legend+ gen=12:
  J=NodeImageStore.cs::NodeImageStore  J1=Get@L21
```

Rules:

- `legend` means a full applicable legend for aliases used in this response.
- `legend+` means additive aliases only; previous bindings remain unchanged.
- A response that introduces aliases must print a legend.
- A response that uses stale or cold aliases should reprint a delta legend or
  inline hint.
- Empty results emit no legend.

The legend is not a separate source of truth. It is a view of the alias book.

## Epochs

An epoch is the lifetime during which aliases may be reused.

An epoch should end on:

- LSP session restart;
- workspace root change;
- confirmed mutation that touches aliased files;
- explicit alias reset;
- future snapshot stamp mismatch;
- broker/client session boundary when broker mode exists.

Within an epoch, aliases are monotonic. Do not recycle `A3` after eviction. A
forgotten alias may become unavailable, but it must not point to a new symbol.

The direct MCP server can start with a process-local epoch. Broker mode should
move epochs to per-client or shared workspace sessions.

## Compression Levels

Compression is progressive. It should be chosen per row, not per whole result.

### L0: Verbose

Cold output. This is today's canonical row.

```text
[3] L44 ::ComfyNodeRenderer:: method Render: void - refs 9 - def L44 - samples L57,L694,+7
```

### L1: Chipped

First alias introduction. Full row plus alias chip.

```text
[3] A3 L44 ::ComfyNodeRenderer:: method Render: void - refs 9 - samples L57,L694,+7
legend gen=12:
  A=ComfyNodeRenderer.cs::ComfyNodeRenderer  A3=Render@L44
```

### L2: Alias First

Warm symbol in normal rows.

```text
[3] A3 Render: void - refs 9 - samples L57,L694,+7
```

### L3: Dense

Compound views such as paths, call chains, impact summaries, and before/after
semantic deltas.

```text
[P0] cost 3 hops 3 verified  A3 -> A7 -> J1
```

Single semantic rows should usually stop at L2. Dense L3 is for relational
output where the row shape itself tells the story.

## Promotion Policy

Aliases should not be coined for every short-lived value. The renderer should
promote based on heat:

```text
heat = recency + repetition + sibling density
```

Recommended policy:

- First sighting: render L0 unless the result already contains multiple symbols
  in the same bucket; then render L1.
- Second sighting in the epoch: render L1 or L2.
- Repeated sighting: render L2.
- Path/call/impact compounds: render L3 when every alias used is either warm or
  introduced by the same response's legend.

If a legend would be too large, prefer less compression. Verbosity is better
than an unreadable alias wall.

## Resolver Rules

Aliases become target strings only after resolver support is implemented.

Target resolution order should be explicit:

1. Bracketed semantic graph indices: `[3]` and `3`.
2. Render-memory aliases: `A3`, `[A3]`, `A3:ctx`.
3. Line targets: `L42`, `file:L42`.
4. `file_path` / `symbol` / `line` fallbacks.

Parser rules:

- Numeric-only bracket values remain graph indices.
- Letter-prefixed aliases route through the alias book.
- Bare symbol names such as `A` remain symbol names unless an alias token shape
  matches exactly.
- Unicode homoglyphs are rejected.
- Alias lookup never calls LSP for unknown aliases.

Unknown alias response:

```text
Alias A7 is not active in render memory gen=12. Run lsp_legend or re-anchor with lsp_grep.
```

Stale alias response:

```text
Alias A7 is stale: ComfyNodeRenderer.cs changed since gen=12. Re-run the source query.
```

## Tool-Specific Rules

### lsp_path

`lsp_path` is the first high-value target for dense rendering.

Before:

```text
[P0] cost 3 hops 3 verified
  [0] L44 ::ComfyNodeRenderer:: method Render
   --calls--> [1] L88 ::ComfyNodeRenderer:: method Update
   --calls--> [2] L21 ::NodeImageStore:: method Get
```

After:

```text
[P0] cost 3 hops 3 verified  A3 -> A7 -> J1
legend gen=12:
  A=ComfyNodeRenderer.cs::ComfyNodeRenderer  A3=Render@L44  A7=Update@L88
  J=NodeImageStore.cs::NodeImageStore        J1=Get@L21
```

For mixed-edge paths in the future, keep per-hop labels:

```text
[P0] A3 -calls-> A7 -refs-> J1
```

### lsp_calls

Calls can compress edge rows once endpoints are warm:

```text
Calls for A3
in:
  [0] B1 <- 1 site
out:
  [1] A7 -> 2 sites
  [2] J1 -> 1 site
```

The section header declares the edge family, so arrows can stay compact. If a
row combines multiple edge families, labels must be explicit.

### lsp_grep and lsp_symbols_at

These are teaching tools as much as search tools. They should introduce aliases
slowly:

```text
[0] A3 L44 ::ComfyNodeRenderer:: method Render: void - refs 9 - samples L57,L694,+7
```

Use L0/L1 for cold results. Use L2 only for symbols already known in the epoch.

### lsp_symbol

`lsp_symbol(A3)` should still show enough detail to verify the alias:

```text
A3 = ComfyNodeRenderer.cs::ComfyNodeRenderer.Render@L44
kind method
refs 9
```

### lsp_refs

References should compress files and repeated target symbols, but keep line
numbers visible:

```text
refs A3
[0] [F1]:57  A3(...)
[1] [F1]:694 A3(...)
[2] [F2]:21  A3(...)
legend+ gen=12:
  F1=src/Oomfi.Platform/Render/ComfyNodeRenderer.cs
  F2=src/Oomfi.Platform/Net/Surfaces/NodeImageStore.cs
```

### Diagnostics

Diagnostics keep `(dN)`. A diagnostic may mention an alias for the affected
symbol, but the diagnostic handle remains the repair target.

```text
(d0) L42 Error Cannot find type J [csharp/CS0246] fixes 2
```

### Mutation Previews

Do not compress the actual edit hunks. Rename, move, and fix previews must show
literal before/after text. Aliases may appear in the preview header only:

```text
Verb: rename A3 Render -> RenderArtifact
```

The edit body stays literal.

## Guardrails

Render memory must defend against misleading compression.

- Never replace `[N]`; aliases are additive.
- Never reuse an alias for a different identity in the same epoch.
- Never use aliases in source edit hunks.
- Never hide unresolved text evidence behind semantic alias syntax.
- Never route unknown aliases through fuzzy symbol search.
- Always distinguish "not active", "stale", and "ambiguous".
- Skip or bracket dense aliases when a symbol with the same literal name exists.
- Keep legends short; unfold rows instead of emitting a huge legend.

## Direct Mode Architecture

The first implementation should be process-local and pure where possible.

New module:

```text
src/cc_lsp_now/render_memory.py
```

Core types:

```text
AliasIdentity
AliasRecord
RenderMemory
```

Minimum API:

```text
touch(identity) -> AliasRecord
lookup(alias) -> AliasRecord | None
aliases_for_response(records) -> legend text
clear_epoch(reason)
```

Server integration:

1. `_record_semantic_nav_context(...)` touches render memory for every
   `SemanticGrepGroup`.
2. `_resolve_semantic_target(...)` resolves minted aliases before line/path
   fallback.
3. `lsp_legend` or `lsp_session(action="legend")` prints the active legend.
4. Renderers begin consuming aliases, starting with `lsp_path`.

This should not replace `_last_semantic_groups` or `_last_semantic_nav` in the
first slice. Those stay as the last-result graph. Render memory is a sidecar.

## Broker Mode

Broker mode should move render memory from process globals to session state:

```text
WorkspaceSession
  master_alias_book          # stable identity -> canonical alias
  client_frontiers           # client id -> aliases already introduced to that agent
  client_render_windows      # client id -> local compression/recentness state
```

Policy:

- the broker allocates canonical aliases so agents converge on one legend;
- each client tracks an "introduced aliases" frontier, so output never compresses
  to an alias that has not been shown in that agent's own context;
- renderers may fall back to a fuller row for one client while emitting dense
  aliases for another client that has already seen the legend;
- named/pinned aliases can be promoted to shared workspace memory, but the
  introduction frontier still gates compression per client;
- pending edit aliases remain per-client unless explicitly shared;
- aliases carry snapshot stamps so stale resolution is deterministic.

The direct-mode `RenderMemory` API should be designed so the broker can own it
later without changing the output grammar.

## QA

Pure tests:

- deterministic alias assignment;
- same identity gets same alias;
- different path/line/kind gets different alias;
- aliases are never reused after eviction;
- legend rows decode every alias used in output;
- unknown aliases refuse without LSP calls;
- stale aliases refuse with cause.

Renderer tests:

- first sighting stays verbose or chipped;
- second sighting compresses;
- `lsp_path` dense output remains reversible;
- `[N]` graph handles remain valid;
- diagnostics keep `(dN)`;
- mutation previews keep literal edit hunks.

Safety tests:

- generic type text does not parse as aliases;
- class literally named `A3` does not silently shadow alias `A3`;
- unicode lookalikes are rejected;
- alias table resets on session restart and confirmed mutation;
- cross-query `[N]` behavior remains last-result scoped.

## Implementation Order

1. Land this document and cross-link it from `docs/rendering.md`.
2. Add `render_memory.py` and pure tests. No output changes.
3. Touch render memory from `_record_semantic_nav_context`.
4. Resolve aliases in `_resolve_semantic_target`.
5. Add `lsp_legend` or `lsp_session(action="legend")`.
6. Compress `lsp_path` dense rows.
7. Roll compression into `lsp_calls`, `lsp_types`, `lsp_grep`, and
   `lsp_symbols_at`.

The first code slice should be resolver-only or `lsp_path`-only. Broad renderer
compression should wait until the alias book and legend contract are proven.
