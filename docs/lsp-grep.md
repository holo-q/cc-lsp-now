# lsp_grep

`lsp_grep` is a semantic bucketizer for bare identifier names. It keeps the
fast, wide feel of `rg ctx`, then asks the language server what each occurrence
means so the model receives symbol groups instead of loose line hits.

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
