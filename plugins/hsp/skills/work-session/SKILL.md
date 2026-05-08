---
name: work-session
description: Use at the start of every HSP-enabled coding, debugging, refactoring, review, research, or repository work session to establish workgroup awareness, ticket discipline, journal checks, and verification flow.
---

# Work Session

Start every non-trivial repo task by orienting to the workgroup:

- Run or query `hsp`/`hsp workgroup` when the current workgroup, project root, broker, or observation scope is unclear.
- Check `hsp.journal()` or `hsp log journal` when other agents may be active, especially before touching shared files.
- Use the HSP `lsp_*` tools for semantic navigation in supported Python, C#, and Rust projects.

Hold a ticket while doing work:

- Start: `hsp.ticket("short task intent")` or `hsp log ticket --message "short task intent"`.
- Scope it with `files=`/`--files` as soon as the touched area is known.
- Release: `hsp.ticket("")` or `hsp log ticket --message ""`.

Coordinate instead of guessing:

- Use `hsp.ask(...)` for blocking questions that another busy agent could answer.
- Use `hsp.chat(...)` or `hsp log chat` for status, replies, and non-blocking coordination.
- Trust hook-injected context on reads/edits; query `hsp log recent --files ...` when you need more.

Build and verify through the gate:

- In Claude Code, normal detected build/check commands are hook-gated automatically.
- In harnesses without shell hooks, use `hsp run -- <command>` for build/check/test commands.

Before finishing, release the ticket, record any useful note, and report verification performed.
