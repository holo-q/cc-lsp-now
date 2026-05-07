# Harness Capability Matrix

This matrix is anchored to Babel's harness roster, not to HSP's internal
surfaces. Babel defines the authoritative agent kinds in
`../../repo-os/babel/src/agent_kind.rs` and summarizes them in
`../../repo-os/babel/README.md`. HSP consumes that roster as the workgroup
planning frame: which harnesses can announce presence, hold tickets, inject
journal context, deny edits, and wait builds.

Status legend:

| Status | Meaning |
|--------|---------|
| `wired` | Implemented in HSP today. |
| `babel` | Babel has a canonical adapter/contract, but HSP does not yet carry that signal natively. |
| `manual` | Available only when the agent intentionally calls HSP MCP/CLI tools. |
| `bridge` | Requires a harness-side bridge into Babel/HSP. |
| `blocked` | Harness lacks the stable identity or lifecycle surface needed for workgroup orchestration. |
| `open` | Needed, ticketed, and not implemented. |

## Babel Roster

| Harness | Babel slug | Babel tier | Native identity | Babel lifecycle surface | HSP workgroup support today | Hard-gate support today | Tickets |
|---------|------------|------------|-----------------|-------------------------|-----------------------------|-------------------------|---------|
| Claude Code | `claude` | supported, daily-driver | `session_id` | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `Notification`, `SubagentStop`, `PreCompact` | `wired`: HSP Claude plugin hooks call `hsp hook`; MCP tools expose `ticket`, `journal`, `ask`, `chat` | `wired`: LSP redirect denial, opt-in edit denial for `Edit`/`MultiEdit`/`Write`, implicit build wait for detected Bash commands | `WG-004`, `WG-005`, `WG-014`, `WG-019` |
| Codex CLI | `codex` | supported, daily-driver | `session_id` | Babel has canonical Codex hooks, including `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `PermissionRequest` | `manual`: current HSP Codex plugin is MCP/skills only; Babel can observe if its Codex hook snippet is installed | `open`: HSP cannot intercept Codex `apply_patch` or shell commands without a Codex hook/plugin carrier | `WG-002`, `WG-003`, `WG-015`, `WG-019` |
| Factory Droid | `factory-droid` | supported roster adapter | `session_id` | Claude-compatible event vocabulary | `babel`: Babel can normalize lifecycle; HSP has no Factory plugin bundle | `open`: possible only after its hooks are routed to `hsp hook` or through a gate-capable bridge | `WG-016` |
| Qwen Code | `qwen-code` | supported roster adapter | `session_id` | Claude-compatible event vocabulary | `babel`: Babel can normalize lifecycle; HSP has no Qwen plugin bundle | `open`: possible only after its hooks are routed to `hsp hook` or through a gate-capable bridge | `WG-016` |
| Kimi CLI | `kimi` | supported roster adapter | `session_id` | Claude-compatible events, TOML install surface | `babel`: Babel can normalize lifecycle; HSP has no Kimi plugin bundle | `open`: possible only after TOML hook config runs `hsp hook` | `WG-016` |
| Gemini CLI | `gemini` | supported roster adapter | `session_id`, `GEMINI_SESSION_ID` | name-mapped `BeforeTool`, `AfterTool`, `BeforeAgent`, `Stop`, `PreCompress` | `babel`: Babel can normalize lifecycle; HSP has no Gemini plugin bundle | `open`: needs mapped pre-tool/edit/build hooks before denial can work | `WG-016` |
| Crush | `crush` | supported partial adapter | `session_id`, `CRUSH_SESSION_ID` | partial: `PreToolUse` only | `babel`: can pulse tool activity only | `open`: insufficient stop/prompt/edit/build coverage for full ticket discipline | `WG-016` |
| Cursor Agent | `cursor` | supported roster adapter | `conversation_id` | Claude-compatible event vocabulary with identity-field adapter | `babel`: Babel can normalize lifecycle; HSP has no Cursor plugin bundle | `open`: possible only if Cursor exposes gate-capable pre-tool/edit hooks to a bridge | `WG-016` |
| Cline | `cline` | supported task adapter | `taskId`, `task_id` | `TaskStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `TaskComplete` | `babel`: task lifecycle can map to workgroup presence | `open`: needs edit/build deny semantics from the extension surface | `WG-016` |
| OpenCode | `opencode` | bridge required | bridge callback API | in-process callbacks, canonical after bridge | `bridge`: no HSP bridge implementation yet | `open`: bridge must support deny replies, not only observation | `WG-017` |
| Amp | `amp` | bridge required | bridge callback API | in-process callbacks, canonical after bridge | `bridge`: no HSP bridge implementation yet | `open`: bridge must support deny replies, not only observation | `WG-017` |
| Kiro | `kiro` | bridge required | form/API specific | hybrid IDE lifecycle after bridge | `bridge`: no HSP bridge implementation yet | `open`: bridge must support deny replies, not only observation | `WG-017` |
| GitHub Copilot CLI | `github-copilot-cli` | unsupported | none stable in documented hook payload | none usable | `blocked`: no stable workgroup identity | `blocked`: cannot enforce without stable session/task id | `WG-018` |
| Roo Code | `roo-code` | unsupported | none | no lifecycle hooks | `blocked`: manual MCP only if user adds it, no orchestration | `blocked`: no hook surface | `WG-018` |
| Kilo Code | `kilo-code` | unsupported | none | no lifecycle hooks | `blocked`: manual MCP only if user adds it, no orchestration | `blocked`: no hook surface | `WG-018` |
| Aider | `aider` | unsupported | none | no lifecycle hooks | `blocked`: manual shell/MCP only, no automatic workgroup presence | `blocked`: no hook surface | `WG-018` |
| Antigravity | `antigravity` | unsupported | none | rules/workflows exist, no lifecycle hooks | `blocked`: no automatic workgroup presence | `blocked`: no lifecycle hook surface | `WG-018` |

