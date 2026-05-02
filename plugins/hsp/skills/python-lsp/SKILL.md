---
name: python-lsp
description: Use HSP's ty-backed Python profile for semantic navigation, diagnostics, references, rename, call hierarchy, formatting, and code actions.
---

# Python LSP

Use this skill when working on Python code and the task benefits from semantic code intelligence rather than plain text search.

Prefer the bundled `lsp_*` MCP tools for:

- diagnostics across a file or glob
- hover, definition, references, type definition, and workspace symbols
- rename and prepare-rename
- call hierarchy and code actions
- file move/create/delete operations that should notify the language server

The unified HSP plugin selects the Python profile for `.py` and `.pyi` files and workspaces with `pyproject.toml`, `setup.py`, or `setup.cfg` markers. HSP uses ty first, then basedpyright for operations ty does not implement yet. Use symbol names first, then add `line` only when a file contains ambiguous symbols. Use `lsp_diagnostics` after edits when the user needs verifier feedback.

