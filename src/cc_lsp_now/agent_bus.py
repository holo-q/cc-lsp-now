"""Workspace-scoped advisory event bus for agent coordination.

The bus is deliberately small: append durable events, keep timed questions,
and render compact weather at boundaries where agents already pause. It is not
a lock manager. The first slice records enough provenance for later broker
introspection while preserving the reversible "warn only" contract from
``docs/agent-bus.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from cc_lsp_now.bus_event import BusEvent, BusEventKind, BusScope, truncate_message


DEFAULT_RECENT_LIMIT = 20


@dataclass
class BusQuestion:
    question_id: str
    opened_event_id: str
    opened_at: float
    expires_at: float
    workspace_root: str
    agent_id: str = ""
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    message: str = ""
    closed_at: float | None = None
    replies: list[int] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_wire(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "question_id": self.question_id,
            "opened_event_id": self.opened_event_id,
            "opened_at": self.opened_at,
            "expires_at": self.expires_at,
            "seconds_left": max(0.0, self.expires_at - now) if self.is_open else 0.0,
            "workspace_root": self.workspace_root,
            "agent_id": self.agent_id,
            "files": list(self.files),
            "symbols": list(self.symbols),
            "aliases": list(self.aliases),
            "message": self.message,
            "closed_at": self.closed_at,
            "replies": list(self.replies),
        }


class AgentBus:
    """Append-only, workspace-scoped coordination substrate.

    Public methods return JSON-compatible dictionaries so they can be exposed
    unchanged over the broker socket and through python-devtools inspection.
    The object is thread-safe because devtools handler threads may inspect it
    while the asyncio broker is appending events.
    """

    def __init__(self) -> None:
        self._events: list[BusEvent] = []
        self._questions: dict[str, BusQuestion] = {}
        self._next_event_id = 1
        self._next_question_id = 1
        self._lock = RLock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            open_questions = [q for q in self._questions.values() if q.is_open]
            return {
                "event_count": len(self._events),
                "last_event_id": self._events[-1].event_id if self._events else "",
                "open_question_count": len(open_questions),
                "open_questions": [q.to_wire() for q in open_questions],
            }

    def event(self, params: dict[str, Any]) -> dict[str, Any]:
        event_type = _string(params.get("event_type")) or BusEventKind.TASK_INTENT.value
        return {"event": _event_wire(self._append(event_type, params))}

    def note(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"event": _event_wire(self._append(BusEventKind.NOTE_POSTED.value, params))}

    def ask(self, params: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = _timeout_seconds(params.get("timeout"), default=180.0)
        now = time.time()
        with self._lock:
            qid = f"Q{self._next_question_id}"
            self._next_question_id += 1
            params = dict(params)
            params["question_id"] = qid
            event = self._append_locked(BusEventKind.BUS_ASK.value, params, now=now)
            question = BusQuestion(
                question_id=qid,
                opened_event_id=event.event_id,
                opened_at=now,
                expires_at=now + timeout_seconds,
                workspace_root=event.workspace_root,
                agent_id=event.agent_id,
                files=list(event.scope.files),
                symbols=list(event.scope.symbols),
                aliases=list(event.scope.aliases),
                message=event.message,
            )
            self._questions[qid] = question
            return {"event": _event_wire(event), "question": question.to_wire(now)}

    def reply(self, params: dict[str, Any]) -> dict[str, Any]:
        question_id = _string(params.get("id")) or _string(params.get("question_id"))
        if not question_id:
            raise ValueError("reply requires id or question_id")
        with self._lock:
            question = self._questions.get(question_id)
            if question is None:
                raise ValueError(f"unknown question: {question_id}")
            params = dict(params)
            params["question_id"] = question_id
            event = self._append_locked(BusEventKind.BUS_REPLY.value, params)
            question.replies.append(event.seq)
            return {"event": _event_wire(event), "question": question.to_wire()}

    def settle(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        now = time.time()
        closed: list[dict[str, Any]] = []
        with self._lock:
            for question in list(self._questions.values()):
                if not question.is_open or question.expires_at > now:
                    continue
                question.closed_at = now
                event = self._append_locked(
                    "bus.closed",
                    {
                        **params,
                        "workspace_root": question.workspace_root,
                        "files": question.files,
                        "symbols": question.symbols,
                        "aliases": question.aliases,
                    "message": f"{question.question_id} closed",
                    "question_id": question.question_id,
                    "metadata": {"opened_event_id": question.opened_event_id},
                },
                    now=now,
                )
                closed.append(self._digest_for_question(question, close_event=event))
        return {"closed": closed}

    def recent(self, params: dict[str, Any]) -> dict[str, Any]:
        self.settle(params)
        workspace_root = _workspace_root(params)
        files = _strings(params.get("files"))
        symbols = _strings(params.get("symbols"))
        aliases = _strings(params.get("aliases"))
        after_id = _int(params.get("after_id"), 0)
        limit = max(1, min(_int(params.get("limit"), DEFAULT_RECENT_LIMIT), 100))

        with self._lock:
            events = [
                event
                for event in self._events
                if event.seq > after_id
                and event.workspace_root == workspace_root
                and _scope_matches(event, files=files, symbols=symbols, aliases=aliases)
            ]
            selected = events[-limit:]
            return {
                "events": [_event_wire(event) for event in selected],
                "truncated": len(events) > len(selected),
                "last_event_id": self._events[-1].event_id if self._events else "",
            }

    def precommit(self, params: dict[str, Any]) -> dict[str, Any]:
        recent = self.recent({**params, "limit": params.get("limit", 10)})
        return {"recent": recent["events"], "suggested": _suggest_checks(recent["events"])}

    def postcommit(self, params: dict[str, Any]) -> dict[str, Any]:
        event = self._append("commit.created", params)
        return {"event": _event_wire(event)}

    def weather(self, params: dict[str, Any]) -> dict[str, Any]:
        self.settle(params)
        workspace_root = _workspace_root(params)
        with self._lock:
            open_questions = [
                q.to_wire()
                for q in self._questions.values()
                if q.is_open and q.workspace_root == workspace_root
            ]
            recent = [
                _event_wire(e)
                for e in self._events
                if e.workspace_root == workspace_root
            ][-10:]
        return {
            "workspace_root": workspace_root,
            "open_questions": open_questions,
            "recent": recent,
            "status": self.status(),
        }

    def _append(self, event_type: str, params: dict[str, Any]) -> BusEvent:
        with self._lock:
            return self._append_locked(event_type, params)

    def _append_locked(
        self,
        event_type: str,
        params: dict[str, Any],
        *,
        now: float | None = None,
    ) -> BusEvent:
        now = time.time() if now is None else now
        root = _workspace_root(params)
        kind = BusEventKind.from_wire(event_type)
        message, truncated = truncate_message(_string(params.get("message")))
        seq = self._next_event_id
        event = BusEvent(
            seq=seq,
            event_id=f"E{seq}",
            kind=kind,
            timestamp=now,
            workspace_id=_workspace_id(root),
            workspace_root=root,
            agent_id=_string(params.get("agent_id")),
            client_id=_string(params.get("client_id")),
            session_id=_string(params.get("session_id")),
            task_id=_string(params.get("task_id")),
            git_head=_string(params.get("git_head")),
            dirty_hash=_string(params.get("dirty_hash")),
            scope=BusScope(
                files=tuple(_strings(params.get("files"))),
                symbols=tuple(_strings(params.get("symbols"))),
                aliases=tuple(_strings(params.get("aliases"))),
            ),
            message=message,
            metadata=_metadata(params.get("metadata")),
            question_id=_string(params.get("question_id")),
            truncated=truncated,
        )
        self._next_event_id += 1
        self._events.append(event)
        _append_jsonl(root, event.to_wire())
        return event

    def _digest_for_question(
        self,
        question: BusQuestion,
        *,
        close_event: BusEvent,
    ) -> dict[str, Any]:
        events = [
            _event_wire(e)
            for e in self._events
            if e.workspace_root == question.workspace_root
            and question.opened_at <= e.timestamp <= (question.closed_at or close_event.timestamp)
            and e.event_id != close_event.event_id
            and (
                e.question_id == question.question_id
                or _scope_matches(e, files=question.files, symbols=question.symbols, aliases=question.aliases)
            )
        ]
        replies = [e for e in events if e.get("event_type") == "bus.reply"]
        return {
            "question": question.to_wire(close_event.timestamp),
            "close_event": _event_wire(close_event),
            "events": events,
            "replies": replies,
        }


def _workspace_root(params: dict[str, Any]) -> str:
    raw = _string(params.get("workspace_root")) or _string(params.get("root"))
    return os.path.abspath(raw or os.getcwd())


def _workspace_id(root: str) -> str:
    return hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timeout_seconds(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if not isinstance(value, str) or not value.strip():
        return default
    raw = value.strip().lower()
    scale = 1.0
    if raw.endswith("ms"):
        scale = 0.001
        raw = raw[:-2]
    elif raw.endswith("s"):
        raw = raw[:-1]
    elif raw.endswith("m"):
        scale = 60.0
        raw = raw[:-1]
    elif raw.endswith("h"):
        scale = 3600.0
        raw = raw[:-1]
    try:
        return max(0.0, float(raw) * scale)
    except ValueError:
        return default


def _scope_matches(
    event: BusEvent,
    *,
    files: list[str],
    symbols: list[str],
    aliases: list[str],
) -> bool:
    return event.scope.overlaps(BusScope(tuple(files), tuple(symbols), tuple(aliases)))


def _suggest_checks(events: list[dict[str, Any]]) -> list[str]:
    tests: list[str] = []
    for event in events:
        if event.get("kind") == BusEventKind.TEST_RAN.value or event.get("event_type") == BusEventKind.TEST_RAN.value:
            metadata = event.get("metadata", {})
            targets = metadata.get("targets", "") if isinstance(metadata, dict) else ""
            for target in _strings(targets):
                if target not in tests:
                    tests.append(target)
    return tests


def _event_wire(event: BusEvent) -> dict[str, Any]:
    wire = event.to_wire()
    # Compatibility aliases keep the first public MCP slice line-oriented and
    # close to docs/agent-bus.md while the internal primitive uses strict
    # ``kind`` and nested ``scope`` fields.
    wire["event_type"] = event.kind.value
    wire.update(event.scope.to_wire())
    return wire


def _append_jsonl(workspace_root: str, payload: dict[str, Any]) -> None:
    path = Path(workspace_root) / "tmp" / "cc-lsp-now-bus.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        # The in-memory bus remains authoritative if a workspace is read-only.
        return


__all__ = ["AgentBus", "BusEvent", "BusQuestion"]
