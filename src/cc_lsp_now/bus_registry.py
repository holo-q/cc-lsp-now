"""Bus directory and journal lookup keyed by workspace root.

The bus is workspace-scoped: every parallel agent in the same project
sees the same event log, but two unrelated projects never collide.
Workspace identity is the SHA-256 prefix of the absolute project path —
stable across processes, no central registry, no broker handshake.

Path policy:

- ``CC_LSP_BUS_DIR`` overrides the storage directory entirely (used by
  tests and by users who keep buses on a separate volume). The workspace
  id is appended so multiple roots still get distinct buckets.
- Broker mode stores under ``$XDG_STATE_HOME/cc-lsp-now/bus/<wsid>/`` so
  the bus survives clean broker restarts.
- Direct mode stores under ``<root>/tmp/cc-lsp-bus/`` because the project
  directory is the natural cleanup boundary for short-lived agent runs.

The registry caches journals per directory so multiple ``get_or_open``
callers in the same broker process share state instead of racing on the
log file.
"""

from __future__ import annotations

import hashlib
import os
from enum import Enum
from pathlib import Path
from typing import Final

from cc_lsp_now.bus_journal import BusJournal
from cc_lsp_now.bus_log import BusLog


WORKSPACE_ID_LENGTH: Final[int] = 12
LOG_FILE_NAME: Final[str] = "events.jsonl"
BUS_DIR_ENV: Final[str] = "CC_LSP_BUS_DIR"
DIRECT_TMP_DIR: Final[str] = "tmp/cc-lsp-bus"
BROKER_RELATIVE: Final[Path] = Path("cc-lsp-now") / "bus"


class BrokerMode(Enum):
    DIRECT = "direct"
    BROKER = "broker"


def workspace_id_for(root: str | Path) -> str:
    """Stable, short, content-addressed id derived from the absolute root.

    SHA-256 prefix is overkill for collision resistance at this scale, but
    the cost is negligible and the prefix is what the broker prints in
    weather output, so a short hex string is what we want.
    """
    absolute = str(Path(root).expanduser().resolve())
    digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()
    return digest[:WORKSPACE_ID_LENGTH]


def bus_dir_for(root: str | Path, mode: BrokerMode) -> Path:
    """Resolve the bus directory for ``root`` under the given mode.

    The ``CC_LSP_BUS_DIR`` override always wins so tests and isolated
    runs can pin the path without thinking about XDG.
    """
    wsid = workspace_id_for(root)
    override = os.environ.get(BUS_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser() / wsid
    if mode is BrokerMode.BROKER:
        state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
        return base / BROKER_RELATIVE / wsid
    return Path(root).expanduser().resolve() / DIRECT_TMP_DIR


def log_path_for(root: str | Path, mode: BrokerMode) -> Path:
    """Path to the JSONL events file for ``root``."""
    return bus_dir_for(root, mode) / LOG_FILE_NAME


class BusRegistry:
    """Process-local cache of open journals keyed by storage directory.

    The registry is the seam where the broker picks ``BROKER`` mode and
    the MCP server in direct mode picks ``DIRECT`` — the journal itself
    has no opinion on either.
    """

    def __init__(self) -> None:
        self._journals: dict[str, BusJournal] = {}

    def get_or_open(self, root: str | Path, mode: BrokerMode) -> BusJournal:
        directory = bus_dir_for(root, mode)
        directory.mkdir(parents=True, exist_ok=True)
        cache_key = str(directory)
        existing = self._journals.get(cache_key)
        if existing is not None:
            return existing
        wsid = workspace_id_for(root)
        absolute_root = str(Path(root).expanduser().resolve())
        log = BusLog(directory / LOG_FILE_NAME)
        journal = BusJournal.open(
            log,
            workspace_id=wsid,
            workspace_root=absolute_root,
        )
        self._journals[cache_key] = journal
        return journal

    def opened(self) -> list[BusJournal]:
        return list(self._journals.values())

    def forget(self, root: str | Path, mode: BrokerMode) -> bool:
        directory = bus_dir_for(root, mode)
        return self._journals.pop(str(directory), None) is not None


__all__ = [
    "BUS_DIR_ENV",
    "BrokerMode",
    "BusRegistry",
    "LOG_FILE_NAME",
    "WORKSPACE_ID_LENGTH",
    "bus_dir_for",
    "log_path_for",
    "workspace_id_for",
]
