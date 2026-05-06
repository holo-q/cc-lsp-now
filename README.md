# HSP — Harness Server Protocol for Agent-First LSP

A **standalone MCP server** that bridges the Language Server Protocol into Claude Code, exposing LSP-backed operations as typed MCP tools. HSP is the harness layer around language servers: it keeps LSP protocol details inside the server while exposing graph-oriented operators for agents. Claude Code's built-in `LSP()` tool covers ~9 methods and is often buggy — this bridge covers the protocol surface for **any** language server while evolving toward a smaller graph-operator interface: find semantic nodes, inspect nodes, expand graph edges, stage mutations, and verify.

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

## Plugin Install Shape

HSP now ships as one plugin with a broker-owned router for Python, C#, and Rust language routes. Builtin routing is the default unless an explicit legacy `LSP_SERVERS`/`LSP_COMMAND` chain is configured; each request forwards the target URI/root to the broker, and the broker chooses a route from the file extension or workspace markers. That keeps each route's LSP chain, method cache, warmup state, and broker session separate while letting a broker restart pick up route-table changes.

Built-in routes:

| Route | LSP chain | Selection signals |
|---------|-----------|-------------------|
| Python | `ty server;basedpyright-langserver --stdio` | `.py`, `.pyi`, `pyproject.toml`, `setup.py`, `setup.cfg` |
| C# | `csharp-ls` | `.cs`, `*.sln`, `*.csproj`, `Directory.Build.props`, `global.json` |
| Rust | `rust-analyzer` | `.rs`, `Cargo.toml`, `rust-project.json` |

Set `HSP_ROUTE=python`, `HSP_ROUTE=csharp`, or `HSP_ROUTE=rust` to force a route for workspace-level operations where no file URI is available. Explicit `LSP_SERVERS` or legacy `LSP_COMMAND` still wins and keeps the old single-chain mode, so the split plugin repos continue to work while users migrate to the unified `hsp` plugin.

## Legacy Split Plugins using hsp

