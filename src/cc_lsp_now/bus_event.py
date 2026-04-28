"""Bus event primitives for the agent bus.

The agent bus (see ``docs/agent-bus.md``) is workspace-scoped, warn-only
weather: durable events, recent/weather/recent digests, timed
questions/replies/settle, and decaying presence. It is not a claim or lease
system. This module is the pure event shape — no IO, no clocks, no
filesystem — so the broker, MCP server, and tests can all share the same
wire schema without dragging in stateful pieces.

Schema is intentionally strict at write time and forgiving at replay time:
unknown fields in a JSONL line are ignored so a forward-rolled log can be
read by an older binary without crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, cast


SCHEMA_VERSION: Final[int] = 1

# Cap a single event message at 8 KiB. Bigger payloads should land elsewhere
# (test logs, file snapshots) — the bus is for compact signal, not storage.
MAX_MESSAGE_BYTES: Final[int] = 8 * 1024


class BusEventKind(Enum):
    """Public, workspace-scoped event kinds.

    String values are the wire form. The set is closed: callers wanting a
    different signal pick the closest fit (``note.posted`` for free-text
    coordination, ``task.intent`` for intent-shaped statements) and put
    structured detail in ``BusEvent.metadata``.
    """

    AGENT_STARTED = "agent.started"
    USER_PROMPT = "user.prompt"
    TASK_INTENT = "task.intent"
    FILE_TOUCHED = "file.touched"
    SYMBOL_TOUCHED = "symbol.touched"
    TEST_RAN = "test.ran"
    COMMIT_CREATED = "commit.created"
    NOTE_POSTED = "note.posted"
    BUS_ASK = "bus.ask"
    BUS_REPLY = "bus.reply"
    BUS_CLOSED = "bus.closed"

    @classmethod
    def from_wire(cls, value: str) -> BusEventKind:
        for kind in cls:
            if kind.value == value:
                return kind
        raise ValueError(f"unknown bus event kind: {value!r}")


@dataclass(frozen=True, slots=True)
class BusScope:
    """Files / symbols / aliases an event or question concerns.

    Scope is the only filter the bus ever applies — `recent`, the
    digest writer, and overlap checks all use ``overlaps``. An empty scope is
    a wildcard: it overlaps anything, on the theory that an event without a
    scope is a workspace-wide note (``agent.started``, ``user.prompt``,
    workspace-level commits).
    """

    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.files or self.symbols or self.aliases)

    def overlaps(self, other: BusScope) -> bool:
        if self.is_empty() or other.is_empty():
            return True
        if any(f in other.files for f in self.files):
            return True
        if any(s in other.symbols for s in self.symbols):
            return True
        return any(a in other.aliases for a in self.aliases)

    @classmethod
    def parse(
        cls,
        files: str = "",
        symbols: str = "",
        aliases: str = "",
    ) -> BusScope:
        """Build a scope from comma- or newline-separated user input.

        The MCP surface accepts plain text (``files="src/server.py,src/x.py"``)
        — this normalizer is the one place we strip whitespace and drop
        empty entries so callers don't sprinkle the rule.
        """
        return cls(
            files=_split(files),
            symbols=_split(symbols),
            aliases=_split(aliases),
        )

    def to_wire(self) -> dict[str, list[str]]:
        return {
            "files": list(self.files),
            "symbols": list(self.symbols),
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_wire(cls, payload: object) -> BusScope:
        if not isinstance(payload, dict):
            return cls()
        data = cast(dict[str, object], payload)
        return cls(
            files=_string_tuple(data.get("files")),
            symbols=_string_tuple(data.get("symbols")),
            aliases=_string_tuple(data.get("aliases")),
        )


def _split(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    pieces: list[str] = []
    for piece in value.replace("\n", ",").split(","):
        cleaned = piece.strip()
        if cleaned:
            pieces.append(cleaned)
    return tuple(pieces)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
    return tuple(out)


def truncate_message(message: str, limit: int = MAX_MESSAGE_BYTES) -> tuple[str, bool]:
    """Clip ``message`` to ``limit`` UTF-8 bytes.

    Truncation happens at byte boundaries (with a UTF-8-safe shrink) so the
    on-disk JSON line is always valid. Returns ``(clipped, truncated_flag)``;
    callers store the flag on the event so digests can show ``...``.
    """
    if not message:
        return "", False
    encoded = message.encode("utf-8")
    if len(encoded) <= limit:
        return message, False
    cut = encoded[:limit]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore"), True


@dataclass(frozen=True, slots=True)
class BusEvent:
    """One durable bus event.

    ``seq`` is the per-log monotonic id (1-based); ``event_id`` is the short
    printable handle (e.g. ``E12``). Both are assigned by the journal at
    append time. ``question_id`` ties replies and closures to their opener.
    ``truncated`` is True when ``message`` was clipped by
    :func:`truncate_message`.

    The class is frozen because events are immutable once written; the
    ``metadata`` dict is treated as a value (callers should not mutate it
    after construction). Equality is structural — two events with the same
    fields compare equal so log replay is stable.
    """

    seq: int
    event_id: str
    kind: BusEventKind
    timestamp: float
    workspace_id: str
    workspace_root: str
    agent_id: str
    client_id: str
    session_id: str
    task_id: str
    git_head: str
    dirty_hash: str
    scope: BusScope
    message: str
    metadata: dict[str, str]
    question_id: str = ""
    truncated: bool = False
    schema_version: int = SCHEMA_VERSION

    def to_wire(self) -> dict[str, object]:
        """Return the JSONL-friendly projection of this event."""
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "timestamp": self.timestamp,
            "workspace_id": self.workspace_id,
            "workspace_root": self.workspace_root,
            "agent_id": self.agent_id,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "git_head": self.git_head,
            "dirty_hash": self.dirty_hash,
            "scope": self.scope.to_wire(),
            "message": self.message,
            "metadata": dict(self.metadata),
            "question_id": self.question_id,
            "truncated": self.truncated,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> BusEvent:
        kind_value = payload.get("kind")
        if not isinstance(kind_value, str):
            raise ValueError("bus event payload is missing 'kind'")
        kind = BusEventKind.from_wire(kind_value)
        return cls(
            seq=_int(payload.get("seq")),
            event_id=_str(payload.get("event_id")),
            kind=kind,
            timestamp=_float(payload.get("timestamp")),
            workspace_id=_str(payload.get("workspace_id")),
            workspace_root=_str(payload.get("workspace_root")),
            agent_id=_str(payload.get("agent_id")),
            client_id=_str(payload.get("client_id")),
            session_id=_str(payload.get("session_id")),
            task_id=_str(payload.get("task_id")),
            git_head=_str(payload.get("git_head")),
            dirty_hash=_str(payload.get("dirty_hash")),
            scope=BusScope.from_wire(payload.get("scope")),
            message=_str(payload.get("message")),
            metadata=_string_string_dict(payload.get("metadata")),
            question_id=_str(payload.get("question_id")),
            truncated=bool(payload.get("truncated", False)),
            schema_version=_int(payload.get("schema_version"), default=SCHEMA_VERSION),
        )


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _string_string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    data = cast(dict[object, object], value)
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


__all__ = [
    "BusEvent",
    "BusEventKind",
    "BusScope",
    "MAX_MESSAGE_BYTES",
    "SCHEMA_VERSION",
    "truncate_message",
]
