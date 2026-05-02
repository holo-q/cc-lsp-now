"""Workspace-session registry for the hsp-broker daemon.

The broker design (see `docs/broker.md`) keeps one warm language-server
session per `(language, root, command, args, env/config hash)` and
reference-counts active clients on top.  This module is the *core* of that
registry — pure data, no socket, no LSP, so render-memory and broker
plumbing can both rely on it without dragging in network state.

Session identity is intentionally explicit (root + opaque config_hash) so
upstream callers — broker daemon, future client lease bookkeeping,
render-memory epoch invalidation — can all hash on the same key.

This file deliberately does not start any servers.  It is the seam where
later slices plug in `LspSession`, warmup state, render-memory alias books,
and snapshot stamps.  Keeping it pure now keeps the wiring honest later.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SessionKey:
    """Identity of a broker workspace session.

    `root` is the workspace folder (absolute path string — the broker treats
    paths as opaque text and does not resolve symlinks here).  `config_hash`
    is an opaque digest covering the language-server command line, args,
    label, and any env vars that change semantics.  Two callers that supply
    the same `(root, config_hash)` pair share one warm session.
    """

    root: str
    config_hash: str


def config_hash(
    server_label: str,
    command: str,
    args: tuple[str, ...] | list[str] = (),
    env: dict[str, str] | None = None,
) -> str:
    """Stable short digest covering the parts of a session config that
    change semantic answers.

    Kept as a free function so direct-mode and broker-mode callers can
    derive the same hash without instantiating a registry.  Intentionally
    short (12 hex chars) — collisions across the same workspace root would
    require an attacker-shaped payload, and broker session keys also pin
    `root`.
    """
    h = hashlib.sha256()
    h.update(server_label.encode("utf-8"))
    h.update(b"\x00")
    h.update(command.encode("utf-8"))
    h.update(b"\x00")
    for a in args:
        h.update(a.encode("utf-8"))
        h.update(b"\x00")
    if env:
        for k in sorted(env):
            h.update(k.encode("utf-8"))
            h.update(b"=")
            h.update(env[k].encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()[:12]


@dataclass
class BrokerSession:
    """Mutable session record owned by the registry.

    `client_count` tracks borrow/return calls; idle eviction policy in a
    later slice will use `last_used_at` once `client_count == 0`.  The
    record is the storage seam for render-memory epochs — when the broker
    eventually owns the alias book, it lives keyed by `session_id`.
    """

    session_id: str
    key: SessionKey
    server_label: str = ""
    started_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    client_count: int = 0

    def touch(self, now: float | None = None) -> None:
        self.last_used_at = now if now is not None else time.time()


class SessionRegistry:
    """In-memory map of `SessionKey -> BrokerSession`.

    The registry is single-process; concurrency control lives one layer up
    (the broker daemon dispatches requests serially per asyncio loop).
    A future broker version will swap this for a session manager that also
    supervises LSP processes; that swap should not change the call sites.
    """

    def __init__(self) -> None:
        self._sessions: dict[SessionKey, BrokerSession] = {}
        self._counter: int = 0

    def get_or_create(
        self,
        key: SessionKey,
        *,
        server_label: str = "",
    ) -> BrokerSession:
        """Return the session for `key`, minting one if it is the first
        sighting.  Same key always returns the same `session_id` within
        the registry's lifetime — render-memory aliases rely on this.
        """
        existing = self._sessions.get(key)
        if existing is not None:
            existing.touch()
            return existing
        self._counter += 1
        session = BrokerSession(
            session_id=f"s{self._counter}",
            key=key,
            server_label=server_label,
        )
        self._sessions[key] = session
        return session

    def get(self, session_id: str) -> BrokerSession | None:
        for s in self._sessions.values():
            if s.session_id == session_id:
                return s
        return None

    def all_sessions(self) -> list[BrokerSession]:
        return list(self._sessions.values())

    def stop(self, session_id: str) -> bool:
        """Drop a session from the registry; returns True if found.

        v1 has no LSP processes to terminate, so this is just registry
        cleanup.  Later slices will hook process shutdown here.
        """
        for k, s in list(self._sessions.items()):
            if s.session_id == session_id:
                del self._sessions[k]
                return True
        return False

    def __len__(self) -> int:
        return len(self._sessions)


def session_to_dict(s: BrokerSession) -> dict[str, object]:
    """Wire-shape projection used by `status` / `session.list` responses.

    Kept here (not in broker.py) so the registry owns its own serialization
    and tests can assert the shape without reaching into broker internals.
    """
    return {
        "session_id": s.session_id,
        "root": s.key.root,
        "config_hash": s.key.config_hash,
        "server_label": s.server_label,
        "started_at": s.started_at,
        "last_used_at": s.last_used_at,
        "client_count": s.client_count,
    }


__all__ = [
    "BrokerSession",
    "SessionKey",
    "SessionRegistry",
    "config_hash",
    "session_to_dict",
]
