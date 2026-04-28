# LSP Now — Agent-First LSP for Claude Code

A **standalone MCP server** that bridges the Language Server Protocol into Claude Code, exposing LSP-backed operations as typed MCP tools. Claude Code's built-in `LSP()` tool covers ~9 methods and is often buggy — this bridge covers the protocol surface for **any** language server while evolving toward a smaller graph-operator interface for agents: find semantic nodes, inspect nodes, expand graph edges, stage mutations, and verify.

## Tools

The target public surface is documented in [docs/tool-surface.md](docs/tool-surface.md). Implemented graph operators include:

| Tool | Purpose |
|------|---------|
| `lsp_grep` | Text search plus semantic binding; groups identifier hits by symbol identity. |
| `lsp_symbols_at` | Expands all semantic symbols on a line, including function args, with last-graph navigation. |
| `lsp_symbol` | Inspects one semantic node from a graph index, `file:Lx`, or `file_path` plus `symbol`/`line`. |
| `lsp_goto` | Resolves definition/declaration/type/implementation destinations through one command. |
| `lsp_refs` | Expands references for a semantic node or graph index. |
| `lsp_outline` | Shows compact file/workspace breadcrumbs from document symbols. |
| `lsp_calls` | Walks incoming/outgoing call edges from a graph node via `direction=in\|out\|both`. |
| `lsp_types` | Walks super/sub type hierarchy edges from a graph node via `direction=super\|sub\|both`. |
| `lsp_session` | Inspects, adds, warms, and restarts workspaces/LSP sessions via `action=status\|add\|warm\|restart`. |
| `lsp_diagnostics` | Reports diagnostics as the main verifier surface. |
| `lsp_fix` | Lists code actions on a semantic target with line diagnostics; stages edit-backed actions for `lsp_confirm`. |
| `lsp_rename` | Previews and stages semantic renames before `lsp_confirm`. |
| `lsp_move` | Previews file moves (single or batched) and import/update edits before `lsp_confirm`. |
| `lsp_confirm` | Commits the currently staged edit transaction. |
| `lsp_log` | Appends agent-bus events, notes, timed questions, replies, and workspace weather through the broker. |

The remaining protocol-shaped tools are transitional. The cut direction is one-way: as each workflow tool lands (`lsp_outline`, `lsp_calls`, `lsp_fix`, `lsp_session`, `lsp_move`), the corresponding raw LSP command wrapper is removed from the public registry — no aliases. Formatting is deliberately not exposed to agents; use editor/save hooks, pre-commit hooks, CI, or a direct formatter run instead. See [docs/tool-surface.md](docs/tool-surface.md) for the full raw → workflow cut map.

File arguments may be full paths, relative paths, or unique basenames. For example, `lsp_outline(file_path="NodesWindow.cs")` resolves the file under active workspaces; if the basename is not unique, the tool returns the matching paths and asks for a more specific path.

## Known LSP Plugins using cc-lsp-now

