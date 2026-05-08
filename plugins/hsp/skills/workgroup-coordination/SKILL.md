---
name: workgroup-coordination
description: "Use when coordinating multiple agents in an HSP workgroup: reading journal context, asking or answering questions, watching hook traffic, interpreting injected context, or deciding whether to wait, chat, or proceed."
---

# Workgroup Coordination

Use the bus as the shared situational layer:

- `hsp.journal()` / `hsp log journal`: full local workgroup picture.
- `hsp log recent --files path`: scoped activity for a file or symbol.
- `hsp watch [location]`: live hook/tool traffic for an exact, subtree, or configured network scope.
- `hsp global`: broker-level LSP/session/status view.

Questions:

- Ask only when another busy agent could plausibly reply.
- If nobody is busy, `ask` returns immediately with a notice instead of waiting.
- Reply with `hsp.chat(..., id="Qn")` or `hsp log chat --id Qn --message "..."`.

Context injection:

- Claude read/edit hooks inject scoped tickets, open questions, and recent rows when files/symbols are known.
- Rows are timestamped and annotated with `@agent`.
- `HSP_HOOK_CONTEXT=0` disables injected context while keeping hooks recording.

Default coordination loop:

1. Check journal/weather.
2. Hold or join the right ticket.
3. Read hook-injected context on file access.
4. Ask/chat only when it reduces collision or uncertainty.
5. Release the ticket and leave a note if the next agent needs the trail.
