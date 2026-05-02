"""Pure tests for the on-disk bus log.

Pinned contracts (see ``docs/agent-bus.md``):

- ``append`` then ``replay`` returns the same events, byte-equal in their
  ``to_wire`` projection.
- ``next_seq`` reads the highest seq from disk + 1, so a fresh process
  resumes the sequence without hand-off.
- ``tail(after_seq)`` filters by seq cursor.
- Malformed lines (truncated writes, future-schema records) are silently
  skipped — replay must never crash on a partial tail.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hsp.bus_event import BusEvent, BusEventKind, BusScope
from hsp.bus_log import BusLog


def _event(seq: int, *, message: str = "x") -> BusEvent:
    return BusEvent(
        seq=seq,
        event_id=f"E{seq}",
        kind=BusEventKind.NOTE_POSTED,
        timestamp=1000.0 + seq,
        workspace_id="wsid",
        workspace_root="/repo",
        agent_id="noesis",
        client_id=f"cli-{seq}",
        session_id="sess",
        task_id="",
        git_head="",
        dirty_hash="",
        scope=BusScope(files=(f"src/{seq}.py",)),
        message=message,
        metadata={},
    )


class BusLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "events.jsonl"

    def test_empty_log_replay_is_empty(self) -> None:
        log = BusLog(self.path)
        self.assertEqual(log.replay(), [])
        self.assertEqual(log.next_seq(), 1)

    def test_append_then_replay_round_trips(self) -> None:
        log = BusLog(self.path)
        first = _event(1, message="first")
        second = _event(2, message="second")
        log.append(first)
        log.append(second)

        replayed = log.replay()
        self.assertEqual(len(replayed), 2)
        self.assertEqual(replayed[0], first)
        self.assertEqual(replayed[1], second)

    def test_next_seq_reads_disk_state(self) -> None:
        # A fresh BusLog instance must agree with one that did the writes —
        # this is the resume path for broker restarts.
        writer = BusLog(self.path)
        writer.append(_event(1))
        writer.append(_event(7))  # gap is intentional; seq is just an id
        reader = BusLog(self.path)
        self.assertEqual(reader.next_seq(), 8)

    def test_tail_filters_by_after_seq(self) -> None:
        log = BusLog(self.path)
        for seq in (1, 2, 3, 4):
            log.append(_event(seq))
        tail = log.tail(after_seq=2)
        self.assertEqual([e.seq for e in tail], [3, 4])

    def test_replay_skips_malformed_lines(self) -> None:
        log = BusLog(self.path)
        log.append(_event(1))
        # Append junk: a truncated JSON line and an empty line.
        with self.path.open("a", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write("\n")
            f.write(json.dumps({"kind": "not.a.kind", "seq": 99}) + "\n")
        log.append(_event(2))
        replayed = log.replay()
        self.assertEqual([e.seq for e in replayed], [1, 2])


if __name__ == "__main__":
    unittest.main()
