"""Workspace-scoped agent-bus journal.

This is the public Wave-1 entry point for the bus. The journal owns:

- a :class:`BusLog` for durable JSONL events,
- a :class:`PresenceTracker` for per-client weather,
- the open-question table (timed asks plus their replies and digest),
- monotonic seq / event_id / question_id assignment.

It is intentionally pure data on top of disk: no sockets, no LSP, no
threads. The broker and MCP server compose the journal with their own
clocks and identity sources; the journal stamps events at append time.

Question lifecycle:

1. ``ask`` opens a window with ``timeout_seconds``. The opener event is a
   ``bus.ask`` durable event with ``question_id`` set.
2. ``reply`` attaches a ``bus.reply`` event to the same ``question_id``.
3. ``settle``/``weather``/``recent`` close any expired open question
   lazily — they emit one ``bus.closed`` durable event whose metadata
   carries the digest summary (counts of replies and related events).
4. A ``reply`` after a question closes is still recorded as a durable
   ``bus.reply`` event but does not change the closed digest. Tests
   document this contract explicitly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from hsp.bus_event import (
    BusEvent,
    BusEventKind,
    BusScope,
    truncate_message,
)
from hsp.bus_log import BusLog
from hsp.bus_presence import (
    PresenceEntry,
    PresenceStatus,
    PresenceTracker,
)


DEFAULT_QUESTION_TIMEOUT_SECONDS: Final[float] = 180.0
DEFAULT_RECENT_LIMIT: Final[int] = 25


@dataclass
class QuestionRecord:
    """One open or closed bus question.

    ``opener`` is the ``bus.ask`` event, ``replies`` are subsequent
    ``bus.reply`` events, and ``related`` is everything in scope that
    landed during the open window (used to compose the digest).
    """

    question_id: str
    opener: BusEvent
    timeout_at: float
    replies: list[BusEvent] = field(default_factory=list)
    related: list[BusEvent] = field(default_factory=list)
    closed: bool = False
    closed_event: BusEvent | None = None


@dataclass
class WeatherReport:
    """Compact workspace status for a new or resumed agent."""

    workspace_id: str
    workspace_root: str
    now: float
    last_seq: int
    presence: tuple[PresenceEntry, ...]
    open_questions: tuple[QuestionRecord, ...]
    closed_questions: tuple[QuestionRecord, ...]
    recent: tuple[BusEvent, ...]
    counts_by_status: dict[str, int]
    pinned_count: int


class BusJournal:
    """High-level bus state for one workspace.

    Construct with :meth:`open` so the in-memory state is rehydrated from
    the on-disk log. The class is single-process; broker-side use should
    funnel all writes through one instance per workspace.
    """

    def __init__(
        self,
        log: BusLog,
        *,
        workspace_id: str,
        workspace_root: str,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._log = log
        self._workspace_id = workspace_id
        self._workspace_root = workspace_root
        self._now_fn = now_fn
        self._presence = PresenceTracker()
        self._events: list[BusEvent] = []
        self._questions: dict[str, QuestionRecord] = {}
        self._next_seq = 1
        self._next_question_index = 1

    @classmethod
    def open(
        cls,
        log: BusLog,
        *,
        workspace_id: str,
        workspace_root: str,
        now_fn: Callable[[], float] = time.time,
    ) -> BusJournal:
        """Build a journal and rehydrate it from the on-disk log."""
        journal = cls(
            log,
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            now_fn=now_fn,
        )
        journal._rehydrate()
        return journal

    # --- properties ---------------------------------------------------------

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def workspace_root(self) -> str:
        return self._workspace_root

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    # --- public API ---------------------------------------------------------

    def append_event(
        self,
        kind: BusEventKind,
        *,
        agent_id: str = "",
        client_id: str = "",
        session_id: str = "",
        task_id: str = "",
        git_head: str = "",
        dirty_hash: str = "",
        scope: BusScope = BusScope(),
        message: str = "",
        metadata: dict[str, str] | None = None,
        question_id: str = "",
        timestamp: float | None = None,
    ) -> BusEvent:
        """Append a durable event and return the stamped record.

        This is the single write path: ``note``, ``ask``, ``reply``, and
        ``settle`` all funnel through here so seq/event_id/timestamp
        assignment is consistent and presence is observed exactly once
        per event.
        """
        ts = self._now_fn() if timestamp is None else timestamp
        clipped, truncated = truncate_message(message)
        event = BusEvent(
            seq=self._next_seq,
            event_id=f"E{self._next_seq}",
            kind=kind,
            timestamp=ts,
            workspace_id=self._workspace_id,
            workspace_root=self._workspace_root,
            agent_id=agent_id,
            client_id=client_id,
            session_id=session_id,
            task_id=task_id,
            git_head=git_head,
            dirty_hash=dirty_hash,
            scope=scope,
            message=clipped,
            metadata=dict(metadata or {}),
            question_id=question_id,
            truncated=truncated,
        )
        self._log.append(event)
        self._absorb(event)
        return event

    def note(
        self,
        message: str,
        *,
        agent_id: str = "",
        client_id: str = "",
        session_id: str = "",
        scope: BusScope = BusScope(),
        metadata: dict[str, str] | None = None,
    ) -> BusEvent:
        """Post a durable, untimed note."""
        return self.append_event(
            BusEventKind.NOTE_POSTED,
            agent_id=agent_id,
            client_id=client_id,
            session_id=session_id,
            scope=scope,
            message=message,
            metadata=metadata,
        )

    def ask(
        self,
        message: str,
        *,
        agent_id: str = "",
        client_id: str = "",
        session_id: str = "",
        scope: BusScope = BusScope(),
        timeout_seconds: float = DEFAULT_QUESTION_TIMEOUT_SECONDS,
        metadata: dict[str, str] | None = None,
    ) -> tuple[BusEvent, QuestionRecord]:
        """Open a timed question. Returns the opener event and record."""
        question_id = f"Q{self._next_question_index}"
        self._next_question_index += 1
        meta = dict(metadata or {})
        meta.setdefault("timeout_seconds", str(timeout_seconds))
        opener = self.append_event(
            BusEventKind.BUS_ASK,
            agent_id=agent_id,
            client_id=client_id,
            session_id=session_id,
            scope=scope,
            message=message,
            metadata=meta,
            question_id=question_id,
        )
        record = self._questions[question_id]
        record.timeout_at = opener.timestamp + max(0.0, timeout_seconds)
        return opener, record

    def reply(
        self,
        question_id: str,
        message: str,
        *,
        agent_id: str = "",
        client_id: str = "",
        session_id: str = "",
        metadata: dict[str, str] | None = None,
    ) -> BusEvent:
        """Attach a reply to ``question_id``.

        A reply that arrives after the question closed is still appended
        as a durable ``bus.reply`` event with ``metadata['late'] = 'true'``,
        but the closed digest already exists and is not retroactively
        rewritten — that's the contract pinned by
        ``test_bus_journal.test_reply_after_close_does_not_corrupt_digest``.
        """
        record = self._questions.get(question_id)
        meta = dict(metadata or {})
        scope = record.opener.scope if record is not None else BusScope()
        if record is not None and record.closed:
            meta.setdefault("late", "true")
        return self.append_event(
            BusEventKind.BUS_REPLY,
            agent_id=agent_id,
            client_id=client_id,
            session_id=session_id,
            scope=scope,
            message=message,
            metadata=meta,
            question_id=question_id,
        )

    def settle(self, *, now: float | None = None) -> list[BusEvent]:
        """Close any expired open questions; return the new ``bus.closed``
        events.

        This is the lazy close path called by ``recent``, ``weather``, and
        explicit ``settle`` callers.
        """
        ts = self._now_fn() if now is None else now
        closed_events: list[BusEvent] = []
        for record in list(self._questions.values()):
            if record.closed:
                continue
            if ts < record.timeout_at:
                continue
            digest_meta = {
                "question_id": record.question_id,
                "reply_count": str(len(record.replies)),
                "related_count": str(len(record.related)),
                "opener_event_id": record.opener.event_id,
            }
            digest_message = self._digest_message(record)
            closed_event = self.append_event(
                BusEventKind.BUS_CLOSED,
                agent_id=record.opener.agent_id,
                client_id=record.opener.client_id,
                session_id=record.opener.session_id,
                scope=record.opener.scope,
                message=digest_message,
                metadata=digest_meta,
                question_id=record.question_id,
                timestamp=ts,
            )
            closed_events.append(closed_event)
        return closed_events

    def recent(
        self,
        *,
        scope: BusScope = BusScope(),
        kinds: Iterable[BusEventKind] | None = None,
        limit: int = DEFAULT_RECENT_LIMIT,
        agent_id: str = "",
        client_id: str = "",
        skip_own: bool = True,
        after_seq: int = 0,
        now: float | None = None,
    ) -> list[BusEvent]:
        """Return recent events filtered by scope and kinds.

        Skips events authored by the supplied ``client_id``/``agent_id`` by
        default (``skip_own=True``) so callers see what *other* agents are
        doing — the dominant use case for hook digests. ``settle`` is
        called first so closed digests appear in the recent window.
        """
        self.settle(now=now)
        kind_set = set(kinds) if kinds is not None else None
        out: list[BusEvent] = []
        for event in self._events:
            if event.seq <= after_seq:
                continue
            if kind_set is not None and event.kind not in kind_set:
                continue
            if not scope.is_empty() and not scope.overlaps(event.scope):
                continue
            if skip_own and self._is_own(event, agent_id=agent_id, client_id=client_id):
                continue
            out.append(event)
        if limit > 0 and len(out) > limit:
            out = out[-limit:]
        return out

    def weather(self, *, now: float | None = None) -> WeatherReport:
        """Compose a compact workspace status snapshot."""
        ts = self._now_fn() if now is None else now
        self.settle(now=ts)
        presence_entries = self._presence.visible(ts)
        counts: dict[str, int] = {
            PresenceStatus.ACTIVE.value: 0,
            PresenceStatus.ASLEEP.value: 0,
            PresenceStatus.PRUNED.value: 0,
        }
        pinned_count = 0
        for entry in self._presence.snapshot(ts):
            counts[entry.status.value] = counts.get(entry.status.value, 0) + 1
            if entry.pinned:
                pinned_count += 1
        open_questions = tuple(q for q in self._questions.values() if not q.closed)
        closed_questions = tuple(q for q in self._questions.values() if q.closed)
        recent = self.recent(limit=DEFAULT_RECENT_LIMIT, skip_own=False, now=ts)
        return WeatherReport(
            workspace_id=self._workspace_id,
            workspace_root=self._workspace_root,
            now=ts,
            last_seq=self.last_seq,
            presence=tuple(presence_entries),
            open_questions=open_questions,
            closed_questions=closed_questions,
            recent=tuple(recent),
            counts_by_status=counts,
            pinned_count=pinned_count,
        )

    def status(self, *, now: float | None = None) -> dict[str, object]:
        """Plain-dict status, suitable for JSON over the broker socket."""
        report = self.weather(now=now)
        return {
            "workspace_id": report.workspace_id,
            "workspace_root": report.workspace_root,
            "now": report.now,
            "last_seq": report.last_seq,
            "presence_count": len(report.presence),
            "pinned_count": report.pinned_count,
            "open_question_count": len(report.open_questions),
            "closed_question_count": len(report.closed_questions),
            "counts_by_status": dict(report.counts_by_status),
        }

    def question(self, question_id: str) -> QuestionRecord | None:
        return self._questions.get(question_id)

    # --- internals ----------------------------------------------------------

    def _rehydrate(self) -> None:
        for event in self._log.replay():
            self._absorb(event, persist_log=False)
        self._next_seq = self._log.next_seq()

    def _absorb(self, event: BusEvent, *, persist_log: bool = True) -> None:
        del persist_log  # the journal already wrote when needed
        self._events.append(event)
        if event.seq >= self._next_seq:
            self._next_seq = event.seq + 1
        if event.kind is BusEventKind.BUS_ASK and event.question_id:
            self._on_ask(event)
        elif event.kind is BusEventKind.BUS_REPLY and event.question_id:
            self._on_reply(event)
        elif event.kind is BusEventKind.BUS_CLOSED and event.question_id:
            self._on_closed(event)
        else:
            self._attach_to_open_questions(event)
        self._track_question_index(event)
        self._presence.observe(event)

    def _on_ask(self, event: BusEvent) -> None:
        timeout_seconds = self._timeout_seconds_from(event)
        record = QuestionRecord(
            question_id=event.question_id,
            opener=event,
            timeout_at=event.timestamp + timeout_seconds,
        )
        self._questions[event.question_id] = record

    def _on_reply(self, event: BusEvent) -> None:
        record = self._questions.get(event.question_id)
        if record is None:
            return
        record.replies.append(event)

    def _on_closed(self, event: BusEvent) -> None:
        record = self._questions.get(event.question_id)
        if record is None:
            return
        record.closed = True
        record.closed_event = event

    def _attach_to_open_questions(self, event: BusEvent) -> None:
        for record in self._questions.values():
            if record.closed:
                continue
            if event.timestamp < record.opener.timestamp:
                continue
            if event.timestamp > record.timeout_at:
                continue
            if not record.opener.scope.overlaps(event.scope):
                continue
            record.related.append(event)

    def _track_question_index(self, event: BusEvent) -> None:
        if not event.question_id.startswith("Q"):
            return
        try:
            idx = int(event.question_id[1:])
        except ValueError:
            return
        if idx >= self._next_question_index:
            self._next_question_index = idx + 1

    def _is_own(
        self,
        event: BusEvent,
        *,
        agent_id: str,
        client_id: str,
    ) -> bool:
        if client_id and event.client_id == client_id:
            return True
        if agent_id and event.agent_id == agent_id:
            return True
        return False

    def _timeout_seconds_from(self, event: BusEvent) -> float:
        raw = event.metadata.get("timeout_seconds", "")
        try:
            return max(0.0, float(raw)) if raw else DEFAULT_QUESTION_TIMEOUT_SECONDS
        except ValueError:
            return DEFAULT_QUESTION_TIMEOUT_SECONDS

    def _digest_message(self, record: QuestionRecord) -> str:
        opener = record.opener
        head = f"{record.question_id} closed: {opener.message}".strip()
        if record.replies:
            head += f" | replies={len(record.replies)}"
        if record.related:
            head += f" | related={len(record.related)}"
        return head


__all__ = [
    "BusJournal",
    "DEFAULT_QUESTION_TIMEOUT_SECONDS",
    "DEFAULT_RECENT_LIMIT",
    "QuestionRecord",
    "WeatherReport",
]
