---
name: rust-lsp
description: Use HSP's rust-analyzer route for Rust semantic navigation, diagnostics, references, rename, call hierarchy, type hierarchy, and code actions.
---

# Rust LSP

Use this skill when working on Rust code and the task benefits from semantic code intelligence rather than plain text search.

Prefer the bundled `lsp_*` MCP tools for:

- diagnostics across a file or glob
- hover, definition, references, type definition, and workspace symbols
- rename and prepare-rename
- call hierarchy, type hierarchy, and code actions
- file move/create/delete operations that should notify the language server

The unified HSP plugin routes `.rs` files and workspaces with `Cargo.toml` or `rust-project.json` markers to rust-analyzer. Use symbol names first, then add `line` only when a file contains ambiguous symbols. Use `lsp_diagnostics` after edits when the user needs verifier feedback.
