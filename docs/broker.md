# hsp-broker Design Note

`hsp` currently runs as an MCP server that owns a short-lived chain of
language-server clients inside one agent/plugin session. That is the right
shape for today's Codex plugin, but agents change the value equation: multiple
clients may ask the same workspace the same semantic questions, and repeatedly
warming Roslyn, ty, basedpyright, or other servers wastes time and loses shared
semantic context.

This note started as a future direction. The first lifecycle slice now exists:
the MCP server defaults to broker-first mode when an LSP chain or builtin
router is configured, auto-starts `hsp-broker`, and forwards LSP requests over
the Unix socket. In router mode the MCP frontend sends URI/root context and the
broker owns route selection, chain hashing, and warm-session lookup. If the
broker is unavailable in `auto` mode, direct in-process spawning remains the
fallback.

## Thesis

The next layer should be a user-level language intelligence broker:

```text
editor / agent / worker
        |
   MCP or CLI client
        |
 hsp-broker daemon
        |
 workspace session manager
        |
 language server processes
        |
 shared disk caches / indexes
```

The broker does not replace language servers. It supervises them, keeps useful
workspace sessions warm, multiplexes multiple clients, and eventually provides
higher-level semantic operations that raw LSP does not make ergonomic.

## Why Not One OS-Wide LSP Server?

Language servers are workspace-shaped, not language-shaped.

For C#, a server session is tied to solution discovery, project references,
compiler options, source generators, analyzers, NuGet state, SDK selection, and
target frameworks. TypeScript, Rust, Python, Java, and other ecosystems have
similar workspace-specific state. An OS singleton would still need to manage
many roots and many toolchain/config variants.

LSP is also client-session-shaped. Initialization options, client capabilities,
dynamic registrations, open-document state, diagnostics ownership, cancellation,
progress, and file watching all assume a specific client talking to a specific
server. A daemon can exist, but it must be a broker that virtualizes client
sessions over workspace sessions.

## Why Agents Change The Payoff

Before agents, one editor was usually the only meaningful client. Editor-local
language-server lifecycles were good enough.

Agent workflows can involve:

- an editor,
- Codex,
- background explorer agents,
- refactor workers,
- test-fix workers,
- review agents,
- devtools or runtime inspectors.

All of them may ask for definitions, references, call graphs, diagnostics, and
rename previews against the same workspace. A warmed broker gives those clients
speed, but more importantly gives them a coordination substrate: stable semantic
answers tied to workspace snapshots.

## First Useful Slice

Do not start with unsaved overlays, distributed locking, or a persistent symbol
database. The smallest useful broker is a process supervisor with session reuse.
That slice is implemented as:

- `hsp-broker` / `python -m hsp.broker`
- JSONL over a user-scoped Unix socket
- MCP server request path: broker-first, direct fallback
- session key: `(root, chain config hash)`
- broker-owned route resolution for builtin routes (`HSP_ROUTER=1`),
- list active sessions,
- stop a session,
- queue/add workspace folders,
- forward LSP requests through one broker-owned language-server chain,
- keep direct `hsp` spawning as a fallback.

This preserves the current LSP bridge behavior while moving the lifecycle from
"per MCP process" to "per warm broker session." It is specifically aimed at
agent fanout: launching N subagents should not mean starting and warming N
copies of csharp-ls, ty, basedpyright, or other configured servers.

Runtime switches:

- `HSP_BROKER=auto` (default): use broker when an LSP chain is configured,
  fall back to direct mode if the broker cannot be reached.
- `HSP_BROKER=on`: require broker mode; broker transport errors surface.
- `HSP_BROKER=off`: keep the old direct in-process lifecycle.
- `HSP_BROKER_SOCKET=/path/to.sock`: isolate or override the broker socket.
- `HSP_BROKER_LOG=/path/to.log`: isolate or override the broker log.
- `HSP_BROKER_IDLE_TTL_SECONDS=14400`: evict idle sessions after this many
  seconds; `0` disables eviction.

## Session Model

A broker session owns:

- one project root,
- one language-server command chain,
- workspace folders registered with each server,
- warmed/opened documents,
- diagnostics cache,
- method routing cache,
- file watcher state,
- pending server lifecycle state.

Clients connect to the broker and borrow a session. The broker keeps idle
sessions alive for a configurable TTL and stops them when they age out.

Current implementation note: the registry stores session records, and
`BrokerLspSession` owns the live `LspClient` chain. Reference counting is still
future work; idle eviction and explicit root/config stop are implemented.

Session identity should be explicit and debuggable:

```text
route=csharp
language=csharp
root=~/holoq/repo-kit
command=csharp-ls
config_hash=...
started_at=...
last_used_at=...
workspace_folders=[...]
```

## Unsaved Buffers

Unsaved buffers are the hard part and should be deferred.

Two clients can hold different unsaved versions of the same file. A correct
broker eventually needs per-client overlays layered over shared workspace state:

```text
disk snapshot
  + client A overlay
  + client B overlay
```

Until overlays exist, the broker should define an honest contract: semantic
answers are against disk plus any documents explicitly opened through the same
broker client session. Agents should prefer saving or applying edits before
requesting workspace-wide semantic operations.

## Semantic Grep

The broker makes a "semantic grep" tool practical.

Raw LSP can answer references once the caller knows an exact file and position.
It generally cannot find every arbitrary local or parameter by bare name across
the workspace. The broker can implement the missing workflow:

1. Search text candidates for a name.
2. For each candidate occurrence, ask the language server what symbol is at that
   position.
