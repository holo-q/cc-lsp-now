from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from cc_lsp_now.agent_log import agent_log
from cc_lsp_now.file_watcher import FileWatcher

log = logging.getLogger(__name__)

EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".go": "go",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascriptreact",
    ".tsx": "typescriptreact",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".rb": "ruby",
    ".lua": "lua",
    ".zig": "zig",
}


def file_uri(path: str) -> str:
    resolved = Path(path).resolve()
    return resolved.as_uri()


def _language_id(uri: str) -> str:
    path = uri.removeprefix("file://")
    ext = Path(path).suffix
    return EXTENSION_LANGUAGE_MAP.get(ext, "plaintext")


_VENV_CANDIDATES = (".venv", "venv", ".env")


def _detect_venv_python(scope_uri: str | None) -> str | None:
    """Find a venv python interpreter under the scope URI.

    VS Code's Python extension injects pythonPath into the `python` config so
    pylance can auto-exclude site-packages. We do the same by checking standard
    venv locations under the workspace scope.
    """
    if not scope_uri or not scope_uri.startswith("file://"):
        return None
    scope_path = scope_uri.removeprefix("file://")
    scope = Path(scope_path)
    for name in _VENV_CANDIDATES:
        for exe in ("python", "python3"):
            candidate = scope / name / "bin" / exe
            if candidate.is_file():
                return str(candidate)
    return None


def _config_for(item: dict) -> dict:
    """Build the config response for one workspace/configuration item.

    Only `python` section gets a `pythonPath` — rest stay empty (server uses
    defaults). This mirrors what VS Code's Python extension does.
    """
    section = item.get("section", "")
    if section == "python":
        venv_python = _detect_venv_python(item.get("scopeUri"))
        if venv_python:
            return {"pythonPath": venv_python, "defaultInterpreterPath": venv_python}
    return {}


class LspError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"LSP error {code}: {message}")


