# Agent Bus

The agent bus is the coordination layer for parallel agents working in the same
workspace. It should feel like weather, not a traffic cop: compact situational
awareness appears at the next natural boundary, and agents adjust course without
needing a lock ritual.

The core idea is simple:

```text
append events -> open timed questions -> inject compact digests at bus stops
```

No hard blocking is part of the first slice. Claims, leases, and permission
gates are the wrong default because they invite bypass behavior and agent
fights. The bus changes the environment by making the relevant nearby motion
visible.

## Goals

- Give parallel agents a compressed signal about overlapping work.
- Let agents ask short coordination questions with a timeout.
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
| `recent` | Show recent related bus activity. |
| `settle` | Close expired questions and show pending digests. |
| `precommit` | Summarize touched files, overlaps, related edits, and suggested checks. |
| `postcommit` | Record a commit and reset the local task digest frontier. |
| `weather` | Compact workspace status for a new or resumed agent. |

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

## Hook CLI

Hooks need a stable CLI so shell integrations do not need to know MCP internals:

```text
cc-lsp-now-log hook --kind pre_edit --files src/server.py
cc-lsp-now-log hook --kind post_edit --files src/server.py --symbols lsp_refs
cc-lsp-now-log hook --kind pre_commit
cc-lsp-now-log hook --kind post_commit --commit d796fc8
cc-lsp-now-log hook --kind test --status passed --targets tests/test_lsp_refs.py
```

The hook command should print nothing when there is no useful signal. Silence is
part of the interface.

Git command wrapping is useful but fragile. Agents can run `git commit` through
shell pipelines, aliases, scripts, or command substitutions, and trying to catch
every spelling turns the bus into brittle enforcement. Prefer native harness
hooks when available, then lightweight git wrapper hooks, then explicit
`lsp_log(action="precommit")` prompts as the fallback. Every path should produce
the same weather report; none should be required for correctness in the first
slice.

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

## First Slice

The initial implementation is now broker-backed and intentionally advisory:

1. `src/cc_lsp_now/bus_event.py` owns the strict event/scope wire schema.
2. `src/cc_lsp_now/agent_bus.py` owns in-memory state, timed questions,
   append-only JSONL persistence at `tmp/cc-lsp-now-bus.jsonl`, and compact
   digest queries.
3. `BrokerDaemon` exposes `bus.event`, `bus.note`, `bus.ask`, `bus.reply`,
   `bus.recent`, `bus.settle`, `bus.precommit`, `bus.postcommit`,
   `bus.weather`, and `bus.status`.
4. The MCP surface is `lsp_log(action="event|note|ask|reply|recent|settle|precommit|postcommit|weather")`.
5. `CC_LSP_DEVTOOLS=1` registers the live broker, bus, registry, and LSP
   manager with `python-devtools` under app id `cc-lsp-now-broker` by default,
   so agents can inspect daemon state without adding bespoke debug endpoints.
6. Coordination remains warn-only: no claims, no leases, no denial path.

Hook output is the next slice. The current bus already has the broker methods
and rendering helpers needed for session-start, edit, test, and commit hooks to
call into the same substrate.

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

## Later Work

- Use semantic identity to match questions against touched symbols, not only
  paths and text names.
- Suggest tests from recent tool traces, diagnostics, touched files, and call
  graph neighborhoods.
- Let `lsp_path` and `lsp_impact` add high-signal neighborhood events.
- Add summarization budgets so a busy workspace still prints one tight notice.
- Explore opt-in hard policies after the warn-only loop proves itself, but keep
  the public default as weather rather than enforcement.
