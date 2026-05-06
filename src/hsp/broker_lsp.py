from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from hsp.agent_log import agent_log
from hsp.alias_coordinator import AliasCoordinator, AliasTouchResult
from hsp.broker_session import SessionKey, SessionRegistry, config_hash, session_to_dict
from hsp.chain_server import ChainServer
from hsp.lsp import LspClient, LspError
from hsp.router import find_project_root
from hsp.render_memory import AliasIdentity, AliasResolution


ClientFactory = Callable[[list[str], str], LspClient]
log = logging.getLogger(__name__)

_SLOW_METHODS: set[str] = {
    "workspace/willRenameFiles",
}
_SLOW_TIMEOUT = 300.0
_DEFAULT_TIMEOUT = 30.0


def chain_to_wire(chain: list[ChainServer]) -> list[dict[str, object]]:
    return [
        {
            "command": cfg.command,
            "args": list(cfg.args),
            "name": cfg.name,
            "label": cfg.label,
        }
        for cfg in chain
    ]


def chain_from_wire(items: object) -> list[ChainServer]:
    if not isinstance(items, list) or not items:
        raise ValueError("chain must be a non-empty list")
    result: list[ChainServer] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("chain entries must be objects")
        row = cast(dict[str, object], item)
        command = row.get("command")
        args_obj = row.get("args", [])
        name = row.get("name", command)
        label = row.get("label", name)
        if not isinstance(command, str) or not command:
            raise ValueError("chain entry command must be a non-empty string")
        if not isinstance(args_obj, list) or any(not isinstance(a, str) for a in args_obj):
            raise ValueError("chain entry args must be a list of strings")
        args = cast(list[str], args_obj)
        if not isinstance(name, str) or not name:
            name = command
        if not isinstance(label, str) or not label:
            label = name
        result.append(ChainServer(command=command, args=list(args), name=name, label=label))
    return result


def chain_config_hash(language: str, chain: list[ChainServer]) -> str:
    payload: list[str] = []
    for cfg in chain:
        payload.extend([cfg.name, cfg.command, *cfg.args, "\0"])
    return config_hash(language or "unknown", "chain", payload)


def _timeout_for(method: str) -> float:
    return _SLOW_TIMEOUT if method in _SLOW_METHODS else _DEFAULT_TIMEOUT


def _is_empty_result(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, (list, dict, str)) and len(result) == 0:
        return True
    return False


def _uri_to_path(uri: str) -> str:
    return uri.removeprefix("file://") if uri.startswith("file://") else uri


def _project_markers() -> list[str]:
    raw = os.environ.get("LSP_PROJECT_MARKERS", ".git").strip()
    return [m.strip() for m in raw.split(",") if m.strip()]


def _find_project_root(file_path: str, markers: list[str] | None = None) -> str | None:
    markers = markers or _project_markers()
    if not markers:
        return None
    return find_project_root(file_path, markers)


@dataclass
class BrokerRequestResult:
    result: Any
    server_label: str
    started: list[str] = field(default_factory=list)
    workspaces_added: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, object]:
        return {
            "result": self.result,
            "server_label": self.server_label,
            "started": self.started,
            "workspaces_added": self.workspaces_added,
        }