class LspClient:
    def __init__(self, command: list[str], root_path: str):
        self._command = command
        self._root_path = os.path.abspath(root_path)
        self._root_uri = file_uri(self._root_path)

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._capabilities: dict = {}

        self.diagnostics: dict[str, list] = {}
        self._open_documents: dict[str, int] = {}
        # mtime snapshot for open documents — resync_open_documents uses it to
        # detect divergence between in-memory and on-disk content and push didChange.
        self._doc_mtime: dict[str, float] = {}
        # Absolute paths of workspace folders currently registered with the server.
        self.workspace_folders: set[str] = {self._root_path}
        self._started = False
        self._file_watcher: FileWatcher | None = None

    @property
    def capabilities(self) -> dict:
        return self._capabilities

    async def start(self) -> None:
        if self._started:
            return

        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._started = True

        result = await self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self._root_uri,
                "rootPath": self._root_path,
                "capabilities": {
                    "textDocument": {
                        "diagnostic": {},
                        "codeAction": {},
                        "rename": {"prepareSupport": True},
                        "signatureHelp": {},
                        "completion": {
                            "completionItem": {"snippetSupport": False},
                        },
                        "formatting": {},
                        "typeDefinition": {},
                        "documentSymbol": {},
                        "publishDiagnostics": {"relatedInformation": True},
                        "callHierarchy": {},
                        "typeHierarchy": {},
                    },
                    "workspace": {
                        "workspaceFolders": True,
                        "configuration": True,
                        "fileOperations": {
                            "dynamicRegistration": False,
                            "willRename": True,
                            "didRename": True,
                            "willCreate": True,
                            "didCreate": True,
                            "willDelete": True,
                            "didDelete": True,
                        },
                        "workspaceEdit": {
                            "documentChanges": True,
                            "resourceOperations": ["create", "rename", "delete"],
                            "failureHandling": "textOnlyTransactional",
                            "normalizesLineEndings": True,
                            "changeAnnotationSupport": {"groupsOnLabel": True},
                        },
                    },
                },
                "workspaceFolders": [
                    {"uri": self._root_uri, "name": os.path.basename(self._root_path)},
                ],
            },
        )
        self._capabilities = result.get("capabilities", {})
        self.notify("initialized", {})
        log.info("LSP server initialized: %s", self._command)

        # Start filesystem watching on the root workspace. Further folders added
        # via add_workspace_folder() also get attached to the observer.
        self._file_watcher = FileWatcher(self)
        self._file_watcher.start([self._root_path])

    async def stop(self) -> None:
        if not self._started or self._process is None:
            return

        if self._file_watcher is not None:
            self._file_watcher.stop()
            self._file_watcher = None

        try:
            await self.request("shutdown", None)
        except (LspError, ConnectionError, BrokenPipeError):
            pass

        try:
            self.notify("exit", None)
        except (ConnectionError, BrokenPipeError):
            pass

        for task in (self._reader_task, getattr(self, "_stderr_task", None)):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        try:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            self._process.kill()

        self._started = False
        self._open_documents.clear()
        self._pending.clear()
        log.info("LSP server stopped")

    async def request(
        self, method: str, params: dict | None, *, timeout: float = 30.0
    ) -> Any:
        self._request_id += 1
        msg_id = self._request_id

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = future

        self._send(msg)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            agent_log(f"{self._command[0]} timed out on {method} after {timeout}s")
            raise

    def notify(self, method: str, params: dict | None) -> None:
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def add_workspace_folder(self, folder_path: str) -> bool:
        """Register an additional workspace folder with the server. Returns True if added."""
        abs_path = os.path.abspath(folder_path)
        if abs_path in self.workspace_folders:
            return False
        self.workspace_folders.add(abs_path)
        folder_uri = file_uri(abs_path)
        self.notify(
            "workspace/didChangeWorkspaceFolders",
            {
                "event": {
                    "added": [{"uri": folder_uri, "name": os.path.basename(abs_path)}],
                    "removed": [],
                }
            },
        )
        log.info("Added workspace folder: %s", abs_path)
        if self._file_watcher is not None:
            self._file_watcher.add_folder(abs_path)
        return True

    async def ensure_document(self, uri: str) -> None:
        path = uri.removeprefix("file://")
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        try:
            self._doc_mtime[uri] = os.path.getmtime(path)
        except OSError:
            pass

        if uri not in self._open_documents:
            self._open_documents[uri] = 0
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": _language_id(uri),
                        "version": 0,
                        "text": text,
                    },
                },
            )
        else:
            version = self._open_documents[uri] + 1
            self._open_documents[uri] = version
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )

    async def resync_open_documents(self) -> int:
        """Re-sync any open documents whose in-memory content diverged from disk.

        For each didOpen'd URI we hold, compare the snapshot mtime against the
        current file mtime. If they differ, the disk was written to outside our
        bridge (Claude's Edit tool, git, another editor), so re-send a didChange
        with the on-disk contents.

        Runs before every LSP request as a guard against out-of-sync state when
        no filesystem watcher is active. O(N) stat calls per sweep.

        Returns the number of documents re-synced.
        """
        refreshed = 0
        for uri in list(self._open_documents.keys()):
            path = uri.removeprefix("file://")
            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                continue
            if current_mtime == self._doc_mtime.get(uri):
                continue
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            version = self._open_documents[uri] + 1
            self._open_documents[uri] = version
            self._doc_mtime[uri] = current_mtime
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
            refreshed += 1
        return refreshed

    def notify_files_renamed(self, renames: list[tuple[str, str]]) -> None:
        """Tell the server that files moved on disk. Also drops stale open-document state."""
        if not renames:
            return
        files = [
            {"oldUri": file_uri(old), "newUri": file_uri(new)}
            for old, new in renames
        ]
        self.notify("workspace/didRenameFiles", {"files": files})
        for old, _new in renames:
            old_uri = file_uri(old)
            self._open_documents.pop(old_uri, None)
            self._doc_mtime.pop(old_uri, None)

    def notify_files_created(self, paths: list[str]) -> None:
        if not paths:
            return
        self.notify(
            "workspace/didCreateFiles",
            {"files": [{"uri": file_uri(p)} for p in paths]},
        )

    def notify_files_deleted(self, paths: list[str]) -> None:
        if not paths:
            return
        self.notify(
            "workspace/didDeleteFiles",
            {"files": [{"uri": file_uri(p)} for p in paths]},
        )
        for p in paths:
            uri = file_uri(p)
            self._open_documents.pop(uri, None)
            self._doc_mtime.pop(uri, None)

    def _send(self, msg: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n"
        self._process.stdin.write(header.encode("ascii") + body)

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        reader = self._process.stdout

        try:
            while True:
                content_length = await self._read_headers(reader)
                if content_length is None:
                    break

                body = await reader.readexactly(content_length)
                msg = json.loads(body)
                self._dispatch(msg)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("LSP read loop error")
        finally:
            self._last_stderr_tail = getattr(self, "_last_stderr_tail", [])
            tail = "\n".join(self._last_stderr_tail[-20:]) if self._last_stderr_tail else ""
            for future in self._pending.values():
                if not future.done():
                    msg = "LSP server disconnected"
                    if tail:
                        msg += f". Last stderr:\n{tail}"
                    future.set_exception(ConnectionError(msg))
            self._pending.clear()

    async def _stderr_loop(self) -> None:
        """Drain the server's stderr into log.warning. Keeps a tail of the last
        N lines so we can include them in the disconnect message."""
        assert self._process is not None and self._process.stderr is not None
        reader = self._process.stderr
        self._last_stderr_tail: list[str] = []
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if not decoded:
                    continue
                log.warning("LSP stderr (%s): %s", self._command[0], decoded)
                self._last_stderr_tail.append(decoded)
                if len(self._last_stderr_tail) > 40:
                    self._last_stderr_tail = self._last_stderr_tail[-40:]
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("LSP stderr loop error")

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> int | None:
        content_length = -1
        while True:
            line = await reader.readline()
            if not line:
                return None
            decoded = line.decode("ascii").strip()
            if not decoded:
                break
            if decoded.lower().startswith("content-length:"):
                content_length = int(decoded.split(":", 1)[1].strip())

        if content_length < 0:
            return None
        return content_length

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and "method" not in msg:
            # Response to a request we sent
            msg_id = msg["id"]
            future = self._pending.pop(msg_id, None)
            if future is None or future.done():
                return

            if "error" in msg:
                err = msg["error"]
                future.set_exception(
                    LspError(err.get("code", -1), err.get("message", ""), err.get("data"))
                )
            else:
                future.set_result(msg.get("result"))

        elif "method" in msg and "id" not in msg:
            # Notification from server
            self._handle_notification(msg["method"], msg.get("params", {}))

        elif "method" in msg and "id" in msg:
            # Server-to-client request. Some servers (notably pylance) blow up
            # if we blanket-reject these — they're waiting on responses to
            # advance their state machine. Handle the common ones sanely.
            method = msg["method"]
            req_id = msg["id"]
            params = msg.get("params", {}) or {}

            if method == "workspace/configuration":
                items = params.get("items", [])
                result = [_config_for(it) for it in items]
                sections = [it.get("section", "?") for it in items]
                agent_log(f"[{self._command[0]} ← {method}] sections={sections}")
                self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
            elif method == "workspace/workspaceFolders":
                folders = [
                    {"uri": file_uri(f), "name": os.path.basename(f)}
                    for f in sorted(self.workspace_folders)
                ]
                agent_log(f"[{self._command[0]} ← {method}] returning {len(folders)} folders")
                self._send({"jsonrpc": "2.0", "id": req_id, "result": folders})
            elif method in (
                "client/registerCapability",
                "client/unregisterCapability",
                "window/workDoneProgress/create",
                "window/showMessageRequest",
            ):
                agent_log(f"[{self._command[0]} ← {method}] ack")
                self._send({"jsonrpc": "2.0", "id": req_id, "result": None})
            else:
                agent_log(f"[{self._command[0]} ← {method}] unknown, returning -32601")
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            self.diagnostics[uri] = params.get("diagnostics", [])
            log.debug("Diagnostics updated for %s: %d items", uri, len(self.diagnostics[uri]))
        elif method == "window/logMessage":
            msg_type = params.get("type", 4)
            message = params.get("message", "")
            level = {1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO}.get(msg_type, logging.DEBUG)
            log.log(level, "LSP [%s]: %s", self._command[0], message)
            if msg_type <= 3:
                label = {1: "error", 2: "warning", 3: "info"}.get(msg_type, "log")
                agent_log(f"[{self._command[0]} {label}] {message}")
