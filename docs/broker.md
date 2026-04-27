# cc-lsp-broker Design Note

`cc-lsp-now` currently runs as an MCP server that owns a short-lived chain of
language-server clients inside one agent/plugin session. That is the right
shape for today's Codex plugin, but agents change the value equation: multiple
clients may ask the same workspace the same semantic questions, and repeatedly
warming Roslyn, ty, basedpyright, or other servers wastes time and loses shared
semantic context.

This note records a future direction, not an active implementation plan.

## Thesis

The next layer should be a user-level language intelligence broker:

```text
editor / agent / worker
        |
   MCP or CLI client
        |
 cc-lsp-broker daemon
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
database. The smallest useful broker is a process supervisor with session reuse:

- `cc-lsp-broker daemon`
- `cc-lsp-broker request --workspace ROOT --server csharp-ls ...`
- session key: `(language, root, command, args, env/config hash)`
- list active sessions,
- stop a session,
- evict idle sessions,
- keep direct `cc-lsp-now` spawning as a fallback.

This first slice should preserve the current LSP bridge behavior while moving
the lifecycle from "per MCP process" to "per warm broker session."

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

Clients connect to the broker and borrow a session. The broker reference-counts
active clients and keeps idle sessions alive for a configurable TTL.

Session identity should be explicit and debuggable:

```text
language=csharp
root=/home/nuck/holoq/repo-kit
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

## Relationship To cc-lsp-now

`cc-lsp-now` should remain useful without a broker.

The migration path should be:

1. Extract current global LSP state behind an `LspSession` object.
2. Keep the existing MCP server path using an in-process `LspSession`.
3. Add a broker daemon that owns many `LspSession` objects.
4. Teach MCP plugins to try the broker first and fall back to direct mode.
5. Add broker-only tools once the lifecycle is stable.

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
