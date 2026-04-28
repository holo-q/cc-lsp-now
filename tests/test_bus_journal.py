"""Pure tests for ``BusJournal``, ``PresenceTracker``, and ``BusRegistry``.

Pinned contracts (see ``docs/agent-bus.md``):

- ``ask`` opens a question with a deterministic ``question_id`` and the
  opener event carries ``BusEventKind.BUS_ASK``.
- ``reply`` attaches a durable ``BusEventKind.BUS_REPLY`` event tied to
  the ``question_id``.
- ``settle`` lazily emits ``BusEventKind.BUS_CLOSED`` for every expired
  question, and the digest carries reply / related counts.
- A reply that arrives after close is appended as ``BusEventKind.BUS_REPLY``
  with ``metadata['late'] == 'true'`` and does NOT mutate the closed
  digest event.
- ``recent`` filters by scope / kinds / limit and skips own events when a
  client/agent identity is supplied.
- ``PresenceTracker`` resolves active < 60 s, asleep ≥ 60 s, pruned ≥
  600 s; pinned entries (≥ 2 user prompts) survive prune.
- ``workspace_id_for`` is deterministic from the absolute path; the
  registry returns one journal per workspace directory.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cc_lsp_now.bus_event import BusEvent, BusEventKind, BusScope
from cc_lsp_now.bus_journal import BusJournal
from cc_lsp_now.bus_log import BusLog
from cc_lsp_now.bus_presence import (
    ACTIVE_WINDOW_SECONDS,
    PRUNE_WINDOW_SECONDS,
    PresenceStatus,
    PresenceTracker,
)
from cc_lsp_now.bus_registry import (
    BUS_DIR_ENV,
    BrokerMode,
    BusRegistry,
    bus_dir_for,
    workspace_id_for,
)


class _Clock:
    """Deterministic clock so tests don't race the wall clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _journal(tmp: Path, clock: _Clock) -> BusJournal:
    log = BusLog(tmp / "events.jsonl")
    return BusJournal.open(
        log,
        workspace_id="wsid",
        workspace_root=str(tmp),
        now_fn=clock,
    )


class BusJournalQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = _Clock()

    def test_ask_opens_a_durable_question(self) -> None:
        journal = _journal(self.tmp, self.clock)
        opener, record = journal.ask(
            "split lsp_refs?",
            agent_id="noesis",
            client_id="cli-noesis",
            scope=BusScope.parse(files="src/server.py"),
            timeout_seconds=180.0,
        )
        self.assertEqual(opener.kind, BusEventKind.BUS_ASK)
        self.assertEqual(opener.question_id, record.question_id)
        self.assertEqual(record.timeout_at, opener.timestamp + 180.0)
        self.assertEqual(record.replies, [])
        self.assertFalse(record.closed)

    def test_reply_attaches_to_question(self) -> None:
        journal = _journal(self.tmp, self.clock)
        opener, record = journal.ask(
            "split?",
            agent_id="noesis",
            client_id="cli-noesis",
            scope=BusScope.parse(files="src/server.py"),
            timeout_seconds=180.0,
        )
        self.clock.advance(10.0)
        reply = journal.reply(
            opener.question_id,
            "go ahead",
            agent_id="reverie",
            client_id="cli-reverie",
        )
        self.assertEqual(reply.kind, BusEventKind.BUS_REPLY)
        self.assertEqual(reply.question_id, opener.question_id)
        self.assertEqual(record.replies, [reply])

    def test_settle_emits_bus_closed_for_expired_question(self) -> None:
        journal = _journal(self.tmp, self.clock)
        opener, record = journal.ask(
            "split?",
            agent_id="noesis",
            client_id="cli-noesis",
            scope=BusScope.parse(files="src/server.py"),
            timeout_seconds=60.0,
        )
        # An overlapping note arrives during the open window.
        self.clock.advance(5.0)
        journal.note(
            "touched server.py",
            agent_id="amanuensis",
            client_id="cli-aman",
            scope=BusScope.parse(files="src/server.py"),
        )
        self.clock.advance(120.0)

        closed = journal.settle()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].kind, BusEventKind.BUS_CLOSED)
        self.assertEqual(closed[0].question_id, opener.question_id)
        self.assertTrue(record.closed)
        self.assertEqual(closed[0].metadata.get("reply_count"), "0")
        # The overlapping note was attached as related during the window.
        self.assertEqual(closed[0].metadata.get("related_count"), "1")

    def test_reply_after_close_does_not_corrupt_digest(self) -> None:
        journal = _journal(self.tmp, self.clock)
        opener, record = journal.ask(
            "split?",
            agent_id="noesis",
            client_id="cli-noesis",
            scope=BusScope.parse(files="src/server.py"),
            timeout_seconds=30.0,
        )
        self.clock.advance(60.0)
        closed_events = journal.settle()
        self.assertEqual(len(closed_events), 1)
        digest = closed_events[0]
        digest_message_before = digest.message
        digest_meta_before = dict(digest.metadata)

        # A late reply lands after the digest has been emitted.
        late = journal.reply(
            opener.question_id,
            "too late",
            agent_id="reverie",
            client_id="cli-reverie",
        )
        self.assertEqual(late.kind, BusEventKind.BUS_REPLY)
        self.assertEqual(late.metadata.get("late"), "true")

        # The closed digest event itself is untouched (BusEvent is frozen).
        self.assertEqual(digest.message, digest_message_before)
        self.assertEqual(dict(digest.metadata), digest_meta_before)
        # The journal still reports the question as closed.
        self.assertTrue(record.closed)


class BusJournalRecentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = _Clock()

    def test_recent_filters_by_scope(self) -> None:
        journal = _journal(self.tmp, self.clock)
        journal.note(
            "server",
            agent_id="a",
            client_id="cli-a",
            scope=BusScope.parse(files="src/server.py"),
        )
        journal.note(
            "other",
            agent_id="a",
            client_id="cli-a",
            scope=BusScope.parse(files="src/other.py"),
        )
        hits = journal.recent(
            scope=BusScope.parse(files="src/server.py"),
            kinds=[BusEventKind.NOTE_POSTED],
            skip_own=False,
        )
        self.assertEqual([e.message for e in hits], ["server"])

    def test_recent_skips_own_events_by_default(self) -> None:
        journal = _journal(self.tmp, self.clock)
        journal.note(
            "from-self",
            agent_id="me",
            client_id="cli-me",
            scope=BusScope.parse(files="src/server.py"),
        )
        journal.note(
            "from-other",
            agent_id="other",
            client_id="cli-other",
            scope=BusScope.parse(files="src/server.py"),
        )
        hits = journal.recent(
            scope=BusScope.parse(files="src/server.py"),
            kinds=[BusEventKind.NOTE_POSTED],
            client_id="cli-me",
        )
        self.assertEqual([e.message for e in hits], ["from-other"])

    def test_recent_respects_limit(self) -> None:
        journal = _journal(self.tmp, self.clock)
        for i in range(5):
            journal.note(
                f"n{i}",
                agent_id="a",
                client_id="cli-a",
                scope=BusScope.parse(files="src/server.py"),
            )
        hits = journal.recent(
            kinds=[BusEventKind.NOTE_POSTED],
            limit=2,
            skip_own=False,
        )
        # Most-recent window keeps the tail of the buffer.
        self.assertEqual([e.message for e in hits], ["n3", "n4"])


