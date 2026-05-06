# Agent Bus

The agent bus is the coordination layer for parallel agents working in the same
workspace. It should feel like weather, not a traffic cop by default: compact
situational awareness appears at the next natural boundary, and agents adjust
course without needing a lock ritual. See
`docs/harness-capability-matrix.md` for the exact harness support matrix and
open teamwork tickets.

The core idea is simple:

```text
append events -> hold tickets -> ask/chat -> inject compact digests at bus stops
```

The bus is not a file lock manager. Tickets are project-level work signals:
they tell other agents "someone is actively changing the substrate" and let
build wrappers wait for a quiet or mutually-waiting moment. The build gate is
the one intentional stop sign, and it is silent: it does not append a board
event or broadcast pressure to hurry a worker.

## Goals

- Give parallel agents a compressed signal about overlapping work.
- Let agents ask short coordination questions with a timeout.
- Let agents announce and release active work without claiming files.
- Keep build commands from racing active edits unless every holder is waiting.
- Surface related edits, tests, commits, notes, and replies at hook boundaries.
- Keep the public model line-oriented and easy for an agent to scan.
- Preserve provenance: workspace, git head, agent/session, files, symbols, and
  aliases.
- Stay reversible: the bus warns and records; it does not own policy yet.

## Non-Goals

- No mandatory file or symbol claims.
- No edit denial in the first implementation.
- No expectation that MCP can push arbitrary messages to another live agent.
- No hidden chat room requiring polling by the model.
- No replacement for git, tests, or the LSP verifier tools.

Hooks are the delivery mechanism. If the harness can prepend or append text to
tool output, the bus can make coordination reactive without needing to interrupt
another agent.

## Event Log

The broker should own a workspace-scoped append-only JSONL log. Direct MCP mode
can start with a local file under the project `tmp/` directory, but the broker is
the durable home because it already knows the workspace, sessions, aliases, and
live LSP state.

Canonical event fields:

```text
event_id
event_type
timestamp
workspace_id
workspace_root
agent_id
session_id
task_id
git_head
dirty_hash
files
symbols
aliases
message
metadata
```

Initial event types:

| Event | Meaning |
|-------|---------|
| `agent.started` | A session joined a workspace. |
| `task.intent` | An agent stated what it is about to do. |
| `file.touched` | A file was edited or staged by a tool/hook. |
| `symbol.touched` | A semantic target was edited, staged, renamed, or moved. |
| `test.ran` | A verifier command ran with pass/fail and target names. |
| `commit.created` | Git history advanced. |
| `note.posted` | Human or agent message intended for nearby workers. |
| `ticket.started` | First agent began working on a project ticket. |
| `ticket.joined` | Another agent joined an existing ticket with the same message. |
| `ticket.released` | An agent released its current ticket. |
| `ticket.closed` | The last holder released a ticket. |
| `chat.message` | Workgroup chat row not attached to a question. |
| `bus.ask` | Timed coordination question opened. |
| `bus.reply` | Reply attached to an open question. |
| `bus.closed` | Question timeout elapsed and digest was emitted. |

Events should be cheap and lossy in display, but not lossy in storage. The
rendered notice can omit most fields; the JSONL record should keep enough data
to reconstruct why a digest was shown.

## Bus Windows

A bus window is a timed question plus all events that occur before it closes.
The opener gives a message, optional scope, and timeout. The broker records the
question and every hook checks whether a visible question overlaps the current
action.

Example:

```text
lsp_log(action="ask",
        message="I am about to split lsp_refs fanout; anyone touching server.py?",
        files="src/server.py,tests/test_lsp_refs.py",
        symbols="lsp_refs,_reference_section_for_target",
        timeout="3m")
```

During the timeout, bus stops show a compact notice:

```text
[lsp-bus question 2m14s left]
agent noesis: about to split lsp_refs fanout; anyone touching server.py?
scope: src/server.py tests/test_lsp_refs.py
reply: lsp_log(action="reply", id="Q12", message="...")
```

At timeout, the next bus stop emits the digest:

```text
[lsp-bus Q12 closed]
question: split lsp_refs fanout?
events during 3m:
  amanuensis edited docs/tool-surface.md
  reverie ran tests/test_lsp_calls.py passed
  noesis touched src/server.py::_reference_section_for_target
replies:
  reverie: root handles shifted indices in lsp_calls
suggest:
  include tests/test_lsp_calls.py with refs tests
```

