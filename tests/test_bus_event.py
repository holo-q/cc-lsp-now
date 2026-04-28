"""Pure tests for the bus event wire shape.

Pinned contracts (see ``docs/agent-bus.md``):

- ``BusEventKind`` round-trips through its string wire form.
- ``BusScope.overlaps`` is a wildcard for empty scopes (workspace-wide
  notes hit every recipient) and an intersection check otherwise.
- ``BusScope.parse`` accepts comma- *and* newline-separated user input
  and drops empty pieces.
- ``BusEvent.to_wire``/``from_wire`` is value-preserving on a populated
  record and tolerant of unknown / missing fields.
- ``truncate_message`` clips at byte boundaries and reports the flag.
"""
from __future__ import annotations

import unittest

from cc_lsp_now.bus_event import (
    MAX_MESSAGE_BYTES,
    SCHEMA_VERSION,
    BusEvent,
    BusEventKind,
    BusScope,
    truncate_message,
)


def _event(
    *,
    seq: int = 7,
    event_id: str = "E7",
    kind: BusEventKind = BusEventKind.NOTE_POSTED,
    timestamp: float = 1700.5,
    workspace_id: str = "wsid000abcd",
    workspace_root: str = "/repo/foo",
    agent_id: str = "noesis",
    client_id: str = "cli-1",
    session_id: str = "sess-1",
    task_id: str = "T-42",
    git_head: str = "deadbee",
    dirty_hash: str = "abc123",
    scope: BusScope = BusScope(files=("src/server.py",), symbols=("Foo",)),
    message: str = "hello",
    metadata: dict[str, str] | None = None,
    question_id: str = "",
    truncated: bool = False,
) -> BusEvent:
    return BusEvent(
        seq=seq,
        event_id=event_id,
        kind=kind,
        timestamp=timestamp,
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        agent_id=agent_id,
        client_id=client_id,
        session_id=session_id,
        task_id=task_id,
        git_head=git_head,
        dirty_hash=dirty_hash,
        scope=scope,
        message=message,
        metadata=dict(metadata or {"k": "v"}),
        question_id=question_id,
        truncated=truncated,
    )


class BusEventKindTests(unittest.TestCase):
    def test_every_kind_round_trips(self) -> None:
        for kind in BusEventKind:
            self.assertIs(BusEventKind.from_wire(kind.value), kind)

    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            BusEventKind.from_wire("not.a.kind")


class BusScopeOverlapTests(unittest.TestCase):
    def test_empty_scope_is_wildcard(self) -> None:
        # An event with no scope (e.g. agent.started, user.prompt) should
        # be visible to any recipient scope, and any opener with no scope
        # should pull every event in.
        empty = BusScope()
        scoped = BusScope(files=("src/server.py",))
        self.assertTrue(empty.overlaps(scoped))
        self.assertTrue(scoped.overlaps(empty))
        self.assertTrue(empty.overlaps(empty))

    def test_files_overlap(self) -> None:
        a = BusScope(files=("src/server.py", "src/x.py"))
        b = BusScope(files=("src/server.py",))
        c = BusScope(files=("src/y.py",))
        self.assertTrue(a.overlaps(b))
        self.assertFalse(a.overlaps(c))

    def test_symbols_and_aliases_overlap_independently(self) -> None:
        a = BusScope(symbols=("Foo",))
        b = BusScope(symbols=("Bar",), aliases=("A3",))
        c = BusScope(aliases=("A3",))
        self.assertFalse(a.overlaps(b))
        self.assertTrue(b.overlaps(c))

    def test_is_empty(self) -> None:
        self.assertTrue(BusScope().is_empty())
        self.assertFalse(BusScope(files=("a",)).is_empty())
        self.assertFalse(BusScope(aliases=("A1",)).is_empty())

    def test_parse_accepts_comma_and_newline(self) -> None:
        scope = BusScope.parse(
            files="src/server.py, src/x.py",
            symbols="Foo\nBar\n",
            aliases="",
        )
        self.assertEqual(scope.files, ("src/server.py", "src/x.py"))
        self.assertEqual(scope.symbols, ("Foo", "Bar"))
        self.assertEqual(scope.aliases, ())

    def test_parse_drops_empty_pieces(self) -> None:
        scope = BusScope.parse(files=" , ,  ,")
        self.assertTrue(scope.is_empty())


class BusEventWireTests(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        original = _event(question_id="Q3", metadata={"reply_count": "2"})
        wire = original.to_wire()
        restored = BusEvent.from_wire(wire)
        self.assertEqual(restored, original)

    def test_wire_includes_schema_version(self) -> None:
        wire = _event().to_wire()
        self.assertEqual(wire["schema_version"], SCHEMA_VERSION)

    def test_from_wire_ignores_unknown_keys(self) -> None:
        wire = _event().to_wire()
        wire["future_field"] = {"hello": "world"}
        restored = BusEvent.from_wire(wire)
        self.assertEqual(restored.kind, BusEventKind.NOTE_POSTED)

    def test_from_wire_supplies_defaults_for_missing_fields(self) -> None:
        # Forward-rolled producer drops a field we still expect; replay
        # must succeed with sensible defaults rather than crashing.
        minimal: dict[str, object] = {"kind": BusEventKind.AGENT_STARTED.value}
        restored = BusEvent.from_wire(minimal)
        self.assertEqual(restored.kind, BusEventKind.AGENT_STARTED)
        self.assertEqual(restored.seq, 0)
        self.assertEqual(restored.message, "")
        self.assertEqual(restored.scope, BusScope())
        self.assertEqual(restored.metadata, {})

    def test_from_wire_rejects_payload_without_kind(self) -> None:
        with self.assertRaises(ValueError):
            BusEvent.from_wire({"seq": 1})


class TruncateMessageTests(unittest.TestCase):
    def test_short_message_is_unchanged(self) -> None:
        clipped, truncated = truncate_message("hi")
        self.assertEqual(clipped, "hi")
        self.assertFalse(truncated)

    def test_long_message_is_clipped_and_flagged(self) -> None:
        big = "a" * (MAX_MESSAGE_BYTES + 100)
        clipped, truncated = truncate_message(big)
        self.assertTrue(truncated)
        self.assertLessEqual(len(clipped.encode("utf-8")), MAX_MESSAGE_BYTES)

    def test_utf8_safe_truncation(self) -> None:
        # Snowman is 3 bytes in UTF-8; a tight limit must not split a
        # multibyte sequence in the middle.
        clipped, truncated = truncate_message("☃" * 5, limit=4)
        self.assertTrue(truncated)
        clipped.encode("utf-8")  # would raise on invalid


if __name__ == "__main__":
    unittest.main()