class PresenceTrackerTests(unittest.TestCase):
    def _ev(
        self,
        kind: BusEventKind,
        *,
        client_id: str = "cli-1",
        agent_id: str = "noesis",
        timestamp: float = 1000.0,
        seq: int = 1,
    ) -> BusEvent:
        return BusEvent(
            seq=seq,
            event_id=f"E{seq}",
            kind=kind,
            timestamp=timestamp,
            workspace_id="wsid",
            workspace_root="/repo",
            agent_id=agent_id,
            client_id=client_id,
            session_id="sess",
            task_id="",
            git_head="",
            dirty_hash="",
            scope=BusScope(),
            message="",
            metadata={},
        )

    def test_active_then_asleep_then_pruned(self) -> None:
        tracker = PresenceTracker()
        entry = tracker.observe(self._ev(BusEventKind.AGENT_STARTED, timestamp=1000.0))
        self.assertEqual(
            tracker.status_at(entry, 1000.0 + ACTIVE_WINDOW_SECONDS - 1),
            PresenceStatus.ACTIVE,
        )
        self.assertEqual(
            tracker.status_at(entry, 1000.0 + ACTIVE_WINDOW_SECONDS),
            PresenceStatus.ASLEEP,
        )
        self.assertEqual(
            tracker.status_at(entry, 1000.0 + PRUNE_WINDOW_SECONDS),
            PresenceStatus.PRUNED,
        )

    def test_pin_after_two_user_prompts_survives_prune(self) -> None:
        tracker = PresenceTracker()
        tracker.observe(self._ev(BusEventKind.USER_PROMPT, timestamp=1000.0, seq=1))
        entry = tracker.observe(
            self._ev(BusEventKind.USER_PROMPT, timestamp=1010.0, seq=2)
        )
        self.assertTrue(entry.pinned)
        # Long after the prune window the entry is still visible because pinned.
        far_future = 1000.0 + PRUNE_WINDOW_SECONDS + 1000.0
        self.assertEqual(
            tracker.status_at(entry, far_future),
            PresenceStatus.ASLEEP,
        )
        visible = tracker.visible(far_future)
        self.assertEqual(len(visible), 1)
        self.assertTrue(visible[0].pinned)

    def test_visible_drops_unpinned_pruned(self) -> None:
        tracker = PresenceTracker()
        tracker.observe(self._ev(BusEventKind.AGENT_STARTED, timestamp=1000.0))
        far = 1000.0 + PRUNE_WINDOW_SECONDS + 1.0
        self.assertEqual(tracker.visible(far), [])


class BusRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._prior_env = os.environ.get(BUS_DIR_ENV)
        # Pin the storage directory so the test never touches XDG_STATE_HOME.
        os.environ[BUS_DIR_ENV] = str(self.root / "bus")

    def tearDown(self) -> None:
        if self._prior_env is None:
            os.environ.pop(BUS_DIR_ENV, None)
        else:
            os.environ[BUS_DIR_ENV] = self._prior_env

    def test_workspace_id_is_deterministic(self) -> None:
        a = workspace_id_for(self.root)
        b = workspace_id_for(self.root)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_workspace_id_differs_per_root(self) -> None:
        other = self.root.parent / "other"
        self.assertNotEqual(workspace_id_for(self.root), workspace_id_for(other))

    def test_bus_dir_for_uses_env_override(self) -> None:
        path = bus_dir_for(self.root, BrokerMode.BROKER)
        self.assertTrue(str(path).startswith(str(self.root / "bus")))

    def test_get_or_open_returns_same_journal_per_root(self) -> None:
        registry = BusRegistry()
        first = registry.get_or_open(self.root, BrokerMode.BROKER)
        second = registry.get_or_open(self.root, BrokerMode.BROKER)
        self.assertIs(first, second)
        self.assertEqual(first.workspace_id, workspace_id_for(self.root))

    def test_get_or_open_different_roots_get_distinct_journals(self) -> None:
        other = self.root.parent / (self.root.name + "_alt")
        other.mkdir()
        registry = BusRegistry()
        a = registry.get_or_open(self.root, BrokerMode.BROKER)
        b = registry.get_or_open(other, BrokerMode.BROKER)
        self.assertIsNot(a, b)
        self.assertNotEqual(a.workspace_id, b.workspace_id)


if __name__ == "__main__":
    unittest.main()
