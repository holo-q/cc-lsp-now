"""Filesystem watcher that feeds workspace/didChangeWatchedFiles into an LSP client.

Pylance (and every other pyright-based server) rely on LSP's client-side watching
contract: the client watches files and pushes ``workspace/didChangeWatchedFiles``
notifications. Without that, the server's workspace-level analysis drifts out of
sync with the filesystem — its program view never receives invalidation events
for files edited outside the bridge (Claude's Edit tool, git ops, external editors).

Design:
- One Observer per LspClient, recursively watching every registered workspace folder.
- PatternMatchingEventHandler keeps us to .py/.pyi and skips dependency caches
  (.venv, node_modules, .git, __pycache__, .mypy_cache, .pytest_cache).
- Watchdog callbacks arrive on background threads; we cross into the LspClient's
  asyncio loop via ``run_coroutine_threadsafe`` and debounce a 100ms window so
  editor write-replace sequences collapse into one notification.
- Debounced flush also refreshes our own open_documents cache — pylance keeps
  per-URI in-memory content for didOpen'd files, so didChangeWatchedFiles alone
  isn't enough; we also push a fresh didChange for any open doc that mutated.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    from cc_lsp_now.lsp import LspClient

log = logging.getLogger(__name__)

# LSP FileChangeType enum values (per spec 3.17)
FILE_CREATED = 1
FILE_CHANGED = 2
FILE_DELETED = 3

# Directory names to skip. These are where dependency caches live; watching them
# creates event storms during package installs / venv rebuilds.
_IGNORE_DIRS = {
    ".venv", "venv", ".env", "env",
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".tox", ".nox",
    "dist", "build", ".eggs",
}

_WATCHED_SUFFIXES = {".py", ".pyi"}

_DEBOUNCE_SECONDS = 0.1


def _should_ignore_path(path: str) -> bool:
    """Skip dep-cache dirs and non-Python files. Called per-event — must be cheap."""
    p = Path(path)
    if p.suffix not in _WATCHED_SUFFIXES:
        return True
    return any(part in _IGNORE_DIRS for part in p.parts)


class _Handler(FileSystemEventHandler):
    """Per-folder watchdog handler. Forwards events into the owning watcher."""

    def __init__(self, watcher: FileWatcher) -> None:
        self._watcher = watcher

    def on_created(self, event):
        if not event.is_directory and not _should_ignore_path(event.src_path):
            self._watcher._enqueue(event.src_path, FILE_CREATED)

    def on_modified(self, event):
        if not event.is_directory and not _should_ignore_path(event.src_path):
            self._watcher._enqueue(event.src_path, FILE_CHANGED)

    def on_deleted(self, event):
        if not event.is_directory and not _should_ignore_path(event.src_path):
            self._watcher._enqueue(event.src_path, FILE_DELETED)

    def on_moved(self, event):
        if event.is_directory:
            return
        src_ignore = _should_ignore_path(event.src_path)
        dst_ignore = _should_ignore_path(event.dest_path)
        if not src_ignore:
            self._watcher._enqueue(event.src_path, FILE_DELETED)
        if not dst_ignore:
            self._watcher._enqueue(event.dest_path, FILE_CREATED)


class FileWatcher:
    """Bridge watchdog events into LSP didChangeWatchedFiles notifications.

    Must be started after the LspClient has an event loop running (start() is
    called from within the client's asyncio context). Cleanup via stop() is
    idempotent — safe to call during shutdown races.
    """

    def __init__(self, client: LspClient) -> None:
        self._client = client
        self._observer: Any | None = None
        self._watched_paths: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, int] = {}  # path → last FileChangeType
        self._flush_task: asyncio.Task | None = None
        self._stopped = False

    def start(self, folders: list[str]) -> None:
        """Begin watching the given workspace folders. Idempotent per-folder."""
        if self._stopped:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("FileWatcher.start called outside an event loop; watching disabled")
            return
        if self._observer is None:
            self._observer = Observer()
            self._observer.start()
        for folder in folders:
            self.add_folder(folder)

    def add_folder(self, folder: str) -> None:
        """Attach the observer to an additional workspace folder."""
        if self._stopped or self._observer is None:
            return
        abs_folder = os.path.abspath(folder)
        if abs_folder in self._watched_paths:
            return
        if not os.path.isdir(abs_folder):
            return
        try:
            self._observer.schedule(_Handler(self), abs_folder, recursive=True)
            self._watched_paths.add(abs_folder)
            log.info("FileWatcher watching %s", abs_folder)
        except (OSError, RuntimeError) as e:
            log.warning("FileWatcher failed to watch %s: %s", abs_folder, e)

    def stop(self) -> None:
        """Tear down the observer. Safe to call more than once."""
        self._stopped = True
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
        self._watched_paths.clear()
        self._pending.clear()

    # ── Event plumbing ───────────────────────────────────────────────────

    def _enqueue(self, path: str, change_type: int) -> None:
        """Called from watchdog's thread. Cross into the asyncio loop."""
        if self._loop is None or self._stopped:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._record(path, change_type), self._loop
            )
        except RuntimeError:
            pass  # loop shutting down

    async def _record(self, path: str, change_type: int) -> None:
        """Coalesce rapid-fire events per path into a single entry."""
        abs_path = os.path.abspath(path)
        # Later events win; a modified-then-deleted sequence ends up as deleted.
        self._pending[abs_path] = change_type
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_debounce())

    async def _flush_after_debounce(self) -> None:
        await asyncio.sleep(_DEBOUNCE_SECONDS)
        if not self._pending or self._stopped:
            return
        events = self._pending
        self._pending = {}
        self._send(events)

    def _send(self, events: dict[str, int]) -> None:
        """Emit didChangeWatchedFiles + refresh any open documents."""
        from cc_lsp_now.lsp import file_uri  # avoid circular at module load

        changes = [
            {"uri": file_uri(path), "type": change_type}
            for path, change_type in events.items()
        ]
        if changes:
            self._client.notify(
                "workspace/didChangeWatchedFiles", {"changes": changes}
            )

        # For files we've didOpen'd, push a fresh didChange too — pylance uses
        # its in-memory content for those URIs, so the watched-files notification
        # alone doesn't invalidate them.
        for path, change_type in events.items():
            uri = file_uri(path)
            if uri not in self._client._open_documents:
                continue
            if change_type == FILE_DELETED:
                self._client._open_documents.pop(uri, None)
                self._client._doc_mtime.pop(uri, None)
                continue
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                new_mtime = os.path.getmtime(path)
            except OSError:
                continue
            version = self._client._open_documents[uri] + 1
            self._client._open_documents[uri] = version
            self._client._doc_mtime[uri] = new_mtime
            self._client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
