# Harness Capability Matrix

This matrix pins what HSP can rely on from each harness surface. It is the
planning artifact for teamwork behavior: presence, tickets, build gates, edit
denial, tool-output traffic, and the gaps that still need tickets.

Status legend:

| Status | Meaning |
|--------|---------|
| `wired` | Implemented in this repo and covered by tests. |
| `manual` | Available when an agent intentionally calls the HSP MCP/CLI surface. |
| `partial` | Some event path exists, but the harness cannot enforce or observe the whole behavior. |
| `missing` | Not implemented or not exposed by the current harness integration. |
| `unknown` | Not verified against a stable harness contract. |

## Surfaces

| Surface | Scope | Current role |
|---------|-------|--------------|
| HSP MCP tools | Any MCP-capable harness | Direct agent API for LSP tools plus team tools (`ticket`, `journal`, `ask`, `chat`, `build_gate`). |
| HSP CLI | Any shell-capable harness | `hsp log`, `hsp hook`, and `hsp run -- <command>` route shell/hook traffic into the same bus. |
| Claude Code plugin | Claude Code | Native hook carrier for session, prompt, stop, generic tool before/after, edit before/after, LSP redirect, build gating, and opt-in edit denial. |
| Codex harness | Codex/Codex-like | MCP tools and shell commands are available, but no HSP plugin hook adapter is defined in this repo. |
| Babel bridge | Babel daemon | Extrinsic session/activity/pane event ingestion into the workgroup bus. No enforcement path. |
| Broker | Shared daemon | Shared LSP sessions, bus state, presence, tickets, journal, ask/chat, build/edit gates. |
| Direct local fallback | Single process | Same API shape as the broker bus, but private to one process/session. |

## Capability Matrix

| Capability | HSP MCP | HSP CLI | Claude plugin | Codex harness | Babel bridge | Notes / tickets |
|------------|---------|---------|---------------|---------------|--------------|-----------------|
| Shared workgroup bus | wired | wired | wired through `hsp hook` | manual | partial | Broker shares by `workspace_root`; local fallback is private. See `WG-001`. |
| Presence heartbeat | wired | partial | wired | manual | wired | MCP tool calls send heartbeat; hooks/Babel add lifecycle signals. |
| Session start | missing | wired via hook adapter | wired | missing | wired | Claude manifest ships `SessionStart`; Codex plugin not present. |
| Session stop / `.end` | missing | wired via hook adapter | wired | missing | wired | Prompt `.end` maps to `session.stop`. |
| User prompt event | missing | wired via hook adapter | wired | missing | missing | Used for prompt-count pinning. |
| Generic tool before/after | missing | wired via hook adapter | wired | missing | partial | Claude generic `PreToolUse`/`PostToolUse`; Babel maps hook lifecycle where available. |
| Tool-output traffic injection | partial | partial | partial | manual | missing | HSP MCP responses include bus/header lines; hook output can inject only where harness displays hook stdout/stderr. See `WG-004`. |
| Hook digest injection | missing | missing | missing | missing | missing | Normal `hsp hook` records silently today; denial/build timeout are the only noisy hook paths. See `WG-004`. |
| Durable note/log | wired | wired | wired through CLI | manual | missing | `lsp_log(action="note")` / `hsp log note`. |
| Tickets | wired | wired | manual through MCP/CLI | manual | missing | `hsp.ticket("...")` / `hsp log ticket --message ...`. |
| Journal | wired | wired | manual through MCP/CLI | manual | missing | `hsp.journal()` / `hsp log journal`. |
| Ask/chat wait loop | wired | wired | manual through MCP/CLI | manual | missing | `hsp.ask(...)` waits; `hsp.chat(..., id="Qn")` unlocks. |
| Build gate query | wired | wired | wired for detected Bash commands | manual through `hsp run` | missing | Quiet, no journal event. |
| Build command wrapper | missing | wired | wired by Bash hooks + `hsp run` | manual | missing | Automatic Codex shell hook not defined. See `WG-003`. |
| Build result logging | partial | wired | wired for detected Bash commands | manual | missing | Recorded as `test.ran` after hook/wrapper completion. |
| Edit before/after logging | missing | wired via hook adapter | wired for `Edit`/`MultiEdit`/`Write` | missing | missing | Claude manifest has native edit matchers. |
| Edit denial without ticket | missing | wired via hook adapter | wired, opt-in | missing | missing | `HSP_REQUIRE_TICKET_FOR_EDITS=1`; `HSP_EDIT_GATE_SCOPE=agent` needs stable `HSP_AGENT_ID`. |
| `apply_patch` denial | missing | missing | not applicable to Claude plugin | missing | missing | Codex `apply_patch` is not intercepted by current HSP hooks. See `WG-002`. |
| LSP built-in redirect denial | missing | wired via redirect hook | wired for `LSP` tool | missing | missing | Claude-only plugin matcher redirects to HSP MCP LSP tools. |
| File/symbol scope extraction | missing | partial | partial | missing | missing | Hook adapter extracts obvious path/symbol fields and command paths only. See `WG-005`. |
| Stable agent identity | partial | partial | partial | partial | partial | `HSP_AGENT_ID` works if the harness propagates it consistently. See `WG-006`. |
| Workgroup marker discovery | missing | missing | missing | missing | missing | Current bus root is `$LSP_ROOT`/cwd, not `workgroup.toml`. See `WG-001`. |
| Capability self-report | missing | missing | missing | missing | missing | No `hsp.capabilities()` or `hsp log capabilities` yet. See `WG-007`. |
| Live bus replay after broker restart | missing | missing | missing | missing | missing | Events append to JSONL, but live `AgentBus` starts empty. See `WG-011`. |
| Internal workspace id consistency | partial | partial | partial | partial | partial | `AgentBus` and `BusRegistry` compute ids separately. See `WG-012`. |
| Internal confirm/test/commit/push stops | partial | partial | partial | missing | missing | Taxonomy exists, but not all stops are emitted internally. See `WG-013`. |