The timeout is coordination pressure, not a lock. If nobody replies, the opener
still gets useful telemetry about what moved nearby during the window.

## Tickets And Build Gate

Every active worker should hold one current ticket. Starting a ticket is the
agent's "I am changing the project" signal:

```text
hsp.ticket("wire workgroup ticket state", files="src/hsp/agent_bus.py")
```

Passing an empty message releases the agent's current ticket:

```text
hsp.ticket("")
```

The broker coalesces identical messages in the same workspace. The first holder
emits `ticket.started`; later holders emit `ticket.joined`; release emits
`ticket.released`; the last release also emits `ticket.closed`. The current
implementation intentionally keeps one ticket per agent because the ergonomic
unit is "what am I doing now?", not a stack of claims to remember.

Build hooks and wrappers call the gate before running expensive or
state-sensitive commands. This is intentionally ambient: agents should run the
build command normally and let HSP detect build-shaped shell commands through
the harness hook, or use `hsp run -- <command>` from shell scripts:

```text
hsp run -- cargo test
```

The internal gate returns `unlocked` when there are no active ticket holders
covering the checker scope, or when every current overlapping holder has also
reached the build gate and is waiting. Workspace-wide commands such as
`cargo check` with no path arguments cover the full workgroup. Path-scoped
commands such as `ruff check src/hsp/cli.py`, `pytest tests/foo.py`, or
`go test ./pkg` wait only on active tickets whose files/symbols overlap that
scope. Unknown-scope tickets still block scoped checkers because HSP cannot
prove the command is unrelated. The all-waiting case prevents deadlock: if all
agents independently arrived at "I need the build", the build can proceed.
Checking the gate is not written to the journal and is not broadcast; it should
not rush active editors.

When a build/checker hook unlocks because every overlapping ticket holder is
waiting, HSP becomes authoritative for that command. The hook runs the command
once under `tmp/hsp-build-batches/`, captures stdout/stderr, records the
`test.ran` result, and returns a harness denial payload so the original Bash
tool does not execute again. Other agents hitting the same command/gate shape
reuse the batched result instead of compiling again. A normal `clear` gate still
lets the harness run the command directly.

When an agent starts a new ticket, its stale build-wait marker is cleared. This
keeps an old "waiting for build" state from accidentally unlocking a later
build while the same agent is editing again.

## Edit Gate

Harnesses that support `PreToolUse` denial can make tickets mandatory for
edits. Set:

```text
HSP_REQUIRE_TICKET_FOR_EDITS=1
```

When enabled, the `edit.before` hook checks `edit_gate` before recording the
edit event. If the gate fails, the hook returns the harness denial payload and
the edit tool does not run. The denial tells the agent to start a ticket and
retry:

```text
hsp.ticket("editing workgroup bus policy")
```

The default scope is `workgroup`: any active ticket in the workspace unlocks
edits. This is the practical mode for mixed harnesses because MCP tools and
shell hooks may run in separate processes. For stricter ownership, set:

```text
HSP_EDIT_GATE_SCOPE=agent
HSP_AGENT_ID=<stable-agent-id>
```

In `agent` scope, the current hook process must present the same `HSP_AGENT_ID`
as the ticket holder. Without a stable id, the hook cannot prove that the MCP
agent holding the ticket is the shell process attempting the edit.

## Journal And Chat

`hsp.journal()` displays the compact shared board: open tickets, open questions,
and the latest rows, defaulting to roughly the last 25 events. Rows should stay
one line where possible:

```text
journal: 4
  E1 08:09:10 ticket.started wire workgroup ticket state [files=src/hsp/agent_bus.py]
  E2 08:10:03 note.posted lsp route warmed
```

`hsp.chat("...")` is the workgroup chat verb. With no id it appends
`chat.message`; with an ask id it records `bus.reply` and closes the question
immediately:

```text
hsp.chat("all ticket holders are waiting", id="Q3")
```

`hsp.ask("...", timeout="2m")` is the waiting form for agents that need a reply.
It opens `bus.ask`, waits until a matching `chat(..., id="Qn")` arrives or the
timeout elapses, and returns the latest journal on timeout. If no agents are
currently busy in the workgroup (no active ticket holders), it does not wait:
the question is recorded, closed immediately, and the result says that no
agents can reply. This makes coordination a treadmill: ask, wait briefly when
there is someone to answer, read the board, continue.