## Capability Axes

| Capability | Claude Code | Codex CLI | Babel supported adapters | Bridge-required adapters | Unsupported harnesses |
|------------|-------------|-----------|--------------------------|--------------------------|----------------------|
| Stable workgroup identity | `wired` through hook payload; stricter gates need shared `HSP_AGENT_ID` | `babel`; HSP plugin does not yet propagate hook identity | `babel`; depends on each adapter's identity fields | `bridge`; bridge must supply `session_id` | `blocked` |
| Presence heartbeat | `wired` through hooks and MCP calls | `manual` in HSP, `babel` if Codex hook snippet is installed | `babel` | `bridge` | `blocked` |
| Prompt and stop events | `wired`; `.end` maps to `session.stop` in `hsp hook` | `babel`, not HSP-native yet | `babel` where event surface is complete | `bridge` | `blocked` |
| Generic tool before/after | `wired` through `PreToolUse`/`PostToolUse` | `babel`; HSP Codex plugin lacks hook carrier | `babel` where native event exists | `bridge` | `blocked` |
| File read/edit observation | `partial`; Claude hooks can see `Read`, `Edit`, `MultiEdit`, `Write` file paths, but HSP currently records edit hooks only | `open`; current HSP Codex plugin cannot observe `apply_patch` or native file reads/edits | `open`; needs per-harness read/edit tool mapping | `open`; bridge must name read/edit operations and files | `blocked` |
| File-scoped journal injection | `wired` for Claude `Read`, `Edit`, `MultiEdit`, and `Write` hook output; renders scoped tickets, open questions, and recent rows with agent labels | `open`; blocked until Codex has a hook/interception path | `open`; needs hook output channel plus scoped recent query | `open`; bridge must support pre-action context output | `blocked` |
| Edit before/after logging | `wired` for Claude `Edit`, `MultiEdit`, `Write` | `open`; Codex `apply_patch` is invisible to HSP today | `open`; needs per-harness edit tool mapping | `open`; bridge must name edit operations | `blocked` |
| Edit denial without ticket | `wired`, opt-in with `HSP_REQUIRE_TICKET_FOR_EDITS=1` | `open`; no `apply_patch`/edit denial path | `open`; only possible on gate-capable pre-tool hooks | `open`; bridge must support deny response | `blocked` |
| Build gate | `wired` for detected Bash commands plus `hsp run` | `manual` through `hsp run`; no automatic shell hook | `open`; needs shell/tool command mapping | `open`; bridge or wrapper required | `manual` through `hsp run` only |
| Tool-output traffic injection | `partial`; HSP tool results render bus context, normal hooks are quiet | `partial`; MCP output only | `open`; needs frontier-based hook digest rendering | `open` | `manual` |
| Tickets | `wired` via MCP/CLI; hooks observe the work | `manual` via MCP/CLI | `manual` until adapters call HSP/Babel ticket APIs | `manual` until bridge exists | `manual` only if MCP is available |
| Journal / ask / chat | `wired` via MCP/CLI | `manual` via MCP/CLI | `manual` until exposed through adapter guidance | `manual` until bridge exists | `manual` only if MCP is available |
| Babel bridge observation | `partial`; independent from HSP Claude plugin | `partial`; independent from HSP Codex plugin | `babel` | `bridge` | `blocked` |
| HSP enforcement | `wired` for Claude-only hooks | `open` | `open` | `open` | `blocked` |

## Current Workgroup Detection

