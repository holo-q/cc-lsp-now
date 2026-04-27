# lsp_grep

`lsp_grep` is a semantic bucketizer for bare identifier names. It keeps the
fast, wide feel of `rg ctx`, then asks the language server what each occurrence
means so the model receives symbol groups instead of loose line hits.

Together with `lsp_symbols_at`, it is the first shipped piece of the
graph-operator surface described in `docs/tool-surface.md`. The long-term
interface is not a one-tool-per-LSP-method mirror; it is a small set of
operators for finding semantic nodes, inspecting them, expanding edges, staging
mutations, and verifying the result.

Default output is one line per semantic identity:

```text
[0] arg ctx: RenderContext — ComfyNodeRenderer:44::Render::ctx — refs 9 — def L44 — samples L57,L694,...
[1] field _ctx: HistorySurfaceContext — HistoryUI:64::_ctx — refs 14 — def L64 — samples L78,L159,L218,...
```

Breadcrumbs use `::` instead of `>` so C# and TypeScript generics stay legible.
When a class name matches its file name, the path is abridged:

```text
ComfyNodeRenderer.cs + class ComfyNodeRenderer -> ComfyNodeRenderer
ComfyNodeRenderer:44::Render::ctx
```

When the file and type disagree, the breadcrumb unfolds just enough:

```text
NodeRenderer.cs::ComfyNodeRenderer:44::Render::ctx
```

The first implementation is intentionally disk-backed and exact:

- `query` must be one identifier.
- text candidates are found under `file_path`, `pattern`, or active workspace
  roots using `LSP_WARMUP_PATTERNS` when available.
- each candidate is resolved with `textDocument/definition`, falling back to
  `textDocument/declaration` and then the occurrence itself.
- groups are counted with `textDocument/references`.
- output stays compact; callers can raise `max_hits` or `max_groups` when a name
  needs more unfolding.

This is the local version of the semantic-grep direction recorded in
`docs/broker.md`. A broker can later make the same operation faster by reusing
warm sessions and indexes across agents.

## Bouncing From Samples

`lsp_grep` records the reference graph it just showed. `lsp_symbols_at` can use
that graph as context, so a bare line target works when it was present in the
last refs/samples:

```text
lsp_symbols_at("L78")
```

Explicit targets do not need context:

```text
lsp_symbols_at("HistoryUI.cs:L78")
lsp_symbols_at("/repo/src/HistoryUI.cs:L78")
```

The output is the same one-line semantic-bucket shape, but for every identifier
on that source line. On a function declaration this intentionally includes the
function name and all arguments, so the model can hop from a sample line into the
local symbol graph without first doing a separate text search. This graph memory
is the pattern later tools such as `lsp_symbol`, `lsp_goto`, and `lsp_refs`
should reuse.
