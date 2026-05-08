---
name: work-ticket
description: Use whenever starting, changing, holding, or releasing an HSP work ticket, or when edits/builds/checks need to coordinate with active work in a workgroup.
---

# Work Ticket

A ticket is the workgroup signal that an agent is actively changing something.
It is not a file lock; it is the build/check/edit coordination handle.

Use one ticket per active task:

```text
hsp.ticket("extract terminal backend trait")
hsp.ticket("")
```

CLI equivalent:

```bash
hsp log ticket --message "extract terminal backend trait" --files src/backend/mod.rs
hsp log ticket --message ""
```

Rules:

- Start a ticket before meaningful edits.
- Keep the message short and action-shaped.
- Add `files`/`symbols` scope once known so context injection and checker gates can be precise.
- Release the ticket when you stop work, hand off, or finish.
- If `HSP_REQUIRE_TICKET_FOR_EDITS=1`, Claude edit hooks can deny edits until a ticket is active.
- Build/check hooks wait on active tickets; if all relevant agents are waiting at the gate, HSP can batch the build once.

When switching tasks, release the old ticket before opening the new one unless both agents intentionally share the same ticket message.
