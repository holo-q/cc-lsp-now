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

Plus `lsp_move_file`, `lsp_create_file`, `lsp_delete_file`, `lsp_implementation`, `lsp_declaration`, `lsp_type_hierarchy_supertypes`, `lsp_type_hierarchy_subtypes`, `lsp_inlay_hint`, `lsp_folding_range`, `lsp_range_formatting`, `lsp_code_lens`, `lsp_confirm` — full LSP surface. Tools are capability-gated at startup: if no server in the chain advertises the capability, the tool isn't registered, saving context tokens.

## Known LSP Plugins using cc-lsp-now

- **[cc-ty-plugin](https://github.com/holo-q/cc-ty-plugin)** — Python via [ty](https://github.com/astral-sh/ty) (Astral), with basedpyright fallback for call hierarchy and `willRenameFiles`.

**Want to add yours?** Open a PR adding a bullet here. An LSP plugin is ~20 lines of JSON — see [cc-ty-plugin/plugin.json](https://github.com/holo-q/cc-ty-plugin/blob/main/.claude-plugin/plugin.json) for the minimal shape (lspServers + mcpServers + the redirect hook). Tested language servers we'd like to see plugins for: `rust-analyzer`, `gopls`, `tsserver`, `clangd`, `lua-language-server`, `solargraph`, `elixir-ls`, `haskell-language-server`, `zls`, `nil`, `jdtls`.

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
        "LSP_SERVERS": "ty server;basedpyright-langserver --stdio"
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
| `LSP_SERVERS` | Yes | `;`-separated chain in priority order. Each entry is `<command> <args...>`. First = primary. Example: `ty server;basedpyright-langserver --stdio;pyright-langserver --stdio` |
| `LSP_ROOT` | No | Workspace root path (defaults to cwd) |
| `LSP_PREFER` | No | Per-method server override: `method1=command,method2=command`. Skips the cold-call probe and routes directly. Example: `workspace/willRenameFiles=basedpyright-langserver,textDocument/callHierarchy=basedpyright-langserver` |
| `LSP_REPLACE` | No | Post-filter command substitution: `old=new,old=new`. Applied to `LSP_SERVERS` entries and `LSP_PREFER` targets so a user can swap a binary without rewriting the whole config. Example: `basedpyright-langserver=pylance-language-server` replaces basedpyright everywhere the plugin mentions it. |
| `LSP_TOOLS` | No | Which tools to register. `all` = everything. Comma list = explicit opt-in. Default = all except formatting. |
| `LSP_DISABLED_TOOLS` | No | Comma-separated tools to exclude from the enabled set |

**Legacy format** (still accepted when `LSP_SERVERS` is unset): `LSP_COMMAND`/`LSP_ARGS` for primary, `LSP_FALLBACK_COMMAND`/`LSP_FALLBACK_ARGS` for first fallback, `LSP_FALLBACK_2_COMMAND`/`LSP_FALLBACK_2_ARGS` for subsequent fallbacks. Prefer `LSP_SERVERS` for new configs.

**Chain behavior**: per-method. On `-32601` the next server in the chain is tried; the first success is cached for that method. All subsequent calls skip to the cached server. `LSP_PREFER` lets you pre-seed that cache to avoid the first-call cost when you already know which server handles a method best.

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