HSP now discovers a workgroup stack by walking upward for `workgroup.toml` or
`.hsp/workgroup.toml`. The deepest marker is the active workgroup; parent
markers remain visible as escalation context. If no marker exists, the process
cwd or `$LSP_ROOT` becomes an ephemeral single-session workgroup.

The active workgroup owns presence, journal, tickets, ask/chat, and the bus log.
Build and checker gates use the same workgroup for social context, but narrow
their mutex key to the detected project/check scope. Project roots are detected
separately from language/build markers such as:

| Route | Markers |
|-------|---------|
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `.git` |
| C# | `*.sln`, `*.csproj`, `Directory.Build.props`, `global.json`, `.git` |
| Rust | `Cargo.toml`, `rust-project.json`, `.git` |

`hsp` prints both layers so agents can see the current policy:

```text
workgroup_stack:
  parent umbrella umbrella: /workspace
  active domain domain: /workspace/domain
project: /workspace/domain/app
gate policy: build=project checker=file/project journal=workgroup
```

## Ticket Register

| Ticket | Priority | Status | Description |
|--------|----------|--------|-------------|
| `WG-001` | high | wired | Explicit workgroup discovery (`workgroup.toml` / `.hsp/workgroup.toml`) and visible hierarchy in `hsp workgroup`; remaining work is richer config policy. |
| `WG-002` | high | open | Define a Codex `apply_patch` interception strategy. Current HSP cannot deny Codex edits because no repo hook path observes that tool. |
| `WG-003` | high | open | Add an HSP Codex hook/plugin adapter if the harness supports shell/tool hooks; map shell commands to `hsp hook` and document unsupported events. |
| `WG-004` | high | open | Make tool-output traffic injection explicit: one compact workgroup header/digest per HSP tool result, with a frontier to avoid repeated journal spam. |
| `WG-005` | medium | open | Improve file/symbol scope extraction from hook payloads and command strings; use LSP identities when possible for edit/result rows. |
| `WG-006` | high | open | Define stable agent identity propagation across MCP server process, shell hooks, Babel panes, and subagents; document required env (`HSP_AGENT_ID`). |
| `WG-007` | medium | open | Add `hsp.capabilities()` / `hsp log capabilities` rendering this matrix from code/config so agents can query live policy. |
| `WG-008` | medium | open | Make build command detection configurable instead of hard-coded first-token/subcommand lists. |
| `WG-009` | medium | open | Add a trial playbook that records expected events for two agents: ticket start, denied edit, allowed edit, build wait, ask/chat, release, build result. |
| `WG-010` | low | open | Keep this matrix mechanically aligned with Babel's `AgentKind::ALL`, including new harnesses and support-tier changes. |
| `WG-011` | high | open | Wire durable JSONL replay into the broker-owned live `AgentBus`, or delete the dead parallel `BusJournal` path and make durability single-source. |
| `WG-012` | medium | open | Collapse duplicate workspace-id hashing into one helper shared by `AgentBus`, `BusRegistry`, docs, and tests. |
| `WG-013` | medium | open | Emit `confirm.before/after`, test, commit, and push stops from HSP internals/wrappers where possible; today several are taxonomy-only. |
| `WG-014` | low | open | Add direct unit coverage for `hsp.redirect_hook.main` denial JSON and keep README/plugin docs aligned with the opt-in denial policy. |
| `WG-015` | medium | open | Bring Codex plugin manifests to parity with current HSP version/routes and document which hook capabilities remain absent; the root Codex manifest is MCP/interface-only while the bundled plugin has Rust routes. |
| `WG-016` | high | open | Build a Babel-to-HSP adapter layer for Babel-supported non-Claude harnesses, starting with normalized lifecycle events and then per-harness edit/build gates. |
| `WG-017` | medium | open | Define bridge callback contracts for OpenCode, Amp, and Kiro that can return deny/allow decisions, not just observation events. |
| `WG-018` | low | blocked | Track unsupported harnesses until they expose stable identity plus lifecycle hooks; do not add cwd/time heuristics as orchestration substitutes. |
| `WG-019` | high | partial | Add file-scoped context injection for read/edit hooks: Claude now queries `bus.recent(files=...)` and renders compact agent-annotated tickets, open asks, and recent rows before/after scoped read/edit hook stops. Remaining work: persistent repeat suppression per `agent_id + file + last_event_id` and non-Claude hook carriers. |

## Trial Profiles

Recommended first hard-policy trial:

```text
HSP_REQUIRE_TICKET_FOR_EDITS=1
HSP_BUILD_GATE_TIMEOUT=2m
```

Recommended stricter identity trial, only after hook and MCP processes share
the same id:

```text
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