class BrokerLspSession:
    """Broker-owned LSP chain for one workspace/config hash.

    The CPU win comes from this object being process-external to all agent
    MCP servers.  Every subagent still gets its own MCP process, but the
    expensive compiler/live-index state is centralized here and keyed by
    `(root, config_hash)`.
    """

    def __init__(
        self,
        root: str,
        chain: list[ChainServer],
        *,
        client_factory: ClientFactory | None = None,
        prefer: dict[str, int] | None = None,
        project_markers: list[str] | None = None,
        language: str = "",
        route_id: str = "",
        route_reason: str = "",
    ) -> None:
        self.root = os.path.abspath(root)
        self.chain = chain
        self.language = language
        self.route_id = route_id
        self.route_reason = route_reason
        self.clients: list[LspClient | None] = [None] * len(chain)
        self.method_handler: dict[str, int | None] = dict(prefer or {})
        self.project_markers = list(project_markers or _project_markers())
        self.pending_workspace_adds: list[str] = []
        self.lock = asyncio.Lock()
        self.client_factory = client_factory or (lambda command, root_path: LspClient(command, root_path))
        self.last_used_at = time.time()
        self.request_count = 0
        self.last_method = ""
        self.last_server_label = ""
        self.last_duration_ms = 0
        self.client_request_counts: list[int] = [0] * len(chain)
        self.aliases = AliasCoordinator()

    async def stop(self) -> None:
        async with self.lock:
            for idx, client in enumerate(list(self.clients)):
                if client is None:
                    continue
                try:
                    await client.stop()
                finally:
                    self.clients[idx] = None

    async def add_workspace(self, path: str) -> dict[str, object]:
        abs_path = os.path.abspath(path)
        if abs_path not in self.pending_workspace_adds:
            self.pending_workspace_adds.append(abs_path)
        added: list[str] = []
        async with self.lock:
            for client in self.clients:
                if client is None:
                    continue
                if client.add_workspace_folder(abs_path):
                    added.append(abs_path)
        return {"path": abs_path, "added": added, "queued": True}

    async def diagnostics(self, uri: str) -> dict[str, object]:
        async with self.lock:
            for idx, client in enumerate(self.clients):
                if client is None:
                    continue
                return {
                    "server_label": self.chain[idx].label,
                    "items": client.diagnostics.get(uri, []),
                }
        return {"server_label": "", "items": []}

    async def notify_files(
        self,
        *,
        renamed: list[tuple[str, str]],
        created: list[str],
        deleted: list[str],
    ) -> dict[str, object]:
        async with self.lock:
            notified: list[str] = []
            for idx, client in enumerate(self.clients):
                if client is None:
                    continue
                client.notify_files_renamed(renamed)
                client.notify_files_created(created)
                client.notify_files_deleted(deleted)
                notified.append(self.chain[idx].label)
        return {"notified": notified}

    async def render_touch(self, client_id: str, identities: list[AliasIdentity]) -> AliasTouchResult:
        async with self.lock:
            self.last_used_at = time.time()
            return self.aliases.touch(client_id, identities)

    async def render_lookup(self, token: str) -> AliasResolution:
        async with self.lock:
            self.last_used_at = time.time()
            return self.aliases.lookup(token)

    async def render_reset_client(self, client_id: str) -> dict[str, object]:
        async with self.lock:
            return {"reset": self.aliases.clear_client(client_id)}

    async def render_reset_session(self, reason: str = "") -> dict[str, object]:
        async with self.lock:
            self.aliases.clear_epoch(reason)
            return self.aliases.status()

    async def request(
        self,
        method: str,
        params: dict | None,
        *,
        uri: str | None,
        empty_fallback_methods: set[str],
    ) -> BrokerRequestResult:
        async with self.lock:
            self.last_used_at = time.time()
            self.request_count += 1
            self.last_method = method
            started: list[str] = []
            workspaces_added: list[str] = []
            timeout = _timeout_for(method)
            started_at = time.monotonic()

            async def get_client(idx: int) -> LspClient:
                if self.clients[idx] is None:
                    cfg = self.chain[idx]
                    client = self.client_factory([cfg.command, *cfg.args], self.root)
                    await client.start()
                    self.clients[idx] = client
                    started.append(cfg.label)
                    for pending in list(self.pending_workspace_adds):
                        client.add_workspace_folder(pending)
                client = self.clients[idx]
                assert client is not None
                return client

            async def prepare_client(idx: int) -> LspClient:
                client = await get_client(idx)
                await client.resync_open_documents()
                if uri:
                    added = self._ensure_workspace_for_client(client, uri)
                    if added and added not in workspaces_added:
                        workspaces_added.append(added)
                    await client.ensure_document(uri)
                return client

            cached_idx = self.method_handler.get(method, "missing")
            if cached_idx != "missing":
                if cached_idx is None:
                    raise LspError(-32601, f"{method} not supported by any server in the chain")
                idx = int(cached_idx)
                client = await prepare_client(idx)
                try:
                    result = await client.request(method, params, timeout=timeout)
                    self.client_request_counts[idx] += 1
                    self.last_server_label = self.chain[idx].label
                    self.last_duration_ms = int((time.monotonic() - started_at) * 1000)
                    log.info(
                        "lsp.request root=%s method=%s server=%s duration_ms=%s cached=true",
                        self.root,
                        method,
                        self.chain[idx].label,
                        self.last_duration_ms,
                    )
                    return BrokerRequestResult(result, self.chain[idx].label, started, workspaces_added)
                except asyncio.TimeoutError:
                    agent_log(f"{self.chain[idx].label} timed out on {method} (broker cached), invalidating")
                    self.method_handler.pop(method, None)

            last_err: LspError | None = None
            last_empty: Any = None
            last_empty_idx: int | None = None
            for idx, _cfg in enumerate(self.chain):
                client = await prepare_client(idx)
                try:
                    result = await client.request(method, params, timeout=timeout)
                    self.client_request_counts[idx] += 1
                except asyncio.TimeoutError:
                    agent_log(f"{self.chain[idx].label} timed out on {method} in broker, trying next")
                    continue
                except LspError as e:
                    if e.code != -32601:
                        raise
                    last_err = e
                    continue

                is_last = idx == len(self.chain) - 1
                if method in empty_fallback_methods and _is_empty_result(result) and not is_last:
                    last_empty = result
                    last_empty_idx = idx
                    continue

                self.method_handler[method] = idx
                self.last_server_label = self.chain[idx].label
                self.last_duration_ms = int((time.monotonic() - started_at) * 1000)
                log.info(
                    "lsp.request root=%s method=%s server=%s duration_ms=%s cached=false",
                    self.root,
                    method,
                    self.chain[idx].label,
                    self.last_duration_ms,
                )
                return BrokerRequestResult(result, self.chain[idx].label, started, workspaces_added)

            if last_empty_idx is not None:
                self.method_handler[method] = last_empty_idx
                self.last_server_label = self.chain[last_empty_idx].label
                self.last_duration_ms = int((time.monotonic() - started_at) * 1000)
                log.info(
                    "lsp.request root=%s method=%s server=%s duration_ms=%s empty_fallback=true",
                    self.root,
                    method,
                    self.chain[last_empty_idx].label,
                    self.last_duration_ms,
                )
                return BrokerRequestResult(last_empty, self.chain[last_empty_idx].label, started, workspaces_added)

            if last_err is not None:
                self.method_handler[method] = None
            raise last_err or LspError(-32601, f"{method} timed out on all servers in the chain")

    def _ensure_workspace_for_client(self, client: LspClient, uri: str) -> str | None:
        abs_file = os.path.abspath(_uri_to_path(uri))
        if any(abs_file.startswith(f + os.sep) or abs_file == f for f in client.workspace_folders):
            return None
        root = _find_project_root(abs_file, self.project_markers)
        if root and root not in client.workspace_folders:
            client.add_workspace_folder(root)
            if root not in self.pending_workspace_adds:
                self.pending_workspace_adds.append(root)
            return root
        return None

    def status(self) -> dict[str, object]:
        clients: list[dict[str, object]] = []
        for idx, cfg in enumerate(self.chain):
            client = self.clients[idx]
            clients.append(
                {
                    "label": cfg.label,
                    "command": cfg.command,
                    "args": list(cfg.args),
                    "state": "live" if client is not None else "not spawned",
                    "folders": sorted(client.workspace_folders) if client is not None else [],
                    "capabilities": sorted(client.capabilities.keys()) if client is not None else [],
                    "open_documents": len(getattr(client, "_open_documents", {})) if client is not None else 0,
                    "pid": getattr(getattr(client, "_process", None), "pid", None) if client is not None else None,
                    "request_count": self.client_request_counts[idx],
                }
            )
        return {
            "root": self.root,
            "language": self.language,
            "route_id": self.route_id,
            "route_reason": self.route_reason,
            "project_markers": list(self.project_markers),
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
            "last_method": self.last_method,
            "last_server_label": self.last_server_label,
            "last_duration_ms": self.last_duration_ms,
            "clients": clients,
            "method_handlers": {
                method: (self.chain[idx].label if idx is not None else None)
                for method, idx in self.method_handler.items()
            },
            "pending_workspace_adds": list(self.pending_workspace_adds),
            "render_memory": self.aliases.status(),
        }