- **[cc-ty-plugin](https://github.com/holo-q/cc-ty-plugin)** — Python via [ty](https://github.com/astral-sh/ty) (Astral), with basedpyright fallback for call hierarchy and `willRenameFiles`.

**Want to add yours?** Open a PR adding a bullet here. An LSP plugin is ~20 lines of JSON — see [cc-ty-plugin/plugin.json](https://github.com/holo-q/cc-ty-plugin/blob/main/.claude-plugin/plugin.json) for the minimal shape (lspServers + mcpServers + the redirect hook). Tested language servers we'd like to see plugins for: `rust-analyzer`, `gopls`, `tsserver`, `clangd`, `lua-language-server`, `solargraph`, `elixir-ls`, `haskell-language-server`, `zls`, `nil`, `jdtls`.

## How the model calls the tools

**Semantic targets, not raw protocol calls.** Tools accept graph indices, bare `Lxx`, `file:Lx`, unique basenames, or `file_path` plus `symbol`/`line`:

```
lsp_symbol(file_path="src/app.py", symbol="OmfiApp")
lsp_goto(file_path="src/app.py", symbol="workflow", line=476, mode="all")
lsp_refs(target="[0]")           # graph index from the previous lsp_grep/lsp_symbols_at
lsp_symbols_at("L78")            # bare Lxx — resolves against the last printed graph
lsp_symbols_at("HistoryUI.cs:L78")  # basename + line, no full path required
```

Sample lists shown by `lsp_grep` (`samples L57,L694,...`) are non-exhaustive — a trailing `...` means more refs exist; unfold with `lsp_refs([N])` or raise `max_hits`. The full count is always reported as `refs N`.

**Batching.** Multiple symbols in one file, multiple files in one call:

```
lsp_diagnostics(file_path="a.py,b.py,c.py")
lsp_diagnostics(pattern="src/**/*.py")
```

**Output format.** Line-number-anchored text, no JSON envelopes. Each response is prefixed with `[server method]` so the model sees which LSP handled the request:

```
[ty textDocument/hover]
<class 'OmfiApp'>
Standalone ComfyUI frontend built on AppKit.
```

## For LSP Plugin Authors

cc-lsp-now is the MCP server; your plugin bundles it. Users install one plugin (yours), get both the native `lspServers` integration (for hooks/diagnostics) *and* the graph-oriented MCP tool set.

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
| `LSP_TOOLS` | No | Which tools to register. `all` = every public tool. Comma list = explicit opt-in. Default = all public tools. |
| `LSP_EXCLUDE` | No | Comma-separated tools to exclude from the enabled set. (Legacy name: `LSP_DISABLED_TOOLS` — still accepted.) |
| `CC_LSP_BROKER` | No | Broker mode: `auto` (default) shares one warm LSP chain across agents and falls back to direct mode if the broker is unreachable; `on` requires the broker; `off` restores one LSP chain per MCP process. |
| `CC_LSP_BROKER_SOCKET` | No | Override the user-scoped Unix socket. Useful for isolated tests or separate broker pools. |
| `CC_LSP_BROKER_LOG` | No | Override the broker log path. Default: `$XDG_STATE_HOME/cc-lsp-now/broker.log` or `~/.local/state/cc-lsp-now/broker.log`. |
| `CC_LSP_BROKER_IDLE_TTL_SECONDS` | No | Idle broker session TTL. Default 14400 seconds. Set `0` to disable automatic idle eviction. |
| `CC_LSP_DEVTOOLS` | No | Set `1`/`true`/`on` to expose the live broker to `python-devtools` for runtime introspection. Registers `broker`, `bus`, `registry`, and `lsp` under app id `cc-lsp-now-broker` by default. |
| `CC_LSP_DEVTOOLS_APP_ID` | No | Override the devtools app id. Default: `cc-lsp-now-broker`. |
| `CC_LSP_DEVTOOLS_READONLY` | No | Devtools readonly mode. Default: enabled, so agents can inspect broker state without mutation tools. |
| `CC_LSP_PROBE_CAPABILITIES` | No | Opt into startup capability probing (`1`/`true`/`on`). Default off so MCP startup never launches heavy language servers before the initialize handshake. Runtime fallback still handles unsupported methods. |
| `LSP_PROJECT_MARKERS` | No | Comma-separated filenames that mark a project root (e.g. `pyproject.toml,setup.py,.git`). When a file outside the current workspace folders is accessed, the bridge walks up looking for these markers and adds the found root to the LSP's workspace via `workspace/didChangeWorkspaceFolders`. Plugins contribute their language's markers — Python plugins list `pyproject.toml`, Rust plugins list `Cargo.toml`, etc. Default: `.git`. |
| `LSP_WARMUP_PATTERNS` | No | Comma-separated glob patterns (e.g. `*.py,*.pyi` for Python, `*.rs` for Rust). When a workspace folder is added (initial spawn or via auto-detection), the bridge bulk-emits `textDocument/didOpen` for matching files so the LSP eagerly indexes them. Prevents the "cold index" failure mode where `willRenameFiles` returns 0 edits because nothing has been indexed yet. No warmup if unset. |
| `LSP_WARMUP_MAX_FILES` | No | Cap on how many files to warm per workspace folder. Default 500. |

**Legacy format** (still accepted when `LSP_SERVERS` is unset): `LSP_COMMAND`/`LSP_ARGS` for primary, `LSP_FALLBACK_COMMAND`/`LSP_FALLBACK_ARGS` for first fallback, `LSP_FALLBACK_2_COMMAND`/`LSP_FALLBACK_2_ARGS` for subsequent fallbacks. Prefer `LSP_SERVERS` for new configs.

**Chain behavior**: per-method. On `-32601` the next server in the chain is tried; the first success is cached for that method. All subsequent calls skip to the cached server. `LSP_PREFER` lets you pre-seed that cache to avoid the first-call cost when you already know which server handles a method best.

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
    ↕ JSONL / Unix socket  [broker mode, default]
cc-lsp-now-broker
    ↕ JSON-RPC / LSP (stdio)
┌─── Primary LSP (ty, rust-analyzer, ...)
└─── Fallback LSP  (basedpyright, pyright, ...)  [lazy-spawned]
```

- Broker mode is default when an LSP chain is configured. Multiple agents reuse
  the same broker-owned LSP chain for the same root/config hash, reducing CPU
  and keeping method routing, diagnostics, and future alias memory aligned.
- The broker owns the first agent-bus slice: `lsp_log` appends durable
  workspace events to `tmp/cc-lsp-now-bus.jsonl`, opens timed questions,
  records replies, settles closed windows, and renders compact weather. The bus
  is advisory only: no claims, leases, or edit denial. See
  [docs/agent-bus.md](docs/agent-bus.md).
- With `CC_LSP_DEVTOOLS=1`, the broker starts `python-devtools` and registers
  live `broker`, `bus`, `registry`, and `lsp` objects so agents can attach via
  the `python-devtools` MCP bridge and inspect daemon state directly.
- Primary and fallback are both lazy-spawned — no LSP processes start until the
  first semantic tool call that needs them.
- Method-level negative capability cache avoids repeated primary round-trips for operations the primary doesn't implement.
- Document sync reads from disk on each tool call (no in-memory tracking of user edits — the files on disk are the source of truth).
- `lsp_session(action="status")` reports broker PID, socket, log path, live
  sessions, client PIDs, open documents, cached method routes, and request
  counts. `action="restart"` stops the matching broker session so the next
  request respawns it; `action="stop"` stops without respawn.

## Context

Built to address [claude-code#40282](https://github.com/anthropics/claude-code/issues/40282) — Claude Code's native LSP tool is missing operations and buggy for some that it does implement. This bridge will be progressively phased out as Claude Code's built-in implementation matures.

## License

MIT