- **[hsp-cs](https://github.com/holo-q/hsp-cs)** — C# via [csharp-ls](https://github.com/razzmatazz/csharp-language-server).
- **[hsp-py](https://github.com/holo-q/hsp-py)** — Python via [ty](https://github.com/astral-sh/ty) (Astral), with basedpyright fallback for call hierarchy and `willRenameFiles`.

**Want to add a new language?** Add a builtin route in `hsp.router` plus the plugin manifest's native `lspServers` entry. The old "one repo per LSP" shape still works, but the preferred interface is a single HSP plugin with routing inside the runtime.

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

`hsp mcp` is the MCP server; your plugin bundles it. Users install one plugin (yours), get both the native `lspServers` integration (for hooks/diagnostics) *and* the graph-oriented MCP tool set.

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
      "args": ["hsp", "mcp"],
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
            "command": "hsp-redirect-hook"
          }
        ]
      }
    ]
  }
}
```

The published HSP Claude plugin already bundles these hooks. Plugin authors
copy the block only when building a new downstream plugin; users should not
hand-install hook config. Ambient bus hooks are enabled by default when the
plugin is installed; set `HSP_HOOKS=0` only when you need the hook commands to
drain their JSON payload and exit without launching `uvx`.

### 3. Configuration via env vars

Set in the `env` block of your `mcpServers` entry:

| Variable | Required | Description |
|----------|----------|-------------|
| `HSP_ROUTER` | No | Builtin routing is the default when no explicit `LSP_SERVERS`/`LSP_COMMAND` is set. Set `0`/`false`/`off` only to force legacy single-chain mode. |
| `HSP_ROUTE` | No | Force one builtin route (`python`, `csharp`, or `rust`) when router mode is enabled and a request has no target file URI. |
| `HSP_HOOKS` | No | Controls bundled ambient bus hooks. Hooks are on by default; set `0`/`false`/`off` to make them drain stdin and exit before launching `uvx`. |
| `LSP_SERVERS` | Required only for custom/legacy plugin configs | `;`-separated chain in priority order. Each entry is `<command> <args...>`. First = primary. Example: `ty server;basedpyright-langserver --stdio;pyright-langserver --stdio` |
| `LSP_ROOT` | No | Workspace root path (defaults to cwd) |
| `LSP_PREFER` | No | Per-method server override: `method1=command,method2=command`. Skips the cold-call probe and routes directly. Example: `workspace/willRenameFiles=basedpyright-langserver,textDocument/callHierarchy=basedpyright-langserver` |
| `LSP_REPLACE` | No | Post-filter command substitution: `old=new,old=new`. Applied to `LSP_SERVERS` entries and `LSP_PREFER` targets so a user can swap a binary without rewriting the whole config. Example: `basedpyright-langserver=pylance-language-server` replaces basedpyright everywhere the plugin mentions it. |
| `LSP_TOOLS` | No | Which tools to register. `all` = every public tool. Comma list = explicit opt-in. Default = all public tools. |
| `LSP_EXCLUDE` | No | Comma-separated tools to exclude from the enabled set. (Legacy name: `LSP_DISABLED_TOOLS` — still accepted.) |
| `HSP_BROKER` | No | Broker mode: `auto` (default) shares one warm LSP chain across agents and falls back to direct mode if the broker is unreachable; `on` requires the broker; `off` restores one LSP chain per MCP process. |
| `HSP_BROKER_SOCKET` | No | Override the user-scoped Unix socket. Useful for isolated tests or separate broker pools. |
| `HSP_BROKER_LOG` | No | Override the broker log path. Default: `$XDG_STATE_HOME/hsp/broker.log` or `~/.local/state/hsp/broker.log`. |
| `HSP_BROKER_IDLE_TTL_SECONDS` | No | Idle broker session TTL. Default 14400 seconds. Set `0` to disable automatic idle eviction. |
| `LSP_DEVTOOLS` | No | Set `1`/`true`/`on` to expose the live broker to `python-devtools` for runtime introspection. Registers `broker`, `bus`, `registry`, and `lsp` under app id `hsp-broker` by default. |
| `LSP_DEVTOOLS_APP_ID` | No | Override the devtools app id. Default: `hsp-broker`. |
| `LSP_DEVTOOLS_READONLY` | No | Devtools readonly mode. Default: enabled, so agents can inspect broker state without mutation tools. |
| `HSP_PROBE_CAPABILITIES` | No | Opt into startup capability probing (`1`/`true`/`on`). Default off so MCP startup never launches heavy language servers before the initialize handshake. Runtime fallback still handles unsupported methods. |
| `LSP_PROJECT_MARKERS` | No | Comma-separated filenames or glob markers that mark a project root (e.g. `pyproject.toml,setup.py,*.csproj,.git`). When a file outside the current workspace folders is accessed, the bridge walks up looking for these markers and adds the found root to the LSP's workspace via `workspace/didChangeWorkspaceFolders`. Routes contribute their language's markers. Default: `.git`. |
| `LSP_WARMUP_PATTERNS` | No | Comma-separated glob patterns (e.g. `*.py,*.pyi` for Python, `*.rs` for Rust). When a workspace folder is added (initial spawn or via auto-detection), the bridge bulk-emits `textDocument/didOpen` for matching files so the LSP eagerly indexes them. Prevents the "cold index" failure mode where `willRenameFiles` returns 0 edits because nothing has been indexed yet. No warmup if unset. |
| `LSP_WARMUP_MAX_FILES` | No | Cap on how many files to warm per workspace folder. Default 500. |

**Legacy format** (still accepted when `LSP_SERVERS` is unset): `LSP_COMMAND`/`LSP_ARGS` for primary, `LSP_FALLBACK_COMMAND`/`LSP_FALLBACK_ARGS` for first fallback, `LSP_FALLBACK_2_COMMAND`/`LSP_FALLBACK_2_ARGS` for subsequent fallbacks. Prefer `LSP_SERVERS` for new configs.

**Chain behavior**: per-method. On `-32601` the next server in the chain is tried; the first success is cached for that method. All subsequent calls skip to the cached server. `LSP_PREFER` lets you pre-seed that cache to avoid the first-call cost when you already know which server handles a method best.

## Standalone / CLI Usage

```bash
uv tool install hsp     # or: pip install hsp

LSP_COMMAND=ty LSP_ARGS=server hsp mcp
LSP_COMMAND=rust-analyzer hsp mcp
LSP_COMMAND=gopls LSP_ARGS=serve hsp mcp
```

The MCP server speaks stdio through `hsp mcp` — useful for testing or for
non-plugin MCP clients. Bare `hsp` is reserved for the workgroup status/debug
surface, so an accidental terminal run shows the current bus/broker shape
instead of blocking on stdio.

## Architecture

```
Claude Code
    ↕ MCP (stdio)
hsp mcp
    ↕ JSONL / Unix socket  [broker mode, default]
hsp-broker
    ↕ JSON-RPC / LSP (stdio)
┌─── Primary LSP (ty, rust-analyzer, ...)
└─── Fallback LSP  (basedpyright, pyright, ...)  [lazy-spawned]
```

- Broker mode is default when an LSP chain or builtin router is configured.
  Multiple agents reuse the same broker-owned LSP chain for the same
  root/config hash, reducing CPU and keeping route selection, method routing,
  diagnostics, and future alias memory aligned.
- The broker owns the agent-bus slice: `lsp_log` appends workspace events to
  `tmp/hsp-bus.jsonl`, opens timed questions, records replies, settles closed
  windows, and renders compact weather. Tickets, build gates, and opt-in edit
  denial are documented in [docs/agent-bus.md](docs/agent-bus.md); harness
  support and open teamwork tickets live in
  [docs/harness-capability-matrix.md](docs/harness-capability-matrix.md).
- With `LSP_DEVTOOLS=1`, the broker starts `python-devtools` and registers
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