## Board Messages

The bus also acts as a small message board. A note is a durable message without a
timeout; a question is a note that expects replies and closes into a digest.
Both should be scoped when possible:

```text
lsp_log(action="note",
        message="Root graph handles shifted while expanding ambiguous calls.",
        files="src/server.py,tests/test_lsp_calls.py",
        symbols="lsp_calls,_call_section_for_target")
```

Board messages should appear in clear hook notices, but they should decay.
Repeated output from the same note quickly becomes clutter, so each agent needs a
digest frontier: show what is new to this agent, then compress or suppress it
until another related event makes it fresh again.

## Bus Stops

Bus stops are hook points where the system can safely inject a compact notice
without interrupting an agent mid-thought:

- session start,
- before edit,
- after edit,
- before `lsp_confirm`,
- after `lsp_confirm`,
- before git commit,
- after git commit,
- before push/pull,
- after tests,
- after LSP mutations such as rename, move, and fix,
- general command output hooks when the harness supports them.

Every stop should run the same policy:

1. Record the current event if the hook has one.
2. Find open questions whose file/symbol/alias scope overlaps.
3. Find recent related events since the agent last saw the bus.
4. Print the smallest useful notice.

This keeps the bus aligned with how agents already work: they catch the next
natural boundary and adjust trajectory.

## Tool Shape

`lsp_log` is the public MCP surface for the bus. It should be intentionally
small:

| Action | Purpose |
|--------|---------|
| `event` | Append a structured event. |
| `note` | Post a visible note without a timeout. |
| `ask` | Open a timed bus question. |
| `reply` | Attach a reply to an open question. |
| `chat` | Post a chat row, optionally replying to and closing an ask id. |
| `ticket` | Start/join this agent's current work ticket, or release with an empty message. |
| `journal` | Show open tickets/questions and the latest compact event rows. |
| `edit_gate` | Quietly report whether the current edit hook may proceed. |
| `recent` | Show recent related bus activity. |
| `settle` | Close expired questions and show pending digests. |
| `precommit` | Summarize touched files, overlaps, related edits, and suggested checks. |
| `postcommit` | Record a commit and reset the local task digest frontier. |
| `weather` | Compact workspace status for a new or resumed agent. |
| `presence` / `workgroup` | Show the derived agent roster: active, asleep, and pinned rows. |

Example precommit output:

```text
Your touched files:
  src/server.py
Overlapping active questions:
  Q12 noesis: split lsp_refs fanout around src/server.py
Recent related edits:
  d796fc8 Fan out refs for ambiguous symbols
Suggested:
  run tests/test_lsp_calls.py tests/test_lsp_refs.py
```

The output should avoid forks that require conscious policy decisions. It should
make the next safe action obvious: run the named tests, inspect the named file,
reply to the named question, or continue.

## Bus CLI

Shell hooks talk to the bus through the same `hsp` binary as the MCP server.
There is no separate `hsp-log` or `hsp-hook`; `hsp log` is the explicit
operator surface and `hsp hook` is the bundled plugin hook adapter that reads
the harness JSON payload from stdin. Both land in the same `lsp_log`/`bus.*`
event stream.

```text
hsp log weather
hsp log recent
hsp log settle
hsp log ticket --message "wire workgroup ticket state" --files src/hsp/agent_bus.py
hsp log ticket --message ""
hsp log journal
hsp log chat --id Q3 --message "all holders waiting"
hsp run -- cargo test
hsp log edit_gate --status workgroup
hsp log note --message "..." --files src/server.py
hsp log ask --message "Anyone touching server.py?" --files src/server.py --timeout 3m
hsp log reply --id Q3 --message "done"
hsp log hook --kind edit.after --files src/server.py
hsp log hook --kind commit.after --commit d796fc8
hsp hook --kind edit.after < "$CLAUDE_HOOK_PAYLOAD"
hsp run -- cargo test
hsp
hsp workgroup . ../other-repo --limit 5
```

Each subcommand prints a compact bus-stop notice or stays silent when no
related motion is nearby. Silence is part of the interface; harness hooks
should pass output straight through without prefixing or summarizing it.

