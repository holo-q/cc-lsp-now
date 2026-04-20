# LSP Now — Full LSP Protocol for Claude Code

A **standalone MCP server** that bridges the Language Server Protocol into Claude Code, exposing every LSP operation as a typed MCP tool. Claude Code's built-in `LSP()` tool covers ~9 methods and is often buggy — this bridge covers the full protocol for **any** language server, with symbol-name addressing, multi-target batching, and a fallback chain.

## Tools

| Tool | LSP Method |
|------|-----------|
| `lsp_diagnostics` | `textDocument/diagnostic` |
| `lsp_hover` | `textDocument/hover` |
| `lsp_definition` | `textDocument/definition` |
| `lsp_references` | `textDocument/references` |
| `lsp_type_definition` | `textDocument/typeDefinition` |
| `lsp_completion` | `textDocument/completion` |
| `lsp_signature_help` | `textDocument/signatureHelp` |
| `lsp_document_symbols` | `textDocument/documentSymbol` |
| `lsp_formatting` | `textDocument/formatting` |
| `lsp_rename` | `textDocument/rename` |
| `lsp_prepare_rename` | `textDocument/prepareRename` |
| `lsp_code_actions` | `textDocument/codeAction` |
| `lsp_call_hierarchy_incoming` | `callHierarchy/incomingCalls` |
| `lsp_call_hierarchy_outgoing` | `callHierarchy/outgoingCalls` |
| `lsp_workspace_symbols` | `workspace/symbol` |

## For LSP Plugin Authors

cc-lsp-now is the MCP server; your plugin bundles it. Users install one plugin (yours), get both the native `lspServers` integration (for hooks/diagnostics) *and* the full MCP tool set.

### 1. Declare the MCP server in `plugin.json`

```json
{
  "name": "ty-lsp",
  "version": "1.0.0",
  "lspServers": {
    "ty": { "command": "ty", "args": ["server"] }
  },
  "mcpServers": {
    "ty-lsp-extended": {
      "command": "uvx",
      "args": ["cc-lsp-now"],
      "env": {
        "LSP_COMMAND": "ty",
        "LSP_ARGS": "server",
        "LSP_FALLBACK_COMMAND": "basedpyright-langserver",
        "LSP_FALLBACK_ARGS": "--stdio"
      }
    }
  }
}
```

### 2. (Optional) Wire the redirect hook

Claude's built-in `LSP()` tool is incomplete and occasionally silent-fails (e.g. returning 0 results when the server supports the operation). Ship a `PreToolUse` hook that denies `LSP()` with a redirect message listing the MCP alternatives:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "LSP",
        "hooks": [
          {
            "type": "command",
            "command": "cc-lsp-now-redirect-hook"
          }
        ]
      }
    ]
  }
}
```

The `cc-lsp-now-redirect-hook` binary ships with cc-lsp-now. No custom script needed — every LSP plugin gets the same redirect behavior by copy-pasting this block.

### 3. Configuration via env vars

Set in the `env` block of your `mcpServers` entry:

| Variable | Required | Description |
|----------|----------|-------------|
| `LSP_COMMAND` | Yes | Primary LSP server binary (e.g., `ty`, `rust-analyzer`, `gopls`) |
| `LSP_ARGS` | No | Space-separated arguments (e.g., `server`) |
| `LSP_ROOT` | No | Workspace root path (defaults to cwd) |
| `LSP_FALLBACK_COMMAND` | No | Fallback LSP when primary returns `-32601` (method not supported) |
| `LSP_FALLBACK_ARGS` | No | Space-separated args for fallback |
| `LSP_TOOLS` | No | Which tools to register. `all` = everything. Comma list = explicit opt-in. Default = all except formatting. |
| `LSP_DISABLED_TOOLS` | No | Comma-separated tools to exclude from the enabled set |

The fallback chain is per-method: when the primary returns `-32601`, that method is cached as unsupported and all future calls go straight to the fallback. No repeated round-trip tax.

## How the model calls the tools

**Symbol names, not line/col.** The bridge resolves names via `documentSymbol` with a text-search fallback:

```
lsp_hover(file_path="src/app.py", symbol="OmfiApp")
lsp_definition(file_path="src/app.py", symbol="workflow", line=476)   # disambiguate
```

**Batching.** Multiple symbols in one file, multiple files in one call:

```
lsp_hover(file_path="src/app.py", symbols="Foo,Bar,Baz")
lsp_diagnostics(file_path="a.py,b.py,c.py")
lsp_diagnostics(pattern="src/**/*.py")
```

**Output format.** Line-number-anchored text, no JSON envelopes. Each response is prefixed with `[server method]` so the model sees which LSP handled the request:

```
[ty textDocument/hover]
<class 'OmfiApp'>
Standalone ComfyUI frontend built on AppKit.
```

## Standalone / CLI Usage

```bash
uv tool install cc-lsp-now     # or: pip install cc-lsp-now

LSP_COMMAND=ty LSP_ARGS=server cc-lsp-now
LSP_COMMAND=rust-analyzer cc-lsp-now
LSP_COMMAND=gopls LSP_ARGS=serve cc-lsp-now
```

The MCP server speaks stdio — useful for testing or for non-plugin MCP clients.

## Architecture

```
Claude Code
    ↕ MCP (stdio)
cc-lsp-now (mcp_server.py)
    ↕ JSON-RPC / LSP (stdio)
┌─── Primary LSP (ty, rust-analyzer, ...)
└─── Fallback LSP  (basedpyright, pyright, ...)  [lazy-spawned]
```

- Primary and fallback are both lazy-spawned — no processes started until first tool call.
- Method-level negative capability cache avoids repeated primary round-trips for operations the primary doesn't implement.
- Document sync reads from disk on each tool call (no in-memory tracking of user edits — the files on disk are the source of truth).

## Context

Built to address [claude-code#40282](https://github.com/anthropics/claude-code/issues/40282) — Claude Code's native LSP tool is missing operations and buggy for some that it does implement. This bridge will be progressively phased out as Claude Code's built-in implementation matures.

## License

MIT