class BrokerLspManager:
    def __init__(
        self,
        registry: SessionRegistry,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.registry = registry
        self.client_factory = client_factory
        self.sessions: dict[str, BrokerLspSession] = {}

    def get_or_create(
        self,
        *,
        root: str,
        config_hash_value: str,
        chain: list[ChainServer],
        server_label: str,
        prefer: dict[str, int] | None = None,
        project_markers: list[str] | None = None,
        language: str = "",
        route_id: str = "",
        route_reason: str = "",
    ) -> tuple[str, BrokerLspSession]:
        session = self.registry.get_or_create(
            SessionKey(root=os.path.abspath(root), config_hash=config_hash_value),
            server_label=server_label,
        )
        existing = self.sessions.get(session.session_id)
        if existing is not None:
            return session.session_id, existing
        lsp_session = BrokerLspSession(
            root,
            chain,
            client_factory=self.client_factory,
            prefer=prefer,
            project_markers=project_markers,
            language=language,
            route_id=route_id,
            route_reason=route_reason,
        )
        self.sessions[session.session_id] = lsp_session
        return session.session_id, lsp_session

    async def stop_session(self, session_id: str) -> bool:
        lsp_session = self.sessions.pop(session_id, None)
        if lsp_session is not None:
            await lsp_session.stop()
        return self.registry.stop(session_id) or lsp_session is not None

    async def stop_matching(self, *, root: str, config_hash_value: str) -> list[str]:
        root = os.path.abspath(root)
        stopped: list[str] = []
        for session in list(self.registry.all_sessions()):
            if session.key.root != root or session.key.config_hash != config_hash_value:
                continue
            if await self.stop_session(session.session_id):
                stopped.append(session.session_id)
        return stopped

    async def evict_idle(self, *, ttl_seconds: float, now: float | None = None) -> list[str]:
        if ttl_seconds <= 0:
            return []
        current = now if now is not None else time.time()
        evicted: list[str] = []
        for session in list(self.registry.all_sessions()):
            live = self.sessions.get(session.session_id)
            last_used = live.last_used_at if live is not None else session.last_used_at
            if current - last_used < ttl_seconds:
                continue
            if await self.stop_session(session.session_id):
                evicted.append(session.session_id)
        return evicted

    async def stop_all(self) -> None:
        for sid in list(self.sessions.keys()):
            await self.stop_session(sid)

    def lsp_status(self) -> dict[str, object]:
        sessions: list[dict[str, object]] = []
        for session in self.registry.all_sessions():
            row = session_to_dict(session)
            live = self.sessions.get(session.session_id)
            if live is not None:
                row["lsp"] = live.status()
            sessions.append(row)
        return {"session_count": len(sessions), "sessions": sessions}


__all__ = [
    "BrokerLspManager",
    "BrokerLspSession",
    "BrokerRequestResult",
    "chain_config_hash",
    "chain_from_wire",
    "chain_to_wire",
]
