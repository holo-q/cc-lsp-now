"""Append-only JSONL bus log.

The log is the durable home of every ``BusEvent``. One event per line, JSON
encoded with no whitespace, opened in append mode for each write. The shape
is intentionally simple:

- ``append(event)`` — write one line, fsync, close.
- ``replay()`` — read all events from disk in seq order.
- ``tail(after_seq)`` — replay tail past a known seq cursor.
- ``next_seq()`` — read the highest seq from disk + 1, so a fresh process
  resumes the sequence without hand-off.

This is intentionally Linux-friendly and naive about rotation: the bus is
short-lived weather, not an audit trail. If the file ever grows past tens
of MiB the operator should archive it; the journal will resume cleanly on
an empty file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from hsp.bus_event import BusEvent


class BusLog:
    """Disk-backed JSONL event store.

    A log instance is cheap and stateless beyond the path. Multiple
    journals or hooks can wrap the same path safely as long as appends are
    line-atomic — which they are at our event sizes (well under 4 KiB on
    Linux). The log does not coordinate seq assignment; the journal owns
    that and asks ``next_seq()`` once on construction.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: BusEvent) -> None:
        """Append one event as a single JSONL line and fsync."""
        line = json.dumps(event.to_wire(), separators=(",", ":"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                import os

                os.fsync(handle.fileno())
            except OSError:
                pass

    def replay(self) -> list[BusEvent]:
        """Read every event currently on disk, in seq/file order.

        Lines that are blank, malformed JSON, or missing required fields
        are skipped silently — replay must succeed in the presence of a
        forward-rolled schema. Higher layers can re-derive recent state
        from whatever survives.
        """
        if not self.path.exists():
            return []
        events: list[BusEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                try:
                    events.append(BusEvent.from_wire(cast(dict[str, object], payload)))
                except (ValueError, KeyError):
                    continue
        return events

    def tail(self, after_seq: int = 0) -> list[BusEvent]:
        """Return events with ``seq > after_seq``."""
        return [event for event in self.replay() if event.seq > after_seq]

    def next_seq(self) -> int:
        """Return ``max(seq) + 1`` over the on-disk log, or 1 for empty."""
        last = 0
        for event in self.replay():
            if event.seq > last:
                last = event.seq
        return last + 1


__all__ = ["BusLog"]
