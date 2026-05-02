"""Multi-slot pending preview store.

A *pending* is the staged form of a ``WorkspaceEdit``: an LSP-side preview has
rendered, the candidate edit list is queued, but disk has not changed yet.
``lsp_confirm`` is the commit operator that turns one staged candidate into
real edits.

Direct mode used to keep a single module-level ``_pending`` slot, which meant
any new preview displaced the last one — fine for a single agent driving
``stage → confirm`` linearly, but lossy for parallel agents that may stage
multiple unrelated previews before any of them is confirmed (see
``docs/agent-tool-roadmap.md``: "Pending Edits", and ``docs/broker.md``:
"Staged Edits And Prediction").

This module holds the direct-mode answer to that gap:

- ``PendingBuffer`` is one staged preview. It now carries a stable ``handle``
  (the stage name); ``DEFAULT_STAGE_HANDLE`` is reserved for legacy callers
  that don't pick a name.
- ``PendingBook`` is the multi-slot store keyed by handle. The most recently
  ``set()`` stage is the *active* stage; ``lsp_confirm(0)`` resolves against
  it so single-agent flows keep working without any code change. Named stages
  (handle != ``DEFAULT_STAGE_HANDLE``) coexist with each other and with the
  default slot, and replacing one stage never disturbs the metadata or
  candidates of any other stage.

The book is intentionally minimal: it is a process-local, in-memory map. The
broker design (``docs/broker.md``: "Staged Edits And Prediction") will later
own the same shape behind ``WorkspaceSession`` so per-client snapshots, lease
ownership, and conflict prediction can layer on top without changing the
public ``handle`` grammar.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from hsp.candidate import Candidate

# Reserved handle for the unnamed slot. Legacy callers (`lsp_rename`,
# `lsp_move`, `lsp_fix`) stage here so `lsp_confirm(0)` keeps committing the
# latest preview exactly the way it did before multi-slot support landed.
DEFAULT_STAGE_HANDLE = "default"


@dataclass
class PendingBuffer:
    """One staged preview transaction.

    ``kind`` is the display kind shown in the confirm transcript
    (``"fix"``, ``"symbol_rename"``, ``"file_move"``, ``"file_move_batch"``,
    etc.). ``candidates`` is the list of confirmable choices the agent picks
    from with ``lsp_confirm(index)``. ``description`` is the one-line preview
    summary already rendered to the agent.

    ``handle`` is the multi-slot key the stage lives under in a
    ``PendingBook``; ``DEFAULT_STAGE_HANDLE`` means the unnamed legacy slot.
    """
    kind: str
    candidates: list[Candidate]
    description: str
    handle: str = DEFAULT_STAGE_HANDLE


@dataclass
class PendingBook:
    """Ordered map of pending previews keyed by stage handle.

    Insertion order is the activation order: the last handle in ``_order`` is
    the *active* stage and is what ``lsp_confirm(0)`` (no ``stage`` arg)
    operates on. Setting an existing handle replaces that stage in place and
    bumps it to active without disturbing other entries.

    The book is the canonical store. Callers should not assume the active
    stage equals any specific handle — they ask via :meth:`active` /
    :meth:`active_handle`.
    """
    _stages: dict[str, PendingBuffer] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    @staticmethod
    def _normalize(handle: str) -> str:
        # Empty string from a legacy caller maps to the default slot. This
        # keeps `_set_pending(...)` (no handle) and `_set_pending(..., handle="")`
        # equivalent to the pre-multi-slot behavior.
        return handle or DEFAULT_STAGE_HANDLE

    def set(self, buffer: PendingBuffer) -> PendingBuffer:
        """Stage ``buffer`` under its ``handle`` and mark it active.

        Replacing an existing handle preserves the metadata of every other
        stage; only the targeted entry is rewritten. The replaced stage's
        candidates and description are dropped — that is the intended
        single-slot-per-handle semantics, not corruption of unrelated stages.
        """
        handle = self._normalize(buffer.handle)
        buffer.handle = handle
        if handle in self._stages:
            # Promote: drop the old position, append at the tail so it becomes
            # the active stage again.
            self._order.remove(handle)
        self._stages[handle] = buffer
        self._order.append(handle)
        return buffer

    def get(self, handle: str) -> PendingBuffer | None:
        return self._stages.get(self._normalize(handle))

    def active(self) -> PendingBuffer | None:
        if not self._order:
            return None
        return self._stages[self._order[-1]]

    def active_handle(self) -> str | None:
        return self._order[-1] if self._order else None

    def drop(self, handle: str) -> PendingBuffer | None:
        """Remove and return the stage at ``handle``; ``None`` if absent."""
        h = self._normalize(handle)
        existing = self._stages.pop(h, None)
        if existing is not None and h in self._order:
            self._order.remove(h)
        return existing

    def clear_active(self) -> PendingBuffer | None:
        """Remove and return the currently active stage, if any."""
        if not self._order:
            return None
        return self.drop(self._order[-1])

    def clear_all(self) -> None:
        self._stages.clear()
        self._order.clear()

    def handles(self) -> list[str]:
        """Stage handles in activation order; last entry is the active one."""
        return list(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __bool__(self) -> bool:
        return bool(self._order)

    def __contains__(self, handle: object) -> bool:
        if not isinstance(handle, str):
            return False
        return self._normalize(handle) in self._stages
