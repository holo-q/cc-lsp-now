---
name: csharp-lsp
description: Use HSP's csharp-ls route for C# semantic navigation, diagnostics, references, rename, call hierarchy, formatting, and code actions.
---

# C# LSP

Use this skill when working on C# code and the task benefits from semantic code intelligence rather than plain text search.

Prefer the bundled `lsp_*` MCP tools for:

- diagnostics across a file or glob
- hover, definition, references, type definition, and workspace symbols
- rename and prepare-rename
- call hierarchy and code actions
- file move/create/delete operations that should notify the language server

The unified HSP plugin routes `.cs` files and workspaces with `*.sln`, `*.csproj`, `Directory.Build.props`, or `global.json` markers to csharp-ls. Use symbol names first, then add `line` only when a file contains ambiguous symbols. Use `lsp_diagnostics` after edits when the user needs verifier feedback.
