"""Decaying presence tracker for the agent bus.

Presence is a derived view over the event log: every event from a client
counts as a heartbeat. The tracker remembers, per-client:

- ``first_seen_at`` / ``last_seen_at`` to compute the active/asleep/pruned
  status at a given ``now``,
- ``last_prompt_at`` and ``prompt_count`` from ``user.prompt`` events,
- ``last_event_id`` so weather can show "what was this client doing last?",
- ``pinned`` — flipped on once ``prompt_count >= 2`` (a second prompt is
  the signal that this client is still steering work, not a one-shot
  fire-and-forget).

Status thresholds match the doc: under 60 s = active, 60 s and up = asleep,
600 s and up = pruned (hidden from weather unless pinned). The tracker
never deletes entries — the log is the source of truth and prune is purely
a display concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from hsp.bus_event import BusEvent, BusEventKind


ACTIVE_WINDOW_SECONDS: Final[float] = 60.0
PRUNE_WINDOW_SECONDS: Final[float] = 600.0
PIN_PROMPT_THRESHOLD: Final[int] = 2


class PresenceStatus(Enum):
    ACTIVE = "active"
    ASLEEP = "asleep"
    PRUNED = "pruned"


@dataclass
class PresenceEntry:
    """One client/agent's last-known state.

    The entry is keyed by ``client_id`` (falling back to ``agent_id`` and
    then ``session_id`` if the client identity is missing). ``status`` is
    a derived field — :meth:`PresenceTracker.snapshot` recomputes it
    against the requested ``now``; storing it on the entry is just so
    callers can read both fields off the same record.
    """

    agent_id: str
    client_id: str
    session_id: str
    first_seen_at: float
    last_seen_at: float
    last_prompt_at: float = 0.0
    prompt_count: int = 0
    last_event_id: str = ""
    pinned: bool = False
    status: PresenceStatus = PresenceStatus.ACTIVE


@dataclass
class PresenceTracker:
    """Map of presence keys to their last-known entry.

    Insertion order is preserved (Python ``dict`` ordering): callers that
    iterate ``snapshot()`` get oldest-first, which is the natural shape for
    weather summaries.
    """

    _entries: dict[str, PresenceEntry] = field(default_factory=dict)

    def observe(self, event: BusEvent) -> PresenceEntry:
        """Update presence from one event and return the (mutated) entry.

        Called from the journal's append path so every durable event acts
        as a heartbeat, not just explicit ``agent.started`` pings.
        """
        key = self._key_for(event)
        entry = self._entries.get(key)
        if entry is None:
            entry = PresenceEntry(
                agent_id=event.agent_id,
                client_id=event.client_id,
                session_id=event.session_id,
                first_seen_at=event.timestamp,
                last_seen_at=event.timestamp,
            )
            self._entries[key] = entry
        else:
            if event.timestamp >= entry.last_seen_at:
                entry.last_seen_at = event.timestamp
            if event.agent_id and not entry.agent_id:
                entry.agent_id = event.agent_id
            if event.client_id and not entry.client_id:
                entry.client_id = event.client_id
            if event.session_id and not entry.session_id:
                entry.session_id = event.session_id
        entry.last_event_id = event.event_id
        if event.kind is BusEventKind.USER_PROMPT:
            entry.last_prompt_at = event.timestamp
            entry.prompt_count += 1
            if entry.prompt_count >= PIN_PROMPT_THRESHOLD:
                entry.pinned = True
        return entry

    def status_at(self, entry: PresenceEntry, now: float) -> PresenceStatus:
        """Compute the presence status for ``entry`` at ``now``.

        Pinned entries never collapse into ``PRUNED`` — they keep their
        idle status (``ACTIVE`` / ``ASLEEP``) so a steady-state user agent
        stays visible after a long silence.
        """
        elapsed = max(0.0, now - entry.last_seen_at)
        if elapsed >= PRUNE_WINDOW_SECONDS and not entry.pinned:
            return PresenceStatus.PRUNED
        if elapsed >= ACTIVE_WINDOW_SECONDS:
            return PresenceStatus.ASLEEP
        return PresenceStatus.ACTIVE

    def snapshot(self, now: float) -> list[PresenceEntry]:
        """Recompute statuses against ``now`` and return every entry."""
        out: list[PresenceEntry] = []
        for entry in self._entries.values():
            entry.status = self.status_at(entry, now)
            out.append(entry)
        return out

    def visible(self, now: float) -> list[PresenceEntry]:
        """Snapshot but drop pruned, non-pinned entries.

        This is what weather digests show to agents — pruned-and-not-pinned
        clients are noise, not signal.
        """
        return [
            entry for entry in self.snapshot(now)
            if entry.status is not PresenceStatus.PRUNED or entry.pinned
        ]

    def _key_for(self, event: BusEvent) -> str:
        return event.client_id or event.agent_id or event.session_id


__all__ = [
    "ACTIVE_WINDOW_SECONDS",
    "PIN_PROMPT_THRESHOLD",
    "PRUNE_WINDOW_SECONDS",
    "PresenceEntry",
    "PresenceStatus",
    "PresenceTracker",
]