## Current Workgroup Detection

The team bus does not currently walk for `workgroup.toml`. The bus key is:

```text
workspace_root = $LSP_ROOT if set, otherwise os.getcwd()
```

That root owns tickets, journal, presence, ask/chat, gates, and
`tmp/hsp-bus.jsonl`. The LSP side separately detects project/session roots with
route markers such as:

| Route | Markers |
|-------|---------|
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `.git` |
| C# | `*.sln`, `*.csproj`, `Directory.Build.props`, `global.json`, `.git` |
| Rust | `Cargo.toml`, `rust-project.json`, `.git` |

If no workgroup marker exists, HSP still has a workspace: the process cwd (or
`LSP_ROOT`). If the broker is enabled and reachable, agents with the same root
share the workgroup. If broker mode is off or falls back locally, the workgroup
is process-local and effectively ephemeral.

## Ticket Register

| Ticket | Priority | Status | Description |
|--------|----------|--------|-------------|
| `WG-001` | high | open | Add explicit workgroup discovery (`workgroup.toml` / `.hsp/workgroup.toml`) and make bus root selection visible in `weather`. Decide precedence against `$LSP_ROOT` and LSP route roots. |
| `WG-002` | high | open | Define a Codex/apply-patch interception strategy. Current HSP cannot deny Codex `apply_patch` edits because no repo hook path observes that tool. |
| `WG-003` | high | open | Add a Codex hook/plugin adapter if the harness supports shell/tool hooks; map shell commands to `hsp hook` and document unsupported events. |
| `WG-004` | high | open | Make tool-output traffic injection explicit: one compact workgroup header/digest per HSP tool result, with a frontier to avoid repeated journal spam. |
| `WG-005` | medium | open | Improve file/symbol scope extraction from hook payloads and command strings; use LSP identities when possible for edit/result rows. |
| `WG-006` | high | open | Define stable agent identity propagation across MCP server process, shell hooks, Babel panes, and subagents; document required env (`HSP_AGENT_ID`). |
| `WG-007` | medium | open | Add `hsp.capabilities()` / `hsp log capabilities` rendering this matrix from code/config so agents can query live policy. |
| `WG-008` | medium | open | Make build command detection configurable instead of hard-coded first-token/subcommand lists. |
| `WG-009` | medium | open | Add a trial playbook that records expected events for two agents: ticket start, denied edit, allowed edit, build wait, ask/chat, release, build result. |
| `WG-010` | low | open | Add capability rows for more harnesses as they are connected (Goose, Aider, Cursor, etc.) without weakening the core HSP contracts. |
| `WG-011` | high | open | Wire durable JSONL replay into the broker-owned live `AgentBus`, or delete the dead parallel `BusJournal` path and make durability single-source. |
| `WG-012` | medium | open | Collapse duplicate workspace-id hashing into one helper shared by `AgentBus`, `BusRegistry`, docs, and tests. |
| `WG-013` | medium | open | Emit `confirm.before/after`, test, commit, and push stops from HSP internals/wrappers where possible; today several are taxonomy-only. |
| `WG-014` | low | open | Add direct unit coverage for `hsp.redirect_hook.main` denial JSON and keep the README/plugin docs aligned with the opt-in denial policy. |
| `WG-015` | medium | open | Bring Codex plugin manifests to parity with the current HSP version/routes and document which hook capabilities remain absent; the root manifest is currently MCP/interface-only and behind the bundled plugin metadata. |

## Trial Profiles

Recommended first hard-policy trial:

```text
HSP_HOOKS=1
HSP_REQUIRE_TICKET_FOR_EDITS=1
HSP_BUILD_GATE_TIMEOUT=2m
```

Recommended stricter identity trial, only after hook and MCP processes share
the same id:

```text
HSP_HOOKS=1
HSP_REQUIRE_TICKET_FOR_EDITS=1
HSP_EDIT_GATE_SCOPE=agent
HSP_AGENT_ID=<stable-agent-id>
```

Expected behavior:

1. Edit without a ticket is denied by the pre-tool hook.
2. `hsp.ticket("...")` unlocks edits.
3. Build commands wait on active tickets.
4. If all ticket holders reach the build gate, the build proceeds.
5. `hsp.ticket("")` releases the ticket and closes it when last holder leaves.