The CLI stays warn-first at this layer too. `hsp log` never blocks the
caller, never claims a file, and never returns a non-zero exit code to gate an
edit — it only describes the surrounding weather. `hsp run` is the explicit
exception for build and verifier commands: it waits on the internal build gate,
executes the command with normal stdio, and records one `test.ran` row with
pass/fail status after the process exits. A gate timeout returns `124` and
does not run the command. Build gating is not an `hsp log` action because
agents should not need to remember or invoke it manually.

### Hook Recipes

These are the ambient stops the Claude plugin ships. Users should not copy
hook blocks by hand; the plugin owns the hook declarations and each hook calls
`hsp hook --kind <kind>` with the JSON payload on stdin. The hook adapter is
env-gated twice: the bundled shell command drains stdin and exits before
launching `uvx` when `HSP_HOOKS` is unset or false, and the Python adapter also
no-ops defensively. Setting `HSP_HOOKS=1` turns the feature on.

The shipped Claude hook slice records every available lifecycle hook that the
plugin can receive: session start/stop, user prompt, notification, subagent
stop, pre-compact, generic tool before/after, and edit before/after. Test,
commit, push, and `lsp_confirm` stops remain in the taxonomy below because
they require shell/tool wrappers or HSP-internal hook points rather than native
Claude hook events.

| Stop | Hook kind | Example invocation |
|------|-----------|--------------------|
| session start | `session.start` | `hsp hook --kind session.start` |
| session stop / `.end` | `session.stop` | `hsp hook --kind session.stop` |
| user prompt | `prompt` | `hsp hook --kind prompt` |
| tool start | `tool.before` | `hsp hook --kind tool.before` |
| tool finish | `tool.after` | `hsp hook --kind tool.after` |
| notification | `notification` | `hsp hook --kind notification` |
| subagent stop | `subagent.stop` | `hsp hook --kind subagent.stop` |
| pre-compact | `compact.before` | `hsp hook --kind compact.before` |
| before edit | `edit.before` | `hsp hook --kind edit.before` |
| after edit | `edit.after` | `hsp hook --kind edit.after` |
| before `lsp_confirm` | `confirm.before` | `hsp hook --kind confirm.before` |
| after `lsp_confirm` | `confirm.after` | `hsp hook --kind confirm.after` |
| after tests | `test` | `hsp hook --kind test` |
| before git commit | `commit.before` | `hsp hook --kind commit.before` |
| after git commit | `commit.after` | `hsp hook --kind commit.after` |
| before push/pull | `push.before` | `hsp hook --kind push.before` |
| after push/pull | `push.after` | `hsp hook --kind push.after` |

Session start and prompt stops emit weather; edit/confirm/test/commit/push
stops record a touched-files event and then emit any digest the broker has
queued for that scope. The same broker decides whether to surface an open
question, a settled digest, a related commit, or nothing.

Generic Bash hooks also recognize common build/verifier commands such as
`cargo test`, `cargo check`, `cargo clippy`, `uv run ...`, `python -m pytest`,
`ruff check`, `mypy`, `eslint`, `prettier --check`, `biome check`, `shellcheck`,
`npm test`, `pnpm run`, `yarn build`, `bun test`, `deno lint`, `go test`,
`go vet`, `make`, `just`, `dotnet test`, `rk test`, `tox`, `nox`, and similar
first-token / subcommand pairs across common ecosystems. On the before hook
they run the quiet build gate and do not append a journal row; on the after hook
they record `test.ran` with normalized `passed` / `failed` status. Set
`HSP_BUILD_GATE_TIMEOUT` to tune the hook wait window; the default is `2m`.

When `HSP_REQUIRE_TICKET_FOR_EDITS=1`, edit before hooks become hard gates.
They use the same broker bus and return a harness-native denial payload rather
than a normal bus-stop notice. This is deliberately opt-in because it changes
the edit hook from weather into policy.

### Timed Questions

`ask` and `reply` are not hook stops; they are the coordination move an agent
makes when it wants to shape the upcoming weather rather than just observe it.
The CLI shape mirrors the MCP actions one-to-one:

```text
hsp log ask --message "Anyone touching server.py?" \
                   --files src/server.py --timeout 3m
hsp log reply --id Q3 --message "done"
```

