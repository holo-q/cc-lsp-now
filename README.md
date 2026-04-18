# LSP Now — Full LSP Protocol for Claude Code

A generic **LSP-to-MCP bridge** that exposes the complete Language Server Protocol as MCP tools for Claude Code.

Claude Code's built-in LSP support covers ~9 methods. This plugin adds the rest — diagnostics, code actions, rename, completions, formatting, call hierarchy, and more — for **any** language server.

## Tools

| Tool | LSP Method | What it does |
|------|-----------|--------------|
| `lsp_diagnostics` | `textDocument/diagnostic` | Errors, warnings, hints |
| `lsp_hover` | `textDocument/hover` | Type info & docs at a position |
| `lsp_definition` | `textDocument/definition` | Jump to definition |
| `lsp_references` | `textDocument/references` | Find all references |
| `lsp_type_definition` | `textDocument/typeDefinition` | Jump to type declaration |
| `lsp_completion` | `textDocument/completion` | Completions at a position |
| `lsp_signature_help` | `textDocument/signatureHelp` | Function parameter info |
| `lsp_document_symbols` | `textDocument/documentSymbol` | File outline / symbol tree |
| `lsp_formatting` | `textDocument/formatting` | Auto-format document |
| `lsp_rename` | `textDocument/rename` | Rename across workspace |
| `lsp_prepare_rename` | `textDocument/prepareRename` | Check if rename is valid |
| `lsp_code_actions` | `textDocument/codeAction` | Quick fixes & refactorings |
| `lsp_call_hierarchy_incoming` | `callHierarchy/incomingCalls` | Who calls this? |
| `lsp_call_hierarchy_outgoing` | `callHierarchy/outgoingCalls` | What does this call? |
| `lsp_workspace_symbols` | `workspace/symbol` | Search symbols across workspace |

## Configuration

Set via environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `LSP_COMMAND` | Yes | LSP server binary (e.g., `ty`, `rust-analyzer`, `gopls`) |
| `LSP_ARGS` | No | Space-separated arguments (e.g., `server`) |
| `LSP_ROOT` | No | Workspace root path (defaults to cwd) |

## Install as Claude Code Plugin

```bash
claude plugin add github:nuck/cc-lsp-now
```

Then configure `LSP_COMMAND` for your language server.

## Standalone Usage

```bash
pip install cc-lsp-now  # or: uvx cc-lsp-now

# Run with ty (Python type checker)
LSP_COMMAND=ty LSP_ARGS=server cc-lsp-now

# Run with rust-analyzer
LSP_COMMAND=rust-analyzer cc-lsp-now

# Run with gopls
LSP_COMMAND=gopls LSP_ARGS=serve cc-lsp-now
```

## Architecture

```
Claude Code ←→ MCP (stdio) ←→ cc-lsp-now ←→ LSP (stdio) ←→ Language Server
```

The bridge spawns the configured language server as a subprocess, speaks JSON-RPC/LSP to it, and exposes each LSP operation as an MCP tool. Documents are synced from disk on each tool call.

## Context

Built to address [claude-code#40282](https://github.com/anthropics/claude-code/issues/40282) — Claude Code's built-in LSP tool is missing critical operations. This plugin fills the gap for any language server, not just the ones Anthropic ships support for.

## License

MIT