3. Group occurrences by semantic identity.
4. Show the groups with representative definitions/usages.
5. Let the caller choose a group and then run references/rename/definition from
   that exact symbol.

This bridges the gap between `rg ctx` and true semantic references.

## Snapshot And Provenance

Agent coordination needs provenance. Broker responses should eventually include:

- session id,
- workspace root,
- language server label/version when known,
- git revision when available,
- file mtimes or content hashes for touched files,
- request method and position,
- whether unsaved overlays were involved.

This lets agents say "these callsites were computed against snapshot X" and
avoid confirming stale rename previews after unrelated edits.

Direct `hsp` should grow the local version first: snapshot stamps on
responses, named pending buffers, named semantic graph pins, and a mutation
journal. Those primitives do not require a broker, and they make the later
broker semantics concrete rather than speculative.

## Staged Edits And Prediction

Today mutation tools stage one in-process pending edit. The preview is useful,
but the state is single-slot: another agent can stage a different edit before
the first one confirms. A broker should model staged edits explicitly:

```text
stage name -> candidate list -> touched files -> snapshot -> owner/caller
```

Once staged edits are first-class, agents can ask predictive questions before
touching disk:

```text
lsp_what_if(stage="rename-output-texture", tool="diagnostics")
lsp_predict_conflict(stage="rename-output-texture")
lsp_witness(stage="rename-output-texture")
```

`what_if` runs read-only tools against an overlay of the staged edit. `witness`
applies a staged edit and reports before/after diagnostics, references, and
other verifier signals. `predict_conflict` compares staged edits across callers
and belongs naturally in the broker once it can see multiple clients.

## Agent Bus And Hooks

The broker should also own the agent bus described in `docs/agent-bus.md`.
Parallel agents do not need a file-claiming bureaucracy by default; they need
short, reliable weather reports at the boundaries where they are already about
to act.

Broker-owned bus state:

- append-only workspace event log,
- open timed questions,
- replies and notes,
- per-agent digest frontiers,
- hook-visible recent activity,
- file, symbol, and alias scope indexes.

The bus turns hooks into coordination points. Session start, edit hooks,
`lsp_confirm`, test runs, and git commit hooks can ask the broker for a compact
notice and print nothing when there is no useful signal. A question such as "I am
about to split `lsp_refs`; anyone touching `server.py`?" opens a timed window.
During the timeout, related events and replies are collected. At timeout, the
next hook output prints the digest.

This should stay warn-only for the first implementation. The useful dynamic is
not blocking edits; it is shifting agents with fresh context before they commit
or duplicate work. If hard claims or leases are added later, they should be
explicit policy on top of the bus rather than the core coordination model.

Current slice: `BrokerDaemon` owns an `AgentBus` instance and exposes it over
the JSONL protocol with `bus.*` methods. `lsp_log` is the MCP-facing workflow
tool, and every append is also persisted under the workspace's
`tmp/hsp-bus.jsonl` for replay/debugging. When `LSP_DEVTOOLS=1` is
set, the broker also registers `broker`, `bus`, `registry`, and `lsp` with
`python-devtools` (`app_id=hsp-broker` unless overridden), which gives
agents direct runtime introspection of daemon state without making devtools a
hard dependency.

Wave 2 layers ambient harness hooks over the same broker. There is no separate
`hsp-log`, `hsp-hook`, or `hsp-run` binary; `hsp log <action>` is the explicit
shell mirror of `lsp_log`, bundled plugin hooks call `hsp hook --kind <kind>`
with harness payloads on stdin, and `hsp run -- <command>` gates
build/verifier commands before recording their result. The hook adapter is
env-gated by `HSP_HOOKS` and no-ops before launching `uvx` by default. Session
start, user prompt, edit before/after, generic tool before/after, and detected
Bash build commands now ship inside the Claude plugin manifests. The CLI stays
warn-first: ordinary log/hook rows do not block, while the explicit build gate
path may wait or time out. See `docs/agent-bus.md` for the full hook taxonomy
and recipes.

## Relationship To hsp

`hsp` should remain useful without a broker.

The migration path was:

1. Extract current global LSP state behind reusable session-shaped pieces.
2. Keep the existing MCP server path using in-process state.
3. Add a broker daemon that owns shared LSP sessions.
4. Teach MCP plugins to try the broker first and fall back to direct mode.
5. Move render-memory alias coordination into broker session state: one master
   alias book per workspace, with a per-client introduction frontier.
6. Add the broker-backed agent bus: durable events, timed questions, hook
   notices, and per-agent digest frontiers.
7. Add broker-only tools once the lifecycle is stable.

That keeps adoption reversible and avoids turning an architecture experiment
into a hard runtime dependency.

## Open Questions

- What IPC should the broker use first: stdio subprocess, Unix socket, HTTP, or
  MCP-to-MCP?
- Should sessions be keyed only by command/env/root, or include discovered
  project graph metadata?
- How should workspace trust be represented when servers run project analyzers,
  source generators, or plugins?
- How should clients declare whether they have unsaved overlays?
- Should semantic grep live in the broker core, or as a tool layered over broker
  primitives?
- How aggressive should idle eviction be for high-memory servers like Roslyn?

## Non-Goals For The First Slice

- No universal language-server replacement.
- No persistent cross-project symbol database.
- No unsaved-buffer overlay engine.
- No cross-agent edit locking.
- No attempt to standardize editor UX.

The first win is simple: one warmed semantic service per workspace/configuration,
shared by multiple agent/editor clients, with transparent fallback to today's
direct MCP server.