`ask` returns the question id (`Q3`, etc.) so a worker can address replies
without scraping `recent`. While the question is open, every hook stop above
whose scope overlaps the question appends a compact "Q3 still open" line. At
timeout, the next stop emits the digest described in *Bus Windows*. If there
are no active ticket holders when the question is opened, `ask` returns a
no-replier notice instead of leaving a stale open question. Nothing about this
gates an edit; the timeout is coordination pressure, not a lock.

### Wiring Notes

Prefer native harness hooks where they exist, lightweight git wrappers as a
second choice, and explicit `lsp_log(action="precommit")` prompts as the
manual fallback. Git wrapping in particular is useful but fragile: agents
invoke `git commit` through pipelines, aliases, scripts, and command
substitutions, and chasing every spelling turns the bus into brittle
enforcement. All three paths funnel through the same `hsp log` surface,
so the broker still sees one event stream regardless of which path fired.

## Broker Relationship

The broker is the natural owner because it can unify:

- warm LSP sessions,
- render aliases and per-agent introduction frontiers,
- staged edit previews,
- workspace snapshot/provenance stamps,
- event logs and bus windows.

Alias alignment matters for coordination. If one agent has seen `A3` and
another has not, the broker can keep a master legend while each client receives
only aliases that have already been introduced in that client's context. Bus
messages should prefer file/symbol names first, then aliases when they are known
to that recipient.

## Wave 1: Broker-Backed Bus

The initial implementation is broker-backed and intentionally advisory:

1. `src/hsp/bus_event.py` owns the strict event/scope wire schema.
2. `src/hsp/agent_bus.py` owns in-memory state, timed questions,
   append-only JSONL persistence at `tmp/hsp-bus.jsonl`, and compact
   digest queries.
3. `BrokerDaemon` exposes `bus.event`, `bus.note`, `bus.ask`, `bus.reply`,
   `bus.chat`, `bus.ticket`, `bus.journal`, `bus.build_gate`, `bus.recent`,
   `bus.settle`, `bus.precommit`, `bus.postcommit`, `bus.weather`, and
   `bus.status`. `bus.build_gate` is internal to hooks/wrappers.
4. The MCP surface is `lsp_log(action="event|note|ask|reply|chat|ticket|journal|recent|settle|precommit|postcommit|weather")`,
   plus short tools `hsp.ticket`, `hsp.journal`, `hsp.ask`, and `hsp.chat`.
5. `LSP_DEVTOOLS=1` registers the live broker, bus, registry, and LSP
   manager with `python-devtools` under app id `hsp-broker` by default,
   so agents can inspect daemon state without adding bespoke debug endpoints.
6. Coordination remains warn-only: no claims, no leases, no denial path.

## Wave 2: Ambient Hook Surface

Wave 2 wires the same broker substrate into harness-fired hook bodies through
a single `hsp` binary. The shape is:

1. No new binary. `log`, `hook`, and `mcp` are subcommands on `hsp`; there is
   no `hsp-log` or `hsp-hook`. One entrypoint keeps install paths, broker
   discovery, and socket auth identical between the MCP server and hooks.
2. The subcommand mirrors public `lsp_log` actions where useful (`weather`,
   `journal`, `recent`, `settle`, `ticket`, `chat`, `note`, `ask`, `reply`,
   `hook`, `precommit`, `postcommit`, `event`). Build gating is not a public
   log action; it is driven by `hsp hook` build-command detection and `hsp run`.
3. Bare `hsp` is the non-mutating debugger for "where is this agent's team
   state?" `hsp workgroup [locations...]` is the same query surface with
   explicit locations/options. It prints the resolved workgroup root, workspace
   id, broker socket/log, append-log paths, live broker weather if reachable,
   and optional LSP session status with `--lsp`. It does not auto-start a
   broker unless `--start-broker` is passed.
4. MCP launch is explicit as `hsp mcp`; plugin manifests must pass that
   subcommand so a terminal `hsp` never blocks on stdio by accident.
5. Bundled plugin hooks are opt-in with `HSP_HOOKS=1` and no-op otherwise.
6. Ambient stops cover session, prompt, edit before/after, `lsp_confirm`
   before/after, test result, git commit before/after, and push before/after.
   The broker decides per-stop whether the digest is worth printing; silent
   exit is the common case.
