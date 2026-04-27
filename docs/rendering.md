# Agent Output Rendering

The public MCP surface is for agents, not editors. Output should be compact,
stable, navigable, and honest about provenance. The renderer contract is part of
the API: if an agent can see a row, it should usually be able to bounce from
that row into another tool without re-resolving the same target by hand.

## Canonical Row Shape

Default data rows should move toward:

```text
[N] L42 ::Scope:: kind name: Type - facts
```

Use the same grammar across semantic tools:

- `[N]` is a semantic graph handle usable by follow-up tools.
- `L42` is always an `L`-prefixed source line.
- `::Scope::` is a breadcrumb, omitted only when it adds no signal.
- Facts stay one-line and use plain labels: `refs 9`, `hits 4`, `def L12`,
  `samples L12,L18,+6`.

Diagnostics use their own namespace because code actions target diagnostics:

```text
(d0) L42 Error Cannot find type Foo [csharp/CS0246] fixes 2
```

The important rule is not the exact punctuation. The important rule is that a
consumer can parse every data row with one small grammar and can feed the handle
back into the tool surface.

## Graph Handles Everywhere

The graph memory should not be limited to `lsp_grep`, `lsp_symbols_at`,
`lsp_calls`, and `lsp_types`.

These tools should also seed follow-up context:

- `lsp_outline`: every symbol row should be addressable by `[N]`.
- `lsp_refs`: every reference row should be addressable by `[N]`.
- `lsp_goto`: every destination row should be addressable by `[N]`.
- `lsp_diagnostics`: every diagnostic row should be addressable by `(dN)`.

That turns the surface into a continuous traversal:

```text
lsp_outline -> lsp_symbol([3]) -> lsp_refs([0]) -> lsp_diagnostics -> lsp_fix((d0))
```

Rows that are printed but not navigable should be rare and intentional.

## Verified Refs Versus Text Hits

`lsp_grep` starts from text search, then asks the language server to bind each
candidate. The renderer must distinguish these cases:

```text
refs 9
hits 4 (unresolved)
```

`refs N` means the language server confirmed semantic references. `hits N
(unresolved)` means the tool found text candidates but could not bind them to a
semantic identity or reference set. This distinction matters during cold-index
and degraded-server states; unresolved text matches must not look like
verified semantic data.

Samples follow the same rule:

```text
samples L57,L694,+7
hit-samples L57,L694,+7
```

## Preview Shape

Mutation previews should look like one family, regardless of whether the source
is rename, move, or fix:

```text
Preview: 1 candidate, 3 file(s), 12 edit(s)
Verb: rename OldName -> NewName
Anchor: Renderer.cs:L78

[0] rename OldName -> NewName
    Renderer.cs (5 edit(s))
      L44  - public void Render(RenderContext OldName)
           + public void Render(RenderContext NewName)

Confirm: lsp_confirm(0)
```

Current gap: `lsp_rename` has the clearest before/after preview, while
`lsp_move` can still expose raw range/newText edits. `lsp_move` should reuse the
same edit preview helper as rename. `lsp_fix` should also show edit previews for
edit-backed actions, because action titles alone are not enough for an agent to
choose safely.

## Diagnostics And Fix Discovery

`lsp_fix` is the right public verb for code actions and repairs. The issue is
not the name; the issue is discoverability. Diagnostics should advertise repair
affordances inline so the model naturally reaches for `lsp_fix` at the moment a
diagnostic appears.

Target shape:

```text
(d0) L42 Error Missing using Foo [csharp/CS0246] fixes 2
     [f0] Add using Foo
     [f1] Fully qualify Foo.Bar
```

Then the natural flow is visible in the output itself:

```text
lsp_fix((d0))
lsp_confirm(0)
```

or eventually:

```text
lsp_confirm(f0)
```

The diagnostic flow should pull `lsp_fix` into view. The tool is fine; the
diagnostic renderer should make the next hop obvious.

## Empty And Truncated States

Empty states should use one family:

```text
No references for Foo.
No diagnostics for Renderer.cs.
No fixes for (d0).
```

Truncation should also use one family:

```text
... +8 more refs; raise max_refs to unfold.
... +6 more groups; raise max_groups to unfold.
... +50 more edges; raise max_edges to unfold.
```

This keeps downstream wrappers and agents from learning a dozen local phrasings
for "nothing here" and "there is more behind this limit."

## Renderer Refactor Plan

Introduce a small internal rendering layer instead of continuing ad-hoc
`lines.append(...)` assembly:

- `LocRef`: canonical file/line/column/snippet rendering.
- `Row`: `[N]` / `[-]` / unindexed row rendering.
- `DiagRow`: diagnostic row with optional `(dN)` handle.
- `Section`: title plus rows plus optional footer.
- `EditPreview`: before/after preview shared by rename, move, and fix.
- `PreviewHeader`: shared mutation preview header.
- `NavBucket`: typed semantic graph registration metadata.

Migration order:

1. Extract `LocRef` and `DiagRow`; make diagnostics use `Lxx`.
2. Make `outline`, `refs`, `goto`, and diagnostics seed graph handles.
3. Move calls/types/grep/symbols output to shared `Row` / `Section` helpers.
4. Move rename/move/fix previews to shared `PreviewHeader` / `EditPreview`.
5. Replace raw string `PendingBuffer.kind` values with `CandidateKind`.
6. Add renderer golden tests to catch drift.

Formatting is deliberately outside this scope. This document is about the
information contract, not pretty-printing source code.