6. Stays warn-first by default: no file claims and no edit denial unless
   `HSP_REQUIRE_TICKET_FOR_EDITS=1` is set. The build gate is the implicit
   quiet build stop. `hsp run` and detected build-command before hooks are
   allowed to wait or time out; hook bodies that pipe other `hsp log` output
   through must not interpret it as a gate.
6. Timed questions (`ask`/`reply`) layer on top of the same stops: open
   questions whose scope overlaps the current stop append a compact reminder,
   and at timeout the next stop emits the closing digest.

## Implementation Notes

These are the load-bearing decisions Wave 1 has settled on. They are narrow
enough to be implementation detail, but durable enough that agents and broker
code can rely on them without re-litigating. Cross-cutting acceptance lives in
`tests/test_agent_bus_contract.py`.

### Workspace Auto-Detection

`workspace_root` is the only sharding key. It is auto-detected from `$LSP_ROOT`
if set, otherwise from the agent's current working directory (resolved via
`os.path.abspath`). `workspace_id` is a short SHA-1 digest of the resolved root
so the broker, JSONL log, and digest-frontier state all agree without depending
on path normalization elsewhere.

LSP `config_hash` deliberately does **not** shard the bus. Two agents running
different chains in the same repo (e.g. one with `ty` only, another with
`ty;basedpyright`) share recent events; otherwise the weather report would
split per chain config and lose exactly the cross-chain visibility the bus is
for.

### User Prompt Hook And Prompt Count

`user.prompt` is the canonical event for "the user spoke to this agent." Hooks
should append one `user.prompt` event per turn; the event's
`metadata.prompt_count` is the running count for that `agent_id`. The bus uses
this count to distinguish ambient context agents from the user's main
conversation thread:

- `prompt_count >= 2` pins the agent visible in presence output even past the
  prune threshold. The pin survives because the user has actively chosen to
  keep talking to this thread; pruning it would lose exactly the agent the
  human is steering.
- `prompt_count <= 1` is treated as a single-shot or warm-up agent and follows
  the normal active/asleep/pruned decay below.

If a prompt hook carries exactly `.end`, the hook adapter records
`session.stop` instead of another prompt. That is the user-facing escape hatch
for marking a thread done even when the harness does not provide a native exit
hook.

### Presence Decay

Presence is decided by the time since each agent's last bus event
(`agent.started`, `user.prompt`, or any other event with a non-empty
`agent_id`):

| State | Threshold | Visibility |
|-------|-----------|------------|
| `active` | `< 60s` | shown prominently in `weather` and `recent`. |
| `asleep` | `>= 60s` | shown dimmed; the agent has gone quiet. |
| `pruned` | `>= 600s` | hidden by default; only surfaces when explicitly listed. |

`prompt_count >= 2` overrides pruning for that agent — the main thread stays
visible regardless of how long it has been silent. These thresholds are cheap
to revisit; the durable contract is the *shape* (three bands monotonic by
recency, plus the prompt-count pin), not the exact second counts.

Any HSP MCP tool call also sends a presence-only `agent.heartbeat` into the
broker. Heartbeats update the workgroup roster without appending JSONL event
noise, so agents appear as soon as they use the HSP tools even if their harness
hooks are not enabled.

## Babel Bridge

HSP can ingest Babel's daemon event stream as an extrinsic source of truth.
Set `HSP_BABEL_BRIDGE=1` on the broker process to subscribe to Babel's Unix
socket (`$XDG_RUNTIME_DIR/babel.sock`, or `/tmp/babel-<uid>.sock`) and fold
`session_state_changed`, `activity_pulse`, hook lifecycle, focus, and pane
open/close events into the same bus. Babel events are stored with
`metadata.source=babel` and `metadata.native_event=<event>`, while the bus kind
is normalized to workgroup concepts such as `agent.heartbeat`, `session.start`,
`session.stop`, `tool.before`, and `tool.after`.

## Later Work

- Use semantic identity to match questions against touched symbols, not only
  paths and text names.
- Suggest tests from recent tool traces, diagnostics, touched files, and call
  graph neighborhoods.
- Let `lsp_path` and `lsp_impact` add high-signal neighborhood events.
- Add summarization budgets so a busy workspace still prints one tight notice.
- Explore opt-in hard policies after the warn-only loop proves itself, but keep
  the public default as weather rather than enforcement.
