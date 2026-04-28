from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from cc_lsp_now.agent_bus import AgentBus
from cc_lsp_now.agent_log import agent_log, drain_agent_messages
from cc_lsp_now.alias_coordinator import (
    AliasCoordinator,
    AliasTouchResult,
    alias_identity_to_wire,
    alias_record_from_wire,
    alias_touch_result_from_wire,
)
from cc_lsp_now.broker import BrokerError
from cc_lsp_now.broker_client import BrokerClient
from cc_lsp_now.broker_lsp import chain_config_hash, chain_to_wire
from cc_lsp_now.lsp import LspClient, LspError, file_uri
from cc_lsp_now.python_refactor import merge_workspace_edits, python_import_rewrite
from cc_lsp_now.candidate import Candidate
from cc_lsp_now.candidate_kind import CandidateKind
from cc_lsp_now.chain_server import ChainServer
from cc_lsp_now.file_move import FileMove
from cc_lsp_now.path_finder import PathDirection, PathEdge, PathNode, PathSearchResult, find_paths
from cc_lsp_now.pending_buffer import DEFAULT_STAGE_HANDLE, PendingBook, PendingBuffer
from cc_lsp_now.render_memory import AliasError, AliasIdentity, AliasKind, AliasRecord, RenderMemory
from cc_lsp_now.warmup_stats import WarmupStats

log = logging.getLogger(__name__)

mcp = FastMCP(
    "lsp-bridge",
    instructions=(
        "These LSP tools provide full language server protocol access and should be preferred "
        "over Claude Code's built-in LSP tool. They accept symbol names directly (no line/col "
        "needed), support fallback to secondary language servers, and return compact formatted output. "
        "Use these instead of the generic LSP() tool for all code intelligence operations."
    ),
)

_chain_configs: list[ChainServer] = []  # parsed from env at first use
_chain_clients: list[LspClient | None] = []  # lazy-spawned clients, same index as _chain_configs
_method_handler: dict[str, int | None] = {}  # method -> chain index; None = exhausted (all -32601)

SEVERITY_LABELS = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}

SYMBOL_KIND_LABELS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}

DISABLED_BY_DEFAULT: set[str] = set()


_last_server: str = ""
# Workspace folders added by auto-detection during the current tool call.
# The header wrapper surfaces these so the model sees when a new project was pulled in.
_added_workspaces_this_call: list[str] = []
# Workspace folders queued before any client was spawned. Flushed on first client start.
_pending_workspace_adds: list[str] = []
# Server labels that were just freshly spawned during the current tool call.
# Surfaced by the header wrapper so the model sees boot events inline.
_just_started_this_call: list[str] = []
# Per-folder files warmed up via didOpen (so we don't re-warm the same folder).
_warmed_folders: set[tuple[int, str]] = set()  # (chain_idx, folder)
# Warmup metadata for status reporting: (chain_idx, folder) -> WarmupStats
_folder_warmup_stats: dict[tuple[int, str], WarmupStats] = {}

_BROKER_DISABLED = {"0", "false", "no", "off", "disabled"}
_BROKER_REQUIRED = {"1", "true", "yes", "on", "required", "force"}
_CAPABILITY_PROBE_ENABLED = {"1", "true", "yes", "on", "enabled", "force"}
CAPABILITY_PROBE_ENV = "CC_LSP_PROBE_CAPABILITIES"

# --- Preview/confirm buffer --------------------------------------------------
#
# Several tools (rename, fix, move, ...) emit previews instead of applying
# edits immediately. The preview stages a `PendingBuffer` that the agent
# then commits via `lsp_confirm(index)`.
#
# Direct mode keeps a process-local `PendingBook` so multiple staged previews
# can coexist under different handles. Single-agent flows that don't pick a
# handle land on the `DEFAULT_STAGE_HANDLE` slot, which is exactly the
# pre-multi-slot single-slot behavior. The most recently set stage is the
# *active* stage; `lsp_confirm(0)` (no `stage` arg) commits against it so
# legacy callers keep working untouched.
#
# `_pending` is kept as a module-level mirror of the active stage so existing
# tests and call sites that read `_pending` directly stay valid. It is
# refreshed by `_set_pending` / `_clear_pending`; reads should treat
# `_pending_book` as canonical when the two could disagree.
_pending_book: PendingBook = PendingBook()
_pending: PendingBuffer | None = None

# Last semantic-grep graph, used by lsp_symbols_at("L78") to bounce from a
# compact samples field into the referenced line without repeating the path.
_last_semantic_nav: list["SemanticNavEntry"] = []
_last_semantic_nav_query: str = ""
_last_semantic_groups: list["SemanticGrepGroup"] = []
_render_memory = RenderMemory()
_client_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
_local_alias_coordinator = AliasCoordinator(_render_memory)


@dataclass
class WorkspaceApplyResult:
    affected: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def absorb(self, other: WorkspaceApplyResult) -> None:
        self.affected.extend(other.affected)
        self.renamed.extend(other.renamed)
        self.created.extend(other.created)
        self.deleted.extend(other.deleted)


@dataclass
class SemanticGrepHit:
    path: str
    line: int
    character: int
    line_text: str
    uri: str
    pos: dict


@dataclass
class SemanticGrepGroup:
    key: str
    name: str
    kind: str
    type_text: str
    definition_path: str
    definition_line: int
    definition_character: int
    hits: list[SemanticGrepHit] = field(default_factory=list)
    reference_locs: list[dict] = field(default_factory=list)
    context_symbols: list[dict] = field(default_factory=list)
    alias: str = ""


@dataclass
class SemanticNavEntry:
    path: str
    line: int
    character: int
    group_index: int
    name: str
    kind: str


@dataclass
class SemanticTarget:
    uri: str
    pos: dict
    path: str
    line: int
    character: int
    name: str = ""
    group: SemanticGrepGroup | None = None


def _set_pending(
    kind: str,
    candidates: list[Candidate],
    description: str,
    *,
    handle: str = "",
) -> PendingBuffer:
    """Stage a set of candidate WorkspaceEdits under ``handle``.

    The default ``handle`` ("") routes to ``DEFAULT_STAGE_HANDLE`` so legacy
    single-slot callers keep their meaning: each new preview replaces the
    previous *default* stage and becomes active. Named handles coexist —
    setting ``handle="rename-history-ui"`` does not disturb the default slot
    or any other named stage. The freshly set stage is always the active
    stage afterwards.

    The agent issues ``lsp_confirm(index)`` (or ``lsp_confirm(index, stage=...)``)
    to pick one candidate and apply it.
    """
    global _pending
    buffer = PendingBuffer(
        kind=kind,
        candidates=candidates,
        description=description,
        handle=handle or DEFAULT_STAGE_HANDLE,
    )
    _pending_book.set(buffer)
    _pending = _pending_book.active()
    return buffer


def _clear_pending(handle: str = "") -> None:
    """Drop a staged preview. Empty ``handle`` clears the *active* stage.

    Passing a specific handle drops only that stage and leaves the rest of
    the book intact. After clearing, ``_pending`` is refreshed to whatever
    is now active (or ``None`` if the book is empty).
    """
    global _pending
    if handle:
        _pending_book.drop(handle)
    else:
        _pending_book.clear_active()
    _pending = _pending_book.active()


def _apply_candidate(candidate: Candidate) -> tuple[int, int]:
    """Apply a single preview candidate's WorkspaceEdit.

    The candidate's ``edit`` dict holds the WorkspaceEdit. Special-cased:
    if candidate kind is ``FILE_MOVE`` with ``from_path`` / ``to_path``, the
    actual ``os.rename`` happens after edits are written — this keeps the
    import-rewrite + file-move atomic per the lsp_move flow.

    Returns (file_count, edit_count) for the summary line.
    """
    edit = candidate.edit

    applied = WorkspaceApplyResult()
    if edit.get("changes") or edit.get("documentChanges"):
        applied = _apply_workspace_edit(edit)

    edit_count = 0
    for _uri, edits in edit.get("changes", {}).items():
        edit_count += len(edits)
    for doc_change in edit.get("documentChanges", []):
        if "textDocument" in doc_change:
            edit_count += len(doc_change.get("edits", []))

    renamed: list[tuple[str, str]] = []
    created: list[str] = []
    deleted: list[str] = []

    # file_move finishes with the rename itself — after any import edits landed.
    if candidate.kind == CandidateKind.FILE_MOVE:
        if candidate.from_path and candidate.to_path:
            to_dir = os.path.dirname(os.path.abspath(candidate.to_path))
            if to_dir:
                os.makedirs(to_dir, exist_ok=True)
            os.rename(candidate.from_path, candidate.to_path)
            renamed.append((candidate.from_path, candidate.to_path))

    # file_move_batch: replay the list of renames after the single WorkspaceEdit
    # covers all import fixups. Order doesn't matter since edits are in other
    # files, and the destinations are unique per call.
    if candidate.kind == CandidateKind.FILE_MOVE_BATCH:
        for move in candidate.moves:
            if move.from_path and move.to_path:
                to_dir = os.path.dirname(os.path.abspath(move.to_path))
                if to_dir:
                    os.makedirs(to_dir, exist_ok=True)
                try:
                    os.rename(move.from_path, move.to_path)
                    renamed.append((move.from_path, move.to_path))
                except OSError as e:
                    agent_log(f"file_move_batch rename failed {move.from_path} → {move.to_path}: {e}")

    # file_create: after any side-effect edits (new imports, __init__ entries)
    # land in sibling modules, materialize the empty file itself. Wrapped in
    # try/except so a filesystem-level failure doesn't crash the confirm path —
    # the edits already wrote successfully and agent can recover manually.
    if candidate.kind == CandidateKind.FILE_CREATE:
        if candidate.from_path:
            try:
                target = Path(candidate.from_path)
                parent = target.parent
                if str(parent):
                    parent.mkdir(parents=True, exist_ok=True)
                target.touch(exist_ok=True)
                created.append(candidate.from_path)
            except OSError as e:
                agent_log(f"file_create touch failed for {candidate.from_path}: {e}")

    # file_delete: cleanup edits have fixed up imports/registrations in siblings;
    # now unlink the file itself. missing_ok so re-confirm is idempotent.
    if candidate.kind == CandidateKind.FILE_DELETE:
        if candidate.from_path:
            try:
                Path(candidate.from_path).unlink(missing_ok=True)
                deleted.append(candidate.from_path)
            except OSError as e:
                agent_log(f"file_delete unlink failed for {candidate.from_path}: {e}")

    # Notify every live server in the chain about the filesystem changes so
    # their in-memory view matches disk. Safe no-op if lists are empty.
    for client in _chain_clients:
        if client is None:
            continue
        client.notify_files_renamed([*applied.renamed, *renamed])
        client.notify_files_created([*applied.created, *created])
        client.notify_files_deleted([*applied.deleted, *deleted])
    _notify_broker_workspace_changes_sync(
        [*applied.renamed, *renamed],
        [*applied.created, *created],
        [*applied.deleted, *deleted],
    )

    affected = {*applied.affected, *created, *deleted}
    affected.update(new for _old, new in renamed)
    affected.update(new for _old, new in applied.renamed)
    return len(affected), edit_count


def _parse_replace() -> dict[str, str]:
    """Parse LSP_REPLACE into a command→command substitution map.

    Format: 'old=new,old=new'
    Example: 'basedpyright-langserver=pylance-language-server'

    Applied as a post-filter on LSP_SERVERS entries and LSP_PREFER targets —
    lets a downstream user swap a binary without rewriting the plugin's full
    config sheet.
    """
    env = os.environ.get("LSP_REPLACE", "").strip()
    if not env:
        return {}
    result: dict[str, str] = {}
    for entry in env.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        old, new = entry.split("=", 1)
        old, new = old.strip(), new.strip()
        if old and new:
            result[old] = new
    return result


def _parse_chain() -> list[ChainServer]:
    """Build the LSP chain from env vars. Index 0 = primary, 1+ = fallbacks in order.

    Preferred format (single env var):
        LSP_SERVERS="ty server;basedpyright-langserver --stdio;pyright-langserver --stdio"
        — ';'-separated servers, each is '<command> <args...>'. First = primary.

    Legacy format (still accepted if LSP_SERVERS is unset):
        LSP_COMMAND=ty LSP_ARGS=server
        LSP_FALLBACK_COMMAND=basedpyright-langserver LSP_FALLBACK_ARGS=--stdio
        LSP_FALLBACK_2_COMMAND=... LSP_FALLBACK_2_ARGS=...

    LSP_REPLACE (optional): applies after parsing. 'basedpyright-langserver=pylance-language-server'
    swaps the command everywhere it appears in the chain and in LSP_PREFER.
    """
    replace = _parse_replace()

    def _sub(cmd: str) -> str:
        return replace.get(cmd, cmd)
    servers_env = os.environ.get("LSP_SERVERS", "").strip()
    if servers_env:
        chain: list[ChainServer] = []
        for i, entry in enumerate(s.strip() for s in servers_env.split(";")):
            if not entry:
                continue
            tokens = entry.split()
            cmd, args = _sub(tokens[0]), tokens[1:]
            label = cmd if i == 0 else f"{cmd} (fallback{f' {i}' if i > 1 else ''})"
            chain.append(ChainServer(command=cmd, args=args, name=cmd, label=label))
        if not chain:
            raise RuntimeError("LSP_SERVERS is empty or malformed")
        return chain

    # Legacy path
    primary_cmd = os.environ.get("LSP_COMMAND")
    if not primary_cmd:
        raise RuntimeError("LSP_SERVERS or LSP_COMMAND environment variable is required")
    primary_cmd = _sub(primary_cmd)

    chain: list[ChainServer] = [ChainServer(
        command=primary_cmd,
        args=os.environ.get("LSP_ARGS", "").split() if os.environ.get("LSP_ARGS") else [],
        name=primary_cmd,
        label=primary_cmd,
    )]

    first_fb = os.environ.get("LSP_FALLBACK_COMMAND")
    if first_fb:
        first_fb = _sub(first_fb)
        chain.append(ChainServer(
            command=first_fb,
            args=os.environ.get("LSP_FALLBACK_ARGS", "").split() if os.environ.get("LSP_FALLBACK_ARGS") else [],
            name=first_fb,
            label=f"{first_fb} (fallback)",
        ))

    i = 2
    while True:
        cmd = os.environ.get(f"LSP_FALLBACK_{i}_COMMAND")
        if not cmd:
            break
        cmd = _sub(cmd)
        chain.append(ChainServer(
            command=cmd,
            args=os.environ.get(f"LSP_FALLBACK_{i}_ARGS", "").split() if os.environ.get(f"LSP_FALLBACK_{i}_ARGS") else [],
            name=cmd,
            label=f"{cmd} (fallback {i})",
        ))
        i += 1

    return chain


def _parse_prefer(chain: list[ChainServer]) -> dict[str, int]:
    """Parse LSP_PREFER into a method→chain-index map for pre-seeding the cache.

    Format: 'method1=serverCommand,method2=serverCommand'
    Example: 'workspace/willRenameFiles=basedpyright-langserver,textDocument/callHierarchy=basedpyright-langserver'
    If the named command isn't in the chain, the entry is ignored.
    """
    prefer_env = os.environ.get("LSP_PREFER", "").strip()
    if not prefer_env:
        return {}
    replace = _parse_replace()
    result: dict[str, int] = {}
    for entry in prefer_env.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        method, cmd = entry.split("=", 1)
        method, cmd = method.strip(), cmd.strip()
        cmd = replace.get(cmd, cmd)
        for idx, cfg in enumerate(chain):
            if cfg.command == cmd:
                result[method] = idx
                break
    return result


def _ensure_chain_configs() -> list[ChainServer]:
    global _chain_configs
    if not _chain_configs:
        _chain_configs = _parse_chain()
        _chain_clients.extend([None] * len(_chain_configs))
        _method_handler.update(_parse_prefer(_chain_configs))
    return _chain_configs


def _broker_mode() -> str:
    raw = os.environ.get("CC_LSP_BROKER", "auto").strip().lower()
    if raw in _BROKER_DISABLED:
        return "off"
    if raw in _BROKER_REQUIRED:
        return "on"
    return "auto"


def _broker_enabled() -> bool:
    if _broker_mode() == "off":
        return False
    return bool(os.environ.get("LSP_SERVERS") or os.environ.get("LSP_COMMAND"))


def _broker_base_params() -> dict[str, object]:
    chain = _ensure_chain_configs()
    language = os.environ.get("LSP_LANGUAGE", "").strip()
    return {
        "root": os.path.abspath(os.environ.get("LSP_ROOT", os.getcwd())),
        "config_hash": chain_config_hash(language, chain),
        "chain": chain_to_wire(chain),
        "server_label": chain[0].label if chain else "",
        "prefer": {
            method: idx
            for method, idx in _method_handler.items()
            if isinstance(idx, int)
        },
    }


def _broker_call_sync(method: str, params: dict[str, object]) -> object:
    with BrokerClient() as client:
        client.connect_or_start()
        return client.request(method, params)


async def _broker_call(method: str, params: dict[str, object]) -> object:
    return await asyncio.to_thread(_broker_call_sync, method, params)


def _broker_unavailable(e: BrokerError) -> bool:
    return e.code in {"broker_unreachable", "transport", "not_connected"}


def _lsp_error_from_broker(e: BrokerError) -> LspError | None:
    if not e.code.startswith("lsp:"):
        return None
    raw_code = e.code.removeprefix("lsp:")
    try:
        code = int(raw_code)
    except ValueError:
        code = -1
    return LspError(code, str(e))


def _wire_list(container: dict[str, object], key: str) -> list[object]:
    value = container.get(key, [])
    return cast(list[object], value) if isinstance(value, list) else []


def _wire_dict(container: dict[str, object], key: str) -> dict[str, object] | None:
    value = container.get(key)
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _wire_float(container: dict[str, object], key: str, default: float = 0.0) -> float:
    value = container.get(key, default)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _broker_lsp_request_sync(method: str, params: dict | None, uri: str | None) -> dict[str, object]:
    wire = _broker_base_params()
    wire.update(
        {
            "lsp_method": method,
            "lsp_params": params,
            "uri": uri or "",
            "empty_fallback_methods": sorted(_parse_empty_fallback_methods()),
        }
    )
    result = _broker_call_sync("lsp.request", wire)
    if not isinstance(result, dict):
        raise BrokerError("invalid_response", "broker lsp.request returned a non-object")
    return cast(dict[str, object], result)


async def _broker_lsp_request(method: str, params: dict | None, uri: str | None) -> dict[str, object]:
    return await asyncio.to_thread(_broker_lsp_request_sync, method, params, uri)


def _broker_render_touch_sync(identities: list[AliasIdentity]) -> AliasTouchResult:
    wire = _broker_base_params()
    wire.update(
        {
            "client_id": _client_id,
            "identities": [alias_identity_to_wire(identity) for identity in identities],
        }
    )
    result = _broker_call_sync("render.touch", wire)
    return alias_touch_result_from_wire(result)


def _broker_render_lookup_sync(token: str) -> AliasRecord | str | None:
    wire = _broker_base_params()
    wire.update({"client_id": _client_id, "token": token})
    result = _broker_call_sync("render.lookup", wire)
    if not isinstance(result, dict):
        raise BrokerError("invalid_response", "broker render.lookup returned a non-object")
    result = cast(dict[str, object], result)
    if result.get("ok") is True:
        return alias_record_from_wire(result.get("record", {}))
    error = result.get("error", "")
    message = result.get("message", "")
    if error == AliasError.INVALID.value:
        return None
    return message if isinstance(message, str) and message else "Alias is not active in broker render memory."


async def _broker_render_lookup(token: str) -> AliasRecord | str | None:
    return await asyncio.to_thread(_broker_render_lookup_sync, token)


async def _broker_lsp_status() -> dict[str, object] | None:
    if not _broker_enabled():
        return None
    try:
        result = await _broker_call("lsp.status", {})
    except BrokerError as e:
        if _broker_mode() == "on":
            raise RuntimeError(f"broker status failed: {e.code}: {e}") from None
        return None
    if isinstance(result, dict):
        return cast(dict[str, object], result)
    return None


async def _known_workspace_roots() -> list[str]:
    roots: set[str] = {os.path.abspath(os.environ.get("LSP_ROOT", os.getcwd()))}
    if _broker_enabled():
        status = await _broker_lsp_status()
        if status:
            current = _broker_base_params()
            root = current["root"]
            chash = current["config_hash"]
            for session_obj in _wire_list(status, "sessions"):
                if not isinstance(session_obj, dict):
                    continue
                session = cast(dict[str, object], session_obj)
                if session.get("root") != root or session.get("config_hash") != chash:
                    continue
                roots.add(str(session.get("root", root)))
                lsp = _wire_dict(session, "lsp")
                if lsp is None:
                    continue
                for client_obj in _wire_list(lsp, "clients"):
                    if not isinstance(client_obj, dict):
                        continue
                    client = cast(dict[str, object], client_obj)
                    folders = client.get("folders", [])
                    if not isinstance(folders, list):
                        continue
                    for folder in folders:
                        if isinstance(folder, str):
                            roots.add(folder)
    for client in _chain_clients:
        if client is not None:
            roots.update(client.workspace_folders)
    roots.update(_pending_workspace_adds)
    return sorted(roots)


async def _stored_diagnostics(uri: str) -> list[dict]:
    if _broker_enabled():
        try:
            params = _broker_base_params()
            params["uri"] = uri
            result = await _broker_call("lsp.diagnostics", params)
            if isinstance(result, dict):
                result_dict = cast(dict[str, object], result)
                items = result_dict.get("items", [])
                if isinstance(items, list):
                    return [cast(dict, item) for item in items if isinstance(item, dict)]
        except BrokerError as e:
            if _broker_mode() == "on":
                raise RuntimeError(f"broker diagnostics failed: {e.code}: {e}") from None
    primary = await _get_client(0)
    return primary.diagnostics.get(uri, [])


def _notify_broker_workspace_changes_sync(
    renamed: list[tuple[str, str]],
    created: list[str],
    deleted: list[str],
) -> None:
    if not _broker_enabled() or not (renamed or created or deleted):
        return
    params = _broker_base_params()
    params.update(
        {
            "renamed": [[old, new] for old, new in renamed],
            "created": list(created),
            "deleted": list(deleted),
        }
    )
    try:
        _broker_call_sync("lsp.notify_files", params)
    except BrokerError as e:
        if _broker_mode() == "on":
            raise
        agent_log(f"broker notify_files failed ({e.code}: {e})")


def _broker_bus_call_sync(method: str, params: dict[str, object]) -> object:
    with BrokerClient() as client:
        client.connect_or_start()
        return client.request(method, params)


async def _broker_bus_call(method: str, params: dict[str, object]) -> object:
    return await asyncio.to_thread(_broker_bus_call_sync, method, params)


# Public action set for `lsp_log`. Order is the rendered "Unknown action" hint
# so an agent can self-correct without reading the source.
_BUS_ACTIONS: tuple[str, ...] = (
    "event",
    "note",
    "ask",
    "reply",
    "recent",
    "settle",
    "precommit",
    "postcommit",
    "weather",
    "status",
)

# Reference list of canonical event kinds — used as documentation and to
# default `event_type` for the `event` action. Free-form `kind` values are
# still accepted; this set just names the hook-friendly ones from
# docs/agent-bus.md so the surface is self-describing for agents.
_BUS_KINDS: tuple[str, ...] = (
    "agent.started",
    "task.intent",
    "file.touched",
    "symbol.touched",
    "test.ran",
    "commit.created",
    "note.posted",
    "bus.ask",
    "bus.reply",
    "bus.closed",
)

# Local fallback bus used when broker mode is "off" or unreachable. The
# broker owns the durable bus when it is alive; this in-process AgentBus
# keeps lsp_log functional for solo agents and broker-down recoveries so
# the public surface stays useful even before the agent-bus harness ships.
_local_bus: AgentBus | None = None


def _get_local_bus() -> AgentBus:
    global _local_bus
    if _local_bus is None:
        _local_bus = AgentBus()
    return _local_bus


def _parse_bus_scope(value: str) -> list[str]:
    """Split a comma- or whitespace-separated agent-supplied scope list."""
    return [part.strip() for part in value.replace(",", " ").split() if part.strip()]


def _parse_bus_duration(value: str, *, default: float = 180.0) -> float | str:
    """Parse a "30s", "3m", "1h" timeout. Returns seconds or an error string.

    The string variant lets ``lsp_log`` surface a parse error inline instead
    of raising into the MCP transport — agents read the error and self-correct.
    Empty input falls back to ``default`` (180s, matching the Wave 1 spec).
    """
    raw = ("" if value is None else str(value)).strip().lower()
    if not raw:
        return default
    scale = 1.0
    body = raw
    if raw.endswith("ms"):
        scale = 0.001
        body = raw[:-2]
    elif raw.endswith("s"):
        body = raw[:-1]
    elif raw.endswith("m"):
        scale = 60.0
        body = raw[:-1]
    elif raw.endswith("h"):
        scale = 3600.0
        body = raw[:-1]
    try:
        seconds = float(body) * scale
    except ValueError:
        return f"timeout {value!r} not parseable; expected forms like 30s, 3m, 1h."
    if seconds < 0:
        return f"timeout {value!r} must be non-negative."
    return seconds


def _bus_params(
    *,
    message: str,
    kind: str = "",
    event_type: str = "",
    files: str,
    symbols: str,
    aliases: str,
    question_id: str,
    timeout: str,
    status: str,
    targets: str,
    commit: str = "",
    action: str = "",
    metadata: str = "",
) -> dict[str, object]:
    """Build the workspace-stamped payload for any ``bus.*`` method.

    ``kind`` / ``event_type`` is the canonical event label
    (``file.touched``, ``test.ran``, …). For ``action="event"`` it becomes the stored
    ``event_type``; for the other actions it lives under ``metadata.kind``
    so digests and recent activity can still group by hook source.
    ``commit`` similarly lands in ``metadata.commit`` so post-commit
    digests can name the SHA without inflating the top-level shape.
    """
    payload: dict[str, object] = {
        "workspace_root": os.path.abspath(os.environ.get("LSP_ROOT", os.getcwd())),
        "agent_id": os.environ.get("CC_LSP_AGENT_ID", _client_id),
        "session_id": _client_id,
        "message": message,
        "files": _parse_bus_scope(files),
        "symbols": _parse_bus_scope(symbols),
        "aliases": _parse_bus_scope(aliases),
    }
    chosen_kind = kind or event_type
    if chosen_kind and action == "event":
        payload["event_type"] = chosen_kind
    if question_id:
        payload["id"] = question_id
        payload["question_id"] = question_id
    if timeout:
        payload["timeout"] = timeout
    meta: dict[str, object] = _parse_bus_metadata(metadata)
    if chosen_kind and action != "event":
        meta["kind"] = chosen_kind
    if status:
        meta["status"] = status
    if targets:
        meta["targets"] = _parse_bus_scope(targets)
    if commit:
        meta["commit"] = commit
    if meta:
        payload["metadata"] = meta
    return payload


def _parse_bus_metadata(value: str) -> dict[str, object]:
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {"value": parsed}


def _local_bus_dispatch(action: str, params: dict[str, object]) -> dict[str, object] | str:
    """Run an action against the in-process AgentBus.

    Returns the bus result dict on success, or a string error so the MCP
    surface can stay defensive (no exception bubbles out of lsp_log).
    """
    bus = _get_local_bus()
    try:
        if action == "event":
            return bus.event(params)
        if action == "note":
            return bus.note(params)
        if action == "ask":
            return bus.ask(params)
        if action == "reply":
            return bus.reply(params)
        if action == "recent":
            return bus.recent(params)
        if action == "settle":
            return bus.settle(params)
        if action == "precommit":
            return bus.precommit(params)
        if action == "postcommit":
            return bus.postcommit(params)
        if action == "weather":
            return bus.weather(params)
        if action == "status":
            return bus.status()
    except ValueError as e:
        return f"bus {action} failed: {e}"
    return f"local bus has no handler for action: {action!r}"


def _render_bus_result(action: str, result: dict[str, object]) -> str:
    if action == "status":
        return _render_bus_status(result)
    if action == "weather":
        return _render_bus_weather(result)
    if action == "recent":
        return _render_bus_recent(result)
    if action == "settle":
        return _render_bus_settle(result)
    if action == "precommit":
        return _render_bus_precommit(result)
    if action in {"event", "note", "postcommit"}:
        event = _wire_dict(result, "event")
        return _render_logged_event(event) if event else "logged event"
    if action == "ask":
        question = _wire_dict(result, "question")
        event = _wire_dict(result, "event")
        if question:
            qid = question.get("question_id", "")
            left = _wire_float(question, "seconds_left")
            msg = question.get("message", "")
            scope = _render_bus_scope(question)
            return "\n".join([
                f"opened {qid} ({left:.0f}s)",
                f"question: {msg}",
                scope,
                "reply: lsp_log(action='reply', id='%s', message='...')" % qid,
            ]).strip()
        return _render_logged_event(event) if event else "opened question"
    if action == "reply":
        event = _wire_dict(result, "event")
        question = _wire_dict(result, "question")
        qid = question.get("question_id", "") if question else ""
        return f"reply recorded for {qid}: {_event_label(event)}"
    return json.dumps(result, indent=2, sort_keys=True)


def _render_bus_status(result: dict[str, object]) -> str:
    last = str(result.get("last_event_id", ""))
    if last and not last.startswith("E"):
        last = f"E{last}"
    return (
        f"bus events={result.get('event_count', 0)} "
        f"last={last or 'E0'} "
        f"open_questions={result.get('open_question_count', 0)}"
    )


def _render_bus_weather(result: dict[str, object]) -> str:
    lines = [f"workspace: {result.get('workspace_root', '')}"]
    questions = _wire_list(result, "open_questions")
    lines.append(f"open questions: {len(questions)}")
    for q_obj in questions[:5]:
        if isinstance(q_obj, dict):
            q = cast(dict[str, object], q_obj)
            lines.append(
                f"  {q.get('question_id', '')} {_wire_float(q, 'seconds_left'):.0f}s "
                f"{q.get('message', '')}"
            )
    recent = _wire_list(result, "recent")
    lines.append(f"recent: {len(recent)}")
    for e_obj in recent[-5:]:
        if isinstance(e_obj, dict):
            lines.append(f"  {_event_label(cast(dict[str, object], e_obj))}")
    return "\n".join(lines)


def _render_bus_recent(result: dict[str, object]) -> str:
    events = _wire_list(result, "events")
    if not events:
        return "recent: (none)"
    lines = ["recent:"]
    for e_obj in events:
        if isinstance(e_obj, dict):
            lines.append(f"  {_event_label(cast(dict[str, object], e_obj))}")
    if result.get("truncated"):
        lines.append("  ... truncated; narrow scope or raise limit")
    return "\n".join(lines)


def _render_bus_settle(result: dict[str, object]) -> str:
    closed = _wire_list(result, "closed")
    if not closed:
        return "settle: no expired questions"
    lines = ["closed questions:"]
    for d_obj in closed:
        if not isinstance(d_obj, dict):
            continue
        digest = cast(dict[str, object], d_obj)
        question = _wire_dict(digest, "question") or {}
        lines.append(f"  {question.get('question_id', '')}: {question.get('message', '')}")
        for e_obj in _wire_list(digest, "events")[-5:]:
            if isinstance(e_obj, dict):
                lines.append(f"    {_event_label(cast(dict[str, object], e_obj))}")
    return "\n".join(lines)


def _render_bus_precommit(result: dict[str, object]) -> str:
    recent = _wire_list(result, "recent")
    suggested = _wire_list(result, "suggested")
    lines = ["precommit weather:"]
    if recent:
        for e_obj in recent[-8:]:
            if isinstance(e_obj, dict):
                lines.append(f"  {_event_label(cast(dict[str, object], e_obj))}")
    else:
        lines.append("  (no related recent bus activity)")
    if suggested:
        lines.append("suggested checks:")
        for item in suggested:
            lines.append(f"  {item}")
    return "\n".join(lines)


def _render_logged_event(event: dict[str, object] | None) -> str:
    if not event:
        return "logged event"
    return f"logged {_event_label(event)}"


def _event_label(event: dict[str, object] | None) -> str:
    if not event:
        return "(unknown event)"
    eid = event.get("event_id", "")
    event_type = event.get("event_type", "") or event.get("kind", "")
    message = event.get("message", "")
    scope = _render_bus_scope(event)
    event_id = str(eid)
    if event_id and not event_id.startswith("E"):
        event_id = f"E{event_id}"
    head = f"{event_id} {event_type}".strip()
    if message:
        head += f" {message}"
    if scope:
        head += f" [{scope}]"
    return _compact_line(head, 220)


def _render_bus_scope(item: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("files", "symbols", "aliases"):
        values = item.get(key, [])
        if isinstance(values, list) and values:
            parts.append(f"{key}=" + ",".join(str(v) for v in values[:5]))
    return " ".join(parts)


# Project-root detection. Plugins contribute markers via LSP_PROJECT_MARKERS.
# Default: .git alone (universal). Python plugins add pyproject.toml etc.
def _project_markers() -> list[str]:
    raw = os.environ.get("LSP_PROJECT_MARKERS", ".git").strip()
    return [m.strip() for m in raw.split(",") if m.strip()]


def _find_project_root(file_path: str) -> str | None:
    """Walk up from file_path looking for a project marker. Returns absolute path or None."""
    markers = _project_markers()
    if not markers:
        return None
    path = Path(file_path).resolve()
    for parent in [path, *path.parents]:
        for marker in markers:
            if (parent / marker).exists():
                return str(parent)
    return None


def _parse_empty_fallback_methods() -> set[str]:
    """Methods where an empty result from one server should route to the next.

    Some methods (references, workspace symbols) ask about 'everywhere this
    appears' — an empty result usually means 'I didn't see it' rather than
    'it truly isn't there'. These methods benefit from falling through to
    the next server when the current one returns empty.

    Methods like definition/hover legitimately return empty (e.g. at a
    whitespace position), so they're NOT in the default set.
    """
    default = "textDocument/references,workspace/symbol"
    raw = os.environ.get("LSP_EMPTY_FALLBACK", default).strip()
    if not raw:
        return set()
    return {m.strip() for m in raw.split(",") if m.strip()}


def _is_empty_result(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, (list, dict, str)) and len(result) == 0:
        return True
    return False


def _parse_warmup_patterns() -> list[str]:
    raw = os.environ.get("LSP_WARMUP_PATTERNS", "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _warmup_max_files() -> int:
    try:
        return max(0, int(os.environ.get("LSP_WARMUP_MAX_FILES", "500")))
    except ValueError:
        return 500


_WARMUP_ALWAYS_EXCLUDE = {".venv", "venv", "__pycache__", "node_modules", ".git", ".claude"}


def _parse_warmup_exclude() -> set[str]:
    raw = os.environ.get("LSP_WARMUP_EXCLUDE", "").strip()
    custom = {p.strip() for p in raw.split(",") if p.strip()}
    return _WARMUP_ALWAYS_EXCLUDE | custom


def _is_excluded(path: Path, root: Path, exclude_names: set[str]) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part in exclude_names for part in rel.parts)


async def _warmup_folder(client: LspClient, folder: str) -> int:
    """Bulk-didOpen files matching LSP_WARMUP_PATTERNS under folder. Returns files warmed."""
    patterns = _parse_warmup_patterns()
    if not patterns:
        return 0
    limit = _warmup_max_files()
    if limit <= 0:
        return 0
    exclude_names = _parse_warmup_exclude()
    count = 0
    root = Path(folder)
    if not root.is_dir():
        return 0
    seen: set[str] = set()
    for pattern in patterns:
        try:
            matches = list(root.rglob(pattern))
        except OSError:
            continue
        for fp in matches:
            if count >= limit:
                return count
            if _is_excluded(fp, root, exclude_names):
                continue
            try:
                resolved = str(fp.resolve())
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                await client.ensure_document(file_uri(resolved))
                count += 1
            except Exception:
                pass
    return count


async def _maybe_warmup(client: LspClient, chain_idx: int, folder: str) -> int:
    """Warm up a folder only if not already warmed. Silent on failure."""
    key = (chain_idx, folder)
    if key in _warmed_folders:
        return 0
    _warmed_folders.add(key)
    n = await _warmup_folder(client, folder)
    _folder_warmup_stats[key] = WarmupStats(count=n, timestamp=time.time())
    if n > 0:
        label = _chain_configs[chain_idx].label
        agent_log(f"Warmed {n} files in {folder} for {label}")
    return n


async def _ensure_workspace_for(uri: str | None) -> None:
    """If the file is outside all known workspace folders, find its project root and add it."""
    if not uri:
        return
    file_path = _uri_to_path(uri)
    abs_file = os.path.abspath(file_path)
    for idx in range(len(_chain_configs)):
        client = _chain_clients[idx]
        if client is None:
            continue  # will be handled on next request when lazy-spawned
        if any(abs_file.startswith(f + os.sep) or abs_file == f for f in client.workspace_folders):
            continue
        root = _find_project_root(abs_file)
        if root and root not in client.workspace_folders:
            client.add_workspace_folder(root)
            if root not in _added_workspaces_this_call:
                _added_workspaces_this_call.append(root)
            await _maybe_warmup(client, idx, root)


async def _get_client(idx: int) -> LspClient:
    _ensure_chain_configs()
    if _chain_clients[idx] is None:
        cfg = _chain_configs[idx]
        root = os.environ.get("LSP_ROOT", os.getcwd())
        client = LspClient([cfg.command, *cfg.args], root)
        await client.start()
        _chain_clients[idx] = client
        if cfg.label not in _just_started_this_call:
            _just_started_this_call.append(cfg.label)
        # Flush any pending workspace adds that were queued before this client existed
        for pending in list(_pending_workspace_adds):
            if client.add_workspace_folder(pending):
                await _maybe_warmup(client, idx, pending)
        # Warm up the primary root too
        await _maybe_warmup(client, idx, client._root_path)
    client = _chain_clients[idx]
    assert client is not None
    return client


_SLOW_METHODS: set[str] = {
    "workspace/willRenameFiles",
}
_SLOW_TIMEOUT = 300.0

async def _request(method: str, params: dict | None, *, uri: str | None = None) -> Any:
    """Route a request through the chain. Caches which server handles each method."""
    global _last_server
    _ensure_chain_configs()
    if _broker_enabled():
        try:
            forwarded = await _broker_lsp_request(method, params, uri)
            label = forwarded.get("server_label", "")
            if isinstance(label, str):
                _last_server = label
            for item in _wire_list(forwarded, "started"):
                if isinstance(item, str) and item not in _just_started_this_call:
                    _just_started_this_call.append(item)
            for item in _wire_list(forwarded, "workspaces_added"):
                if isinstance(item, str) and item not in _added_workspaces_this_call:
                    _added_workspaces_this_call.append(item)
            return forwarded.get("result")
        except BrokerError as e:
            lsp_error = _lsp_error_from_broker(e)
            if lsp_error is not None:
                raise lsp_error
            if _broker_mode() == "on" or not _broker_unavailable(e):
                raise RuntimeError(f"broker request failed: {e.code}: {e}") from None
            agent_log(f"broker unavailable ({e.code}: {e}); falling back to direct LSP")

    empty_fallback = _parse_empty_fallback_methods()

    timeout = _SLOW_TIMEOUT if method in _SLOW_METHODS else 30.0

    # Fast path: method already resolved to a specific chain index
    if method in _method_handler:
        idx = _method_handler[method]
        if idx is None:
            raise LspError(-32601, f"{method} not supported by any server in the chain")
        client = await _get_client(idx)
        await client.resync_open_documents()
        await _ensure_workspace_for(uri)
        if uri:
            await client.ensure_document(uri)
        _last_server = _chain_configs[idx].label
        try:
            return await client.request(method, params, timeout=timeout)
        except asyncio.TimeoutError:
            agent_log(f"{_chain_configs[idx].label} timed out on {method} (cached), invalidating")
            del _method_handler[method]
            # Fall through to cold path

    # Cold path: try each server in order
    last_err: LspError | None = None
    last_empty: Any = None
    last_empty_idx: int | None = None

    for idx in range(len(_chain_configs)):
        client = await _get_client(idx)
        await client.resync_open_documents()
        await _ensure_workspace_for(uri)
        if uri:
            await client.ensure_document(uri)
        try:
            result = await client.request(method, params, timeout=timeout)
        except asyncio.TimeoutError:
            agent_log(f"{_chain_configs[idx].label} timed out on {method} after {timeout}s, trying next")
            continue
        except LspError as e:
            if e.code != -32601:
                raise
            last_err = e
            continue

        # Empty-fallback: method opted in + result is empty + more servers available
        is_last = idx == len(_chain_configs) - 1
        if (method in empty_fallback and _is_empty_result(result) and not is_last):
            last_empty = result
            last_empty_idx = idx
            log.info(
                "%s returned empty on %s, trying next server",
                _chain_configs[idx].label, method,
            )
            continue

        _method_handler[method] = idx
        _last_server = _chain_configs[idx].label
        if idx > 0:
            label = _chain_configs[idx].label
            agent_log(f"Routing {method} to {label}")
        return result

    # All servers tried. If one returned an empty result (and no server had an actual
    # match), return the empty result rather than raising — downstream tool formats
    # it as "no results".
    if last_empty_idx is not None:
        _method_handler[method] = last_empty_idx
        _last_server = _chain_configs[last_empty_idx].label
        return last_empty

    # Only cache as unsupported if we got actual -32601 errors, not just timeouts
    if last_err is not None:
        _method_handler[method] = None
    raise last_err or LspError(-32601, f"{method} timed out on all servers in the chain")


def _header(method: str) -> str:
    return f"[{_last_server} {method}]"


# --- Formatting helpers ---


def _pos(line: int, col: int) -> dict:
    return {"line": line - 1, "character": col - 1}


def _uri_to_path(uri: str) -> str:
    return uri.removeprefix("file://") if uri.startswith("file://") else uri


def _loc_str(loc: dict) -> str:
    path = _uri_to_path(loc.get("uri", ""))
    start = loc.get("range", {}).get("start", {})
    line = start.get("line", 0) + 1
    return f"{line}  {path}"


def _range_str(r: dict) -> str:
    s = r.get("start", {})
    e = r.get("end", {})
    sl, sc = s.get("line", 0) + 1, s.get("character", 0) + 1
    el, ec = e.get("line", 0) + 1, e.get("character", 0) + 1
    if sl == el:
        return f"L{sl}:{sc}-{ec}"
    return f"L{sl}:{sc}-L{el}:{ec}"


def _line_snapshot(file_path: str, pos: dict) -> str:
    """One-line context for position-sensitive failures."""
    line_idx = pos.get("line", 0)
    char_idx = pos.get("character", 0)
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        line_text = lines[line_idx] if 0 <= line_idx < len(lines) else ""
    except OSError:
        line_text = ""
    caret = " " * max(char_idx, 0) + "^"
    return f"{file_path}:{line_idx + 1}:{char_idx + 1}\n  {line_text}\n  {caret}"


def _active_workspace_summary() -> str:
    summaries: list[str] = []
    for idx, client in enumerate(_chain_clients):
        if client is None:
            continue
        label = _chain_configs[idx].label if idx < len(_chain_configs) else f"server[{idx}]"
        folders = ", ".join(sorted(client.workspace_folders))
        summaries.append(f"{label}: {folders}")
    return "\n".join(summaries) if summaries else "(no active LSP clients)"


def _diagnostic_snapshot(uri: str, pos: dict) -> str:
    target_line = pos.get("line", 0)
    lines: list[str] = []
    for idx, client in enumerate(_chain_clients):
        if client is None:
            continue
        label = _chain_configs[idx].label if idx < len(_chain_configs) else f"server[{idx}]"
        for diag in client.diagnostics.get(uri, []):
            rng = diag.get("range", {})
            start = rng.get("start", {})
            end = rng.get("end", {})
            if start.get("line", -1) <= target_line <= end.get("line", -1):
                severity = _severity_label(diag.get("severity", 0))
                message = diag.get("message", "")
                lines.append(f"{label}: {severity} {_range_str(rng)} {message}")
    return "\n".join(lines) if lines else "(none on target line)"


def _raw_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return repr(value)


def _compact_line(text: str, limit: int = 180) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _py_index_to_utf16_units(text: str, py_index: int) -> int:
    """Convert a Python string index into the UTF-16 column LSP expects."""
    return len(text[:py_index].encode("utf-16-le")) // 2


def _severity_label(n: int) -> str:
    return SEVERITY_LABELS.get(n, f"Unknown({n})")


def _symbol_kind_label(n: int) -> str:
    return SYMBOL_KIND_LABELS.get(n, f"Unknown({n})")


def _normalize_locations(result: dict | list | None) -> list[str]:
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]
    return [_loc_str(loc) for loc in result]


def _format_outline_tree(sym: dict, indent: int = 0) -> list[str]:
    """One-line breadcrumb per symbol; nesting shown by indent, line by ``Lxx``.

    Output shape matches the rest of the workflow surface (see ``lsp_grep``):
    leading ``Lxx`` so a model can hop straight from outline to
    ``lsp_symbols_at("Lxx")`` without re-grepping the file.
    """
    kind = _symbol_kind_label(sym.get("kind", 0))
    name = sym.get("name", "")
    loc = sym.get("location", sym.get("range", {}))
    if "uri" in loc:
        line = loc.get("range", {}).get("start", {}).get("line", 0) + 1
    else:
        line = loc.get("start", {}).get("line", 0) + 1
    pad = "  " * indent
    lines = [f"L{line}  {pad}{kind} {name}"]
    for child in sym.get("children", []):
        lines.extend(_format_outline_tree(child, indent + 1))
    return lines


def _range_contains_line(r: dict, line: int) -> bool:
    start = r.get("start", {})
    end = r.get("end", {})
    return start.get("line", -1) <= line <= end.get("line", -1)


def _symbols_on_line(symbols: list[dict], line: int) -> list[tuple[int, dict, str, str]]:
    """Return semantic symbol positions that are declared on or enclosing a line.

    Each tuple is (rank, position, kind, name). Lower rank is better.
    """
    results: list[tuple[int, dict, str, str]] = []
    for sym in symbols:
        sel = sym.get("selectionRange", sym.get("range", sym.get("location", {}).get("range", {})))
        rng = sym.get("range", sym.get("location", {}).get("range", {}))
        sel_start = sel.get("start", {})
        kind = _symbol_kind_label(sym.get("kind", 0))
        name = sym.get("name", "")

        if sel_start.get("line") == line:
            results.append((0, sel_start, kind, name))
        elif _range_contains_line(rng, line):
            results.append((1, sel_start, kind, name))

        for child in sym.get("children", []):
            results.extend(_symbols_on_line([child], line))
    return sorted(
        results,
        key=lambda h: (
            h[0],
            abs(h[1].get("line", line) - line),
            h[1].get("character", 0),
        ),
    )


_LINE_POSITION_SKIP_WORDS = {
    "abstract",
    "as",
    "async",
    "await",
    "base",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "default",
    "def",
    "delegate",
    "do",
    "else",
    "enum",
    "event",
    "explicit",
    "extern",
    "false",
    "False",
    "finally",
    "fixed",
    "from",
    "for",
    "foreach",
    "get",
    "if",
    "implicit",
    "import",
    "in",
    "interface",
    "internal",
    "is",
    "lambda",
    "lock",
    "namespace",
    "new",
    "None",
    "nonlocal",
    "null",
    "operator",
    "out",
    "override",
    "pass",
    "params",
    "partial",
    "private",
    "protected",
    "public",
    "readonly",
    "record",
    "ref",
    "return",
    "sealed",
    "set",
    "sizeof",
    "static",
    "struct",
    "switch",
    "this",
    "throw",
    "true",
    "True",
    "try",
    "typeof",
    "unsafe",
    "using",
    "var",
    "virtual",
    "void",
    "volatile",
    "while",
    "with",
    "yield",
}


def _fallback_position_on_line(file_path: str, line: int) -> dict:
    """Pick a useful token when the caller provides only a line number.

    LSP rename/prepareRename usually requires the cursor to sit on the symbol
    token. Column 0 often points at whitespace or a modifier, which collapses
    into an unhelpful "Cannot rename at this position." Use document symbols
    when available; this fallback keeps line-only calls usable for servers that
    do not return symbols.
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        line_text = text.splitlines()[line]
    except (IndexError, OSError):
        return {"line": line, "character": 0}

    # Constructors, methods, and invocations: prefer the token immediately
    # before an opening paren.
    paren_match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>\n]+>)?\s*\(", line_text)
    if paren_match and paren_match.group(1) not in _LINE_POSITION_SKIP_WORDS:
        return {"line": line, "character": paren_match.start(1)}

    tokens = list(re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", line_text))
    for idx, token in enumerate(tokens):
        word = token.group(0)
        if word in {"class", "struct", "interface", "enum", "record", "delegate"} and idx + 1 < len(tokens):
            return {"line": line, "character": tokens[idx + 1].start()}

    for token in tokens:
        if token.group(0) not in _LINE_POSITION_SKIP_WORDS:
            return {"line": line, "character": token.start()}
    return {"line": line, "character": 0}


async def _position_for_line(file_path: str, uri: str, line: int) -> dict:
    line_idx = line - 1
    try:
        doc_symbols = await _request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        }, uri=uri)
    except LspError:
        doc_symbols = None

    if doc_symbols:
        hits = _symbols_on_line(doc_symbols, line_idx)
        if hits:
            _rank, pos, _kind, _name = min(
                hits,
                key=lambda h: (h[0], abs(h[1].get("line", line_idx) - line_idx), h[1].get("character", 0)),
            )
            return pos

    return _fallback_position_on_line(file_path, line_idx)


async def _prepare_rename_probe(uri: str, pos: dict) -> tuple[bool, Any]:
    try:
        result = await _request("textDocument/prepareRename", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        return True, result
    except (LspError, asyncio.TimeoutError, ConnectionError) as e:
        return False, str(e)


async def _rename_trace(
    *,
    file_path: str,
    uri: str,
    pos: dict,
    new_name: str,
    operation: str = "rename",
    rename_result: Any = None,
    error: Exception | None = None,
    include_prepare: bool = True,
) -> str:
    lines = [
        "Rename trace:",
        f"  server: {_last_server or '(unknown)'}",
        f"  newName: {new_name!r}",
        "  target:",
        *[f"    {line}" for line in _line_snapshot(file_path, pos).splitlines()],
        "  diagnostics on target line:",
        *[f"    {line}" for line in _diagnostic_snapshot(uri, pos).splitlines()],
        "  active workspaces:",
        *[f"    {line}" for line in _active_workspace_summary().splitlines()],
    ]
    if include_prepare:
        ok, prepare = await _prepare_rename_probe(uri, pos)
        label = "raw prepareRename response" if ok else "prepareRename error"
        lines.append(f"  {label}:")
        lines.extend(f"    {line}" for line in _raw_json(prepare).splitlines())
    if error is not None:
        lines.append(f"  {operation} error:")
        lines.extend(f"    {line}" for line in str(error).splitlines())
    else:
        lines.append(f"  raw {operation} response:")
        lines.extend(f"    {line}" for line in _raw_json(rename_result).splitlines())
    return "\n".join(lines)


# --- Symbol resolution ---


class AmbiguousSymbol(Exception):
    def __init__(self, matches: list[tuple[int, str, str]]):
        self.matches = matches


class AmbiguousFilePath(ValueError):
    def __init__(self, query: str, matches: list[str]):
        super().__init__(query)
        self.query = query
        self.matches = matches

    def __str__(self) -> str:
        return _file_path_error(self)


def _file_path_error(e: AmbiguousFilePath) -> str:
    lines = [f"Multiple files match {e.query!r} — pass a more specific path:"]
    lines.extend(f"  {match}" for match in e.matches[:50])
    if len(e.matches) > 50:
        lines.append(f"  ... {len(e.matches) - 50} more")
    return "\n".join(lines)


def _file_search_roots() -> list[Path]:
    roots: list[Path] = []
    for client in _chain_clients:
        if client is not None:
            roots.extend(Path(folder) for folder in client.workspace_folders)
    roots.extend(Path(path) for path in _pending_workspace_adds)
    roots.append(Path(os.environ.get("LSP_ROOT", os.getcwd())))
    roots.append(Path(os.getcwd()))

    seen: set[str] = set()
    resolved_roots: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        resolved_roots.append(resolved)
    return resolved_roots


def _find_file_by_name(query: str) -> list[str]:
    exclude_names = _parse_warmup_exclude()
    matches: list[str] = []
    seen: set[str] = set()
    for root in _file_search_roots():
        try:
            candidates = [root] if root.is_file() else root.rglob(query)
        except OSError:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            if path.name != query:
                continue
            parent = root.parent if root.is_file() else root
            if _is_excluded(path, parent, exclude_names):
                continue
            try:
                resolved = str(path.resolve())
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            matches.append(resolved)
    return sorted(matches)


def _resolve_file_path(file_path: str, *, must_exist: bool = True) -> str:
    raw = file_path.strip()
    if not raw:
        raise ValueError("File path is required.")

    path = Path(raw).expanduser()
    if path.exists():
        return str(path.resolve())

    has_path_part = path.is_absolute() or len(path.parts) > 1
    if has_path_part:
        if must_exist:
            raise ValueError(f"File not found: {raw}")
        return str(path.resolve())

    matches = _find_file_by_name(raw)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousFilePath(raw, matches)
    if must_exist:
        raise ValueError(f"File {raw!r} not found under active workspaces.")
    return str(path.resolve())


async def _resolve(
    file_path: str,
    symbol: str = "",
    line: int = 0,
) -> tuple[str, dict]:
    """Resolve a symbol name or line number to a URI + LSP position.

    Resolution pipeline:
    1. If only line given → use document symbols/token fallback
    2. If symbol given → documentSymbol search, then text fallback
    3. Multiple matches + line → disambiguate by closest line
    4. Multiple matches, no line → raise AmbiguousSymbol with all matches
    """
    file_path = _resolve_file_path(file_path)
    uri = file_uri(file_path)

    if not symbol and line > 0:
        return uri, await _position_for_line(file_path, uri, line)

    if not symbol:
        raise ValueError("Provide 'symbol' name or 'line' number.")

    # 1. Try documentSymbol for semantic resolution
    await _request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}, uri=uri)
    # ensure_document was called by _request, now query symbols
    try:
        doc_symbols = await _request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
    except LspError:
        doc_symbols = None

    if doc_symbols:
        hits = _search_symbol_tree(doc_symbols, symbol)
        if len(hits) == 1:
            return uri, _refine_column(file_path, hits[0][1], symbol)
        if hits and line > 0:
            best = min(hits, key=lambda h: abs(h[0] - (line - 1)))
            return uri, _refine_column(file_path, best[1], symbol)
        if hits:
            raise AmbiguousSymbol([
                (h[0] + 1, h[2], h[3]) for h in hits
            ])

    # 2. Fallback: text search with word boundaries
    text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    text_hits: list[tuple[int, dict, str]] = []
    for i, file_line in enumerate(text.splitlines()):
        m = pattern.search(file_line)
        if m:
            text_hits.append((i, {"line": i, "character": m.start()}, file_line.strip()))

    if len(text_hits) == 1:
        return uri, text_hits[0][1]
    if text_hits and line > 0:
        best = min(text_hits, key=lambda h: abs(h[0] - (line - 1)))
        return uri, best[1]
    if text_hits:
        raise AmbiguousSymbol([
            (h[0] + 1, "", h[2]) for h in text_hits
        ])

    raise ValueError(f"Symbol {symbol!r} not found in {file_path}")


async def _resolve_symbol_targets(file_path: str, symbol: str) -> list[SemanticTarget]:
    """Resolve every same-file symbol match for read-only fan-out tools.

    Most target-taking tools need exactly one semantic node, so
    ``_resolve_semantic_target`` should keep returning a disambiguation error
    on multiple matches. Graph inspection is different: when an agent asks for
    ``SelectArtifact`` in a file, the overloads / wrappers / relative helpers
    are often one cognitive function. Read-only tools such as ``lsp_calls`` can
    expand all concrete matches and show the whole local neighborhood instead
    of forcing a manual line-by-line retry loop.
    """
    resolved_path = _resolve_file_path(file_path)
    uri = file_uri(resolved_path)
    await _request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}, uri=uri)
    try:
        doc_symbols = await _request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
    except LspError:
        doc_symbols = None

    targets: list[SemanticTarget] = []
    if doc_symbols:
        seen: set[tuple[int, int, str]] = set()
        for line0, pos, _kind, name in _search_symbol_tree(doc_symbols, symbol):
            refined = _refine_column(resolved_path, pos, symbol)
            key = (refined.get("line", line0), refined.get("character", 0), name)
            if key in seen:
                continue
            seen.add(key)
            targets.append(_target_from_resolved_uri(uri, refined, name))
        if targets:
            return targets

    text = Path(resolved_path).read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    for i, file_line in enumerate(text.splitlines()):
        m = pattern.search(file_line)
        if not m:
            continue
        pos = {"line": i, "character": m.start()}
        targets.append(_target_from_resolved_uri(uri, pos, symbol))
    return targets


def _search_symbol_tree(
    symbols: list[dict], query: str
) -> list[tuple[int, dict, str, str]]:
    """Search documentSymbol tree. Returns [(line_0based, position, kind_label, name)]."""
    results: list[tuple[int, dict, str, str]] = []
    for sym in symbols:
        name = sym.get("name", "")
        if query in name:
            r = sym.get("selectionRange", sym.get("range", sym.get("location", {}).get("range", {})))
            start = r.get("start", {})
            line = start.get("line", 0)
            kind = _symbol_kind_label(sym.get("kind", 0))
            results.append((line, start, kind, name))
        for child in sym.get("children", []):
            results.extend(_search_symbol_tree([child], query))
    return results


def _refine_column(file_path: str, pos: dict, symbol: str) -> dict:
    """If position is at column 0, search the line text for the exact symbol name."""
    if pos.get("character", 0) != 0:
        return pos
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        target_line = text.splitlines()[pos.get("line", 0)]
        m = re.search(r'\b' + re.escape(symbol) + r'\b', target_line)
        if m:
            return {"line": pos["line"], "character": m.start()}
    except (IndexError, OSError):
        pass
    return pos


def _ambiguous_msg(e: AmbiguousSymbol) -> str:
    lines = ["Multiple matches — pass line= to disambiguate:"]
    for line_n, kind, text in e.matches:
        parts = [f"  {line_n}"]
        if kind:
            parts.append(f"  {kind}")
        parts.append(f"  {text}")
        lines.append("".join(parts))
    return "\n".join(lines)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _semantic_grep_max_files() -> int:
    try:
        return max(1, int(os.environ.get("LSP_GREP_MAX_FILES", "2000")))
    except ValueError:
        return 2000


def _semantic_grep_patterns(pattern: str = "") -> list[str]:
    if pattern:
        return [pattern]
    return _parse_warmup_patterns() or ["**/*"]


def _candidate_scan_paths(root: Path, pattern: str, max_files: int) -> list[str]:
    """Return readable candidate files under ``root`` for semantic grep.

    `lsp_grep` starts as text search plus semantic regrouping, so file scanning
    stays deliberately conservative: respect warmup globs/excludes, skip large
    blobs, and let the LSP decide identity after a token is found.
    """
    if max_files <= 0:
        return []
    if root.is_file():
        return [str(root.resolve())]
    if not root.is_dir():
        return []

    exclude_names = _parse_warmup_exclude()
    seen: set[str] = set()
    paths: list[str] = []
    for glob_pattern in _semantic_grep_patterns(pattern):
        try:
            matches = root.rglob(glob_pattern)
        except OSError:
            continue
        for path in matches:
            if len(paths) >= max_files:
                return paths
            if not path.is_file() or _is_excluded(path, root, exclude_names):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                resolved = str(path.resolve())
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(resolved)
    return paths


def _semantic_grep_paths(file_path: str, pattern: str, roots: list[str], max_files: int) -> list[str]:
    if file_path:
        paths: list[str] = []
        for raw in (p.strip() for p in file_path.split(",")):
            if not raw:
                continue
            resolved = _resolve_file_path(raw)
            paths.extend(_candidate_scan_paths(Path(resolved).expanduser(), pattern, max_files - len(paths)))
            if len(paths) >= max_files:
                break
        return paths

    if pattern and Path(pattern).is_absolute() and any(ch in pattern for ch in "*?["):
        matched = [p for p in glob.glob(pattern, recursive=True) if Path(p).is_file()]
        return [str(Path(p).resolve()) for p in matched[:max_files]]

    paths = []
    for root in roots:
        paths.extend(_candidate_scan_paths(Path(root), pattern, max_files - len(paths)))
        if len(paths) >= max_files:
            break
    return paths


def _semantic_grep_text_hits(paths: list[str], query: str, max_hits: int) -> list[SemanticGrepHit]:
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(query) + r"(?![A-Za-z0-9_])")
    hits: list[SemanticGrepHit] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        uri = file_uri(path)
        for line_idx, line_text in enumerate(text.splitlines()):
            search_text = _identifier_search_region(line_text)
            for match in pattern.finditer(search_text):
                character = _py_index_to_utf16_units(line_text, match.start())
                hits.append(SemanticGrepHit(
                    path=path,
                    line=line_idx,
                    character=character,
                    line_text=line_text.strip(),
                    uri=uri,
                    pos={"line": line_idx, "character": character},
                ))
                if len(hits) >= max_hits:
                    return hits
    return hits


def _identifier_search_region(line_text: str) -> str:
    """Drop obvious line-comment tails before text→semantic token scanning."""
    markers = [idx for marker in ("//", "#") if (idx := line_text.find(marker)) >= 0]
    if not markers:
        return line_text
    return line_text[:min(markers)]


def _location_from_lsp_item(item: dict) -> dict | None:
    if "uri" in item and "range" in item:
        return item
    if "targetUri" in item:
        return {
            "uri": item.get("targetUri", ""),
            "range": item.get("targetSelectionRange", item.get("targetRange", {})),
        }
    return None


def _locations_from_lsp(result: Any) -> list[dict]:
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    locs: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            loc = _location_from_lsp_item(item)
            if loc:
                locs.append(loc)
    return locs


def _semantic_location_key(loc: dict) -> str:
    return f"{loc.get('uri', '')}:{_range_str(loc.get('range', {}))}"


def _range_contains_position(rng: dict, line: int, character: int) -> bool:
    start = rng.get("start", {})
    end = rng.get("end", {})
    start_line = start.get("line", -1)
    end_line = end.get("line", -1)
    if line < start_line or line > end_line:
        return False
    if line == start_line and character < start.get("character", 0):
        return False
    if line == end_line and character > end.get("character", 0):
        return False
    return True


def _symbol_stack_at(symbols: list[dict], line: int, character: int) -> list[dict]:
    best: list[dict] = []
    for sym in symbols:
        rng = sym.get("range", sym.get("location", {}).get("range", {}))
        if not _range_contains_position(rng, line, character):
            continue
        child_stack = _symbol_stack_at(sym.get("children", []), line, character)
        stack = [sym, *child_stack]
        if len(stack) > len(best):
            best = stack
    return best


def _strip_hover_markdown(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or not stripped:
            continue
        lines.append(stripped)
    return " ".join(lines)


def _hover_text(hover: Any) -> str:
    if not hover:
        return ""
    contents = hover.get("contents", "") if isinstance(hover, dict) else hover
    if isinstance(contents, dict):
        return _strip_hover_markdown(str(contents.get("value", "")))
    if isinstance(contents, list):
        return _strip_hover_markdown(" ".join(
            str(c.get("value", "")) if isinstance(c, dict) else str(c)
            for c in contents
        ))
    return _strip_hover_markdown(str(contents))


def _semantic_kind_and_type(query: str, hover: Any) -> tuple[str, str]:
    text = _hover_text(hover)
    kind = "symbol"
    body = text
    m = re.match(r"^\(([^)]+)\)\s*(.*)$", text)
    if m:
        raw_kind = m.group(1).strip().lower()
        kind = {
            "parameter": "arg",
            "local variable": "local",
            "local": "local",
            "field": "field",
            "property": "property",
            "method": "method",
            "function": "function",
            "class": "class",
            "struct": "struct",
            "interface": "interface",
            "variable": "var",
        }.get(raw_kind, raw_kind)
        body = m.group(2).strip()

    type_text = ""
    colon = re.search(r"\b" + re.escape(query) + r"\s*:\s*([^=]+)", body)
    if colon:
        type_text = colon.group(1).strip()
    else:
        idx = body.find(query)
        if idx > 0:
            before = body[:idx].strip()
            before = re.sub(
                r"\b(public|private|protected|internal|static|readonly|const|sealed|partial|async|virtual|override|ref|out|in)\b",
                "",
                before,
            )
            type_text = " ".join(before.split())
    return kind, _compact_line(type_text, 90)


def _context_breadcrumb(path: str, line: int, character: int, query: str, symbols: list[dict]) -> str:
    stack = _symbol_stack_at(symbols, line - 1, character)
    file_name = Path(path).name
    file_stem = Path(path).stem

    type_kinds = {"Class", "Struct", "Interface", "Enum", "Module", "Namespace"}
    callable_kinds = {"Method", "Function", "Constructor"}

    type_symbols = [sym for sym in stack if _symbol_kind_label(sym.get("kind", 0)) in type_kinds]
    callable_symbols = [sym for sym in stack if _symbol_kind_label(sym.get("kind", 0)) in callable_kinds]

    if type_symbols:
        first_type = type_symbols[0].get("name", "")
        if first_type == file_stem:
            base = file_stem
            extra_types = [sym.get("name", "") for sym in type_symbols[1:]]
        else:
            base = f"{file_name}::{first_type}"
            extra_types = [sym.get("name", "") for sym in type_symbols[1:]]
    else:
        base = file_name
        extra_types = []

    pieces = [f"{base}:{line}", *extra_types]
    for sym in callable_symbols:
        name = sym.get("name", "")
        kind = _symbol_kind_label(sym.get("kind", 0))
        if kind == "Constructor":
            name = ".ctor"
        pieces.append(name)
    if not pieces[-1].endswith(query):
        pieces.append(query)
    return "::".join(part for part in pieces if part)


def _format_semantic_sample_locs(group: SemanticGrepGroup) -> str:
    locs = group.reference_locs[:3]
    if locs:
        parts: list[str] = []
        for loc in locs:
            path = _uri_to_path(loc.get("uri", ""))
            line = loc.get("range", {}).get("start", {}).get("line", 0) + 1
            if path == group.definition_path:
                parts.append(f"L{line}")
            else:
                parts.append(f"{Path(path).name}:L{line}")
        if len(group.reference_locs) > len(locs):
            parts.append("...")
        return ",".join(parts)
    hit_parts = [f"L{hit.line + 1}" for hit in group.hits[:3]]
    if len(group.hits) > len(hit_parts):
        hit_parts.append("...")
    return ",".join(hit_parts)


def _format_semantic_grep_group(index: int, group: SemanticGrepGroup) -> str:
    ref_count = len(group.reference_locs) if group.reference_locs else len(group.hits)
    type_suffix = f": {group.type_text}" if group.type_text else ""
    alias_prefix = f"{group.alias} " if group.alias else ""
    scope = _context_breadcrumb(
        group.definition_path or group.hits[0].path,
        group.definition_line or group.hits[0].line + 1,
        group.definition_character,
        group.name,
        group.context_symbols,
    )
    if group.definition_path and group.definition_path != group.hits[0].path:
        def_label = f"{Path(group.definition_path).name}:L{group.definition_line}"
    else:
        def_label = f"L{group.definition_line or group.hits[0].line + 1}"
    samples = _format_semantic_sample_locs(group)
    return _compact_line(
        f"[{index}] {alias_prefix}{group.kind} {group.name}{type_suffix} — {scope} — refs {ref_count} — def {def_label} — samples {samples}",
        240,
    )


def _alias_identity_from_group(group: SemanticGrepGroup) -> AliasIdentity:
    """Project a semantic group into render-memory's stable target identity."""
    hit = group.hits[0] if group.hits else None
    path = group.definition_path or (hit.path if hit is not None else "")
    line = group.definition_line or (hit.line + 1 if hit is not None else 1)
    character = group.definition_character if group.definition_character >= 0 else (hit.character if hit else 0)
    container = Path(path).stem
    if group.context_symbols:
        stack = _symbol_stack_at(group.context_symbols, line - 1, character)
        for sym in reversed(stack):
            kind = _symbol_kind_label(sym.get("kind", 0))
            if kind in {"Class", "Struct", "Interface", "Enum", "Module", "Namespace"}:
                container = sym.get("name", "") or container
                break
    symbol_kind = group.kind
    alias_kind = AliasKind.TYPE if symbol_kind in {"class", "struct", "interface", "enum", "type"} else AliasKind.SYMBOL
    return AliasIdentity(
        kind=alias_kind,
        name=group.name,
        path=path,
        line=line,
        character=character,
        symbol_kind=symbol_kind,
        bucket_key=container or path,
        bucket_label=f"{Path(path).name}::{container}" if container else Path(path).name,
    )


def _target_from_alias_record(record: AliasRecord) -> SemanticTarget:
    ident = record.identity
    uri = file_uri(ident.path)
    pos = {"line": max(ident.line - 1, 0), "character": max(ident.character, 0)}
    return SemanticTarget(
        uri=uri,
        pos=pos,
        path=ident.path,
        line=ident.line,
        character=ident.character,
        name=ident.name,
    )


def _alias_looks_like_render_memory_target(target: str) -> bool:
    raw = target.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    return bool(re.fullmatch(r"[A-Z]+[0-9]+", raw)) and not raw.isdigit()


def _touch_alias_identities(identities: list[AliasIdentity]) -> AliasTouchResult:
    if _broker_enabled():
        try:
            return _broker_render_touch_sync(identities)
        except BrokerError as e:
            if _broker_mode() == "on" or not _broker_unavailable(e):
                raise RuntimeError(f"broker render touch failed: {e.code}: {e}") from None
            agent_log(f"broker render touch unavailable ({e.code}: {e}); falling back to direct render memory")
        except ValueError as e:
            if _broker_mode() == "on":
                raise RuntimeError(f"broker render touch returned invalid data: {e}") from None
            agent_log(f"broker render touch returned invalid data ({e}); falling back to direct render memory")
    return _local_alias_coordinator.touch(_client_id, identities)


def _record_semantic_nav_context(query: str, groups: list[SemanticGrepGroup]) -> str:
    """Remember the last semantic graph so a later bare ``L78`` has context."""
    global _last_semantic_nav_query
    _last_semantic_groups.clear()
    _last_semantic_groups.extend(groups)
    _last_semantic_nav.clear()
    _last_semantic_nav_query = query
    alias_group_indices: list[int] = []
    alias_identities: list[AliasIdentity] = []
    seen: set[tuple[str, int, int, int]] = set()
    for group_index, group in enumerate(groups):
        if group.hits:
            group.alias = ""
            alias_group_indices.append(group_index)
            alias_identities.append(_alias_identity_from_group(group))
        if group.reference_locs:
            for loc in group.reference_locs:
                path = _uri_to_path(loc.get("uri", ""))
                start = loc.get("range", {}).get("start", {})
                line = start.get("line", 0) + 1
                character = start.get("character", 0)
                key = (path, line, character, group_index)
                if key in seen:
                    continue
                seen.add(key)
                _last_semantic_nav.append(SemanticNavEntry(
                    path=path,
                    line=line,
                    character=character,
                    group_index=group_index,
                    name=group.name,
                    kind=group.kind,
                ))
        else:
            for hit in group.hits:
                key = (hit.path, hit.line + 1, hit.character, group_index)
                if key in seen:
                    continue
                seen.add(key)
                _last_semantic_nav.append(SemanticNavEntry(
                    path=hit.path,
                    line=hit.line + 1,
                    character=hit.character,
                    group_index=group_index,
                    name=group.name,
                    kind=group.kind,
                ))
    if not alias_identities:
        return ""
    result = _touch_alias_identities(alias_identities)
    for group_index, decision in zip(alias_group_indices, result.decisions, strict=False):
        groups[group_index].alias = decision.record.alias
    return result.legend


def _nav_context_summary(entries: list[SemanticNavEntry]) -> str:
    lines = ["Ambiguous line in last semantic graph — pass file:Lline:"]
    for entry in entries[:20]:
        lines.append(
            f"  [{entry.group_index}] {Path(entry.path).name}:L{entry.line}  {entry.kind} {entry.name}  {entry.path}"
        )
    if len(entries) > 20:
        lines.append(f"  ... {len(entries) - 20} more")
    return "\n".join(lines)


def _graph_target_from_index(raw_index: str) -> SemanticTarget | str:
    if not _last_semantic_groups:
        return "No previous semantic graph. Run lsp_grep/lsp_symbols_at first or pass file_path+symbol."
    index = int(raw_index)
    if index < 0 or index >= len(_last_semantic_groups):
        return f"Graph index [{index}] not found in last semantic graph for {_last_semantic_nav_query!r}."
    group = _last_semantic_groups[index]
    if not group.hits:
        return f"Graph index [{index}] has no source hits."
    hit = group.hits[0]
    return SemanticTarget(
        uri=hit.uri,
        pos=hit.pos,
        path=hit.path,
        line=hit.line + 1,
        character=hit.character,
        name=group.name,
        group=group,
    )


def _line_text(path: str, line: int) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[line - 1].strip()
    except (OSError, IndexError):
        return ""


def _identifier_at_position(path: str, pos: dict) -> str:
    try:
        line_text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[pos.get("line", 0)]
    except (OSError, IndexError):
        return ""
    character = pos.get("character", 0)
    search_text = _identifier_search_region(line_text)
    fallback = ""
    for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", search_text):
        name = match.group(0)
        if name in _LINE_POSITION_SKIP_WORDS:
            continue
        start = _py_index_to_utf16_units(line_text, match.start())
        end = _py_index_to_utf16_units(line_text, match.end())
        if start <= character <= end:
            return name
        if not fallback and start >= character:
            fallback = name
    return fallback


def _target_from_resolved_uri(uri: str, pos: dict, name: str = "") -> SemanticTarget:
    path = _uri_to_path(uri)
    return SemanticTarget(
        uri=uri,
        pos=pos,
        path=path,
        line=pos.get("line", 0) + 1,
        character=pos.get("character", 0),
        name=name or _identifier_at_position(path, pos),
    )


def _semantic_group_from_target(target: SemanticTarget, kind: str = "root") -> SemanticGrepGroup:
    """Wrap a resolved target as a graph row so root anchors are navigable."""
    line0 = max(target.line - 1, 0)
    hit = SemanticGrepHit(
        path=target.path,
        line=line0,
        character=target.character,
        line_text=_line_text(target.path, target.line),
        uri=target.uri,
        pos=target.pos,
    )
    return SemanticGrepGroup(
        key=f"{target.path}:{line0}:{target.character}:{target.name}",
        name=target.name or _identifier_at_position(target.path, target.pos),
        kind=kind,
        type_text="",
        definition_path=target.path,
        definition_line=target.line,
        definition_character=target.character,
        hits=[hit],
    )


async def _resolve_semantic_target(
    target: str = "",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
) -> SemanticTarget | str:
    target = target.strip()
    if target:
        if _broker_enabled():
            try:
                broker_alias = await _broker_render_lookup(target)
            except BrokerError as e:
                if _broker_mode() == "on" or not _broker_unavailable(e):
                    return f"broker render lookup failed: {e.code}: {e}"
                broker_alias = None
            except ValueError as e:
                if _broker_mode() == "on":
                    return f"broker render lookup returned invalid data: {e}"
                broker_alias = None
            if isinstance(broker_alias, AliasRecord):
                return _target_from_alias_record(broker_alias)
            if isinstance(broker_alias, str) and _alias_looks_like_render_memory_target(target):
                return broker_alias

        alias_result = _render_memory.lookup(target)
        if alias_result.ok and alias_result.record is not None:
            return _target_from_alias_record(alias_result.record)
        if (
            alias_result.error is not AliasError.INVALID
            and _alias_looks_like_render_memory_target(target)
        ):
            return alias_result.message

        graph_index = re.fullmatch(r"\[?(\d+)\]?", target)
        if graph_index:
            return _graph_target_from_index(graph_index.group(1))

        resolved_line = _resolve_line_target(target)
        if isinstance(resolved_line, tuple):
            path, target_line = resolved_line
            uri = file_uri(path)
            pos = await _position_for_line(path, uri, target_line)
            return _target_from_resolved_uri(uri, pos)
        return resolved_line

    if file_path or symbol or line > 0:
        try:
            uri, pos = await _resolve(file_path, symbol, line)
            return _target_from_resolved_uri(uri, pos, symbol)
        except AmbiguousSymbol as e:
            return _ambiguous_msg(e)
        except (LspError, ValueError) as e:
            return f"LSP error: {e}"

    return "Provide target, or file_path with symbol/line."


def _resolve_path_hint(path_hint: str) -> str | None:
    path_hint = path_hint.strip()
    if not path_hint:
        return None
    try:
        return _resolve_file_path(path_hint)
    except ValueError:
        pass
    direct = Path(path_hint).expanduser()
    matches = [entry.path for entry in _last_semantic_nav if entry.path == path_hint or Path(entry.path).name == path_hint]
    if len(set(matches)) == 1:
        return matches[0]
    suffix_matches = [entry.path for entry in _last_semantic_nav if entry.path.endswith(path_hint)]
    if len(set(suffix_matches)) == 1:
        return suffix_matches[0]
    if direct.is_absolute() or len(direct.parts) > 1:
        return str(direct.resolve())
    return None


def _resolve_line_target(target: str, file_path: str = "", line: int = 0) -> tuple[str, int] | str:
    if file_path and line > 0:
        try:
            return _resolve_file_path(file_path), line
        except ValueError as e:
            return str(e)

    target = target.strip()
    if not target:
        return "Provide target like 'L78', 'path:L78', or file_path+line."

    line_only = re.fullmatch(r"L?(\d+)", target)
    if line_only:
        target_line = int(line_only.group(1))
        matches = [entry for entry in _last_semantic_nav if entry.line == target_line]
        paths = sorted({entry.path for entry in matches})
        if len(paths) == 1:
            return paths[0], target_line
        if matches:
            return _nav_context_summary(matches)
        if not _last_semantic_nav:
            return "No previous lsp_grep context. Pass an explicit file:Lline target."
        return f"L{target_line} was not in the last lsp_grep graph for {_last_semantic_nav_query!r}."

    explicit = re.fullmatch(r"(.+?):L?(\d+)", target)
    if explicit:
        path_hint = explicit.group(1)
        try:
            path = _resolve_file_path(path_hint)
        except AmbiguousFilePath as e:
            return str(e)
        except ValueError:
            path = _resolve_path_hint(path_hint)
        if path is None:
            return f"Could not resolve path in target {target!r}."
        return path, int(explicit.group(2))

    return "Provide target like 'L78', 'path:L78', or file_path+line."


def _identifier_hits_on_line(path: str, line: int) -> list[tuple[str, SemanticGrepHit]]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        line_text = text.splitlines()[line - 1]
    except (OSError, IndexError):
        return []
    uri = file_uri(path)
    hits: list[tuple[str, SemanticGrepHit]] = []
    search_text = _identifier_search_region(line_text)
    for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", search_text):
        name = match.group(0)
        if name in _LINE_POSITION_SKIP_WORDS:
            continue
        character = _py_index_to_utf16_units(line_text, match.start())
        hits.append((name, SemanticGrepHit(
            path=path,
            line=line - 1,
            character=character,
            line_text=line_text.strip(),
            uri=uri,
            pos={"line": line - 1, "character": character},
        )))
    return hits


def _resolve_paths(file_path: str, pattern: str) -> list[str] | str:
    """Resolve multi-file arguments into a list of paths.

    Supports comma-separated file_path and glob patterns.
    Returns a list of paths on success, or an error string if inputs are empty.
    """
    try:
        if file_path and "," in file_path:
            return [_resolve_file_path(p.strip()) for p in file_path.split(",") if p.strip()]
        if file_path:
            return [_resolve_file_path(file_path)]
    except ValueError as e:
        return str(e)
    if pattern:
        return sorted(glob.glob(pattern, recursive=True))
    return "Provide file_path or pattern."


# --- Tool implementations ---


async def _outline_single(file_path: str) -> str:
    """Compact symbol outline for one file. Empty result → 'No symbols found.'."""
    file_path = _resolve_file_path(file_path)
    uri = file_uri(file_path)
    result = await _request("textDocument/documentSymbol", {
        "textDocument": {"uri": uri},
    }, uri=uri)
    if not result:
        return "No symbols found."
    lines: list[str] = []
    for sym in result:
        lines.extend(_format_outline_tree(sym))
    return "\n".join(lines)


async def lsp_outline(file_path: str = "", pattern: str = "") -> str:
    """Compact file/workspace breadcrumbs — one line per symbol.

    Workflow replacement for the raw ``textDocument/documentSymbol`` verb (see
    ``docs/tool-surface.md``). Each line is ``Lxx  <indent><Kind> <name>`` so a
    model can pivot straight into ``lsp_symbols_at("Lxx")`` without re-scanning.

    ``file_path`` accepts a single path, a comma-separated list, or a unique
    basename resolved under the active workspaces (via ``_resolve_paths``).
    ``pattern`` is a glob fallback when ``file_path`` is empty. Multi-file
    output is grouped under ``=== path ===`` headers, mirroring the batching
    shape of ``lsp_diagnostics``.
    """
    paths = _resolve_paths(file_path, pattern)
    if isinstance(paths, str):
        return paths
    try:
        if len(paths) == 1:
            return await _outline_single(paths[0])
        sections: list[str] = []
        for p in paths:
            body = await _outline_single(p)
            sections.append(f"=== {p} ===\n{body}")
        return "\n\n".join(sections)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_rename(file_path: str, new_name: str, symbol: str = "", line: int = 0) -> str:
    """Preview a symbol rename across the workspace. Pass symbol name or line number.

    Stages the returned WorkspaceEdit under ``_pending``. Call ``lsp_confirm(0)``
    to apply it.
    """
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        try:
            result = await _request("textDocument/rename", {
                "textDocument": {"uri": uri},
                "position": pos,
                "newName": new_name,
            }, uri=uri)
        except (LspError, asyncio.TimeoutError, ConnectionError) as e:
            return await _rename_trace(
                file_path=file_path,
                uri=uri,
                pos=pos,
                new_name=new_name,
                error=e,
            )
        if not result:
            _clear_pending()
            trace = await _rename_trace(
                file_path=file_path,
                uri=uri,
                pos=pos,
                new_name=new_name,
                rename_result=result,
            )
            return f"No rename edits returned.\n\n{trace}"

        edit_files = _collect_edit_files(result)
        total_edits = sum(len(edits) for _, edits in edit_files)

        lines: list[str] = []
        for path, edits in edit_files:
            lines.append(f"{path}: {len(edits)} edit(s)")
            lines.extend(_format_text_edit_preview(path, edits))

        title = f"rename {symbol or f'line {line}'} → {new_name} ({len(edit_files)} file(s), {total_edits} edit(s))"
        _set_pending(
            CandidateKind.SYMBOL_RENAME.value,
            [Candidate(kind=CandidateKind.SYMBOL_RENAME, title=title, edit=result)],
            title,
        )
        lines.insert(
            0,
            f"Preview: {len(edit_files)} file(s), {total_edits} edit(s). Call lsp_confirm(0) to commit the rename.",
        )
        lines.insert(1, "Target:")
        lines[2:2] = [f"  {line}" for line in _line_snapshot(file_path, pos).splitlines()]
        return "\n".join(lines)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


def _apply_text_edits(text: str, edits: list[dict]) -> str:
    """Apply LSP TextEdits to a string. Edits are applied end-to-start to keep offsets valid.

    LSP ``character`` offsets are UTF-16 code units, not Python string indexes.
    Convert the line-relative UTF-16 position before slicing, or edits after
    astral Unicode characters land in the wrong place.

    LSP allows a position with line == total_lines (one past the last line) to
    mean "end of file" — this is how pylance encodes full-document replacements
    for rename-driven edits. Previously we rejected such edits as out-of-range,
    silently dropping every import-rewrite in a move. Now we treat lines past
    the array as EOF.
    """
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def _utf16_to_py_index(line_text: str, utf16_units: int) -> int:
        if utf16_units <= 0:
            return 0
        consumed = 0
        for idx, ch in enumerate(line_text):
            next_consumed = consumed + len(ch.encode("utf-16-le")) // 2
            if next_consumed > utf16_units:
                return idx
            consumed = next_consumed
            if consumed == utf16_units:
                return idx + 1
        return len(line_text)

    def _offset(pos: dict) -> int | None:
        line = pos["line"]
        char = pos["character"]
        if line < 0 or line > len(line_starts):
            return None
        if line == len(line_starts):
            return len(text)
        start = line_starts[line]
        next_start = line_starts[line + 1] if line + 1 < len(line_starts) else len(text)
        line_end = next_start - 1 if next_start > start and text[next_start - 1] == "\n" else next_start
        line_text = text[start:line_end]
        return start + _utf16_to_py_index(line_text, char)

    sorted_edits = sorted(
        edits,
        key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]),
        reverse=True,
    )

    result = text
    for edit in sorted_edits:
        start_offset = _offset(edit["range"]["start"])
        end_offset = _offset(edit["range"]["end"])
        if start_offset is None or end_offset is None:
            raise ValueError(f"Invalid text edit range: {_range_str(edit.get('range', {}))}")
        if start_offset > end_offset:
            raise ValueError(f"Invalid reversed text edit range: {_range_str(edit.get('range', {}))}")
        result = result[:start_offset] + edit["newText"] + result[end_offset:]
    return result


def _format_text_edit_preview(path: str, edits: list[dict]) -> list[str]:
    """Render final before/after lines for a set of LSP TextEdits.

    Roslyn often returns minimal edits such as ``Outpu -> Artifac`` for
    ``GetOutputTexture -> GetArtifactTexture``. Showing only that raw span is
    correct but misleading; this preview applies the edits in-memory and prints
    the resulting line so the agent can confirm the semantic effect before
    calling ``lsp_confirm``.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [
            f"  {_range_str(e.get('range', {}))} → {e.get('newText', '')!r}"
            for e in edits
        ]

    after_text = _apply_text_edits(text, edits)
    before_lines = text.splitlines()
    after_lines = after_text.splitlines()
    touched_lines = sorted({
        e.get("range", {}).get("start", {}).get("line", -1)
        for e in edits
    })

    lines: list[str] = []
    for line_idx in touched_lines:
        if line_idx < 0:
            continue
        before = before_lines[line_idx] if line_idx < len(before_lines) else ""
        after = after_lines[line_idx] if line_idx < len(after_lines) else ""
        line_edits = [
            e for e in edits
            if e.get("range", {}).get("start", {}).get("line", -1) == line_idx
        ]
        raw = ", ".join(
            f"{_range_str(e.get('range', {}))} → {e.get('newText', '')!r}"
            for e in line_edits
        )
        if before == after:
            lines.append(f"  L{line_idx + 1}: {raw}")
            continue
        lines.extend([
            f"  L{line_idx + 1}:",
            f"    - {_compact_line(before)}",
            f"    + {_compact_line(after)}",
            f"    edit: {raw}",
        ])
    return lines


def _apply_create_file(uri: str, options: dict) -> WorkspaceApplyResult:
    path = _uri_to_path(uri)
    target = Path(path)
    ignore_if_exists = bool(options.get("ignoreIfExists"))
    overwrite = bool(options.get("overwrite"))
    if target.exists():
        if ignore_if_exists:
            return WorkspaceApplyResult()
        if not overwrite:
            raise FileExistsError(path)
    if target.parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        target.write_text("", encoding="utf-8")
    else:
        target.touch(exist_ok=False)
    return WorkspaceApplyResult(affected=[path], created=[path])


def _apply_rename_file(old_uri: str, new_uri: str, options: dict) -> WorkspaceApplyResult:
    old_path = _uri_to_path(old_uri)
    new_path = _uri_to_path(new_uri)
    old = Path(old_path)
    new = Path(new_path)
    ignore_if_exists = bool(options.get("ignoreIfExists"))
    overwrite = bool(options.get("overwrite"))
    if new.exists():
        if ignore_if_exists:
            return WorkspaceApplyResult()
        if not overwrite:
            raise FileExistsError(new_path)
        if new.is_dir():
            shutil.rmtree(new)
        else:
            new.unlink()
    if new.parent:
        new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    return WorkspaceApplyResult(affected=[old_path, new_path], renamed=[(old_path, new_path)])


def _apply_delete_file(uri: str, options: dict) -> WorkspaceApplyResult:
    path = _uri_to_path(uri)
    target = Path(path)
    ignore_if_not_exists = bool(options.get("ignoreIfNotExists"))
    recursive = bool(options.get("recursive"))
    if not target.exists():
        if ignore_if_not_exists:
            return WorkspaceApplyResult()
        raise FileNotFoundError(path)
    if target.is_dir():
        if not recursive:
            raise IsADirectoryError(path)
        shutil.rmtree(target)
    else:
        target.unlink()
    return WorkspaceApplyResult(affected=[path], deleted=[path])


def _apply_workspace_edit(edit: dict) -> WorkspaceApplyResult:
    """Apply a WorkspaceEdit to the filesystem."""
    result = WorkspaceApplyResult()

    for change_uri, edits in edit.get("changes", {}).items():
        path = _uri_to_path(change_uri)
        text = Path(path).read_text(encoding="utf-8")
        Path(path).write_text(_apply_text_edits(text, edits), encoding="utf-8")
        result.affected.append(path)

    for doc_change in edit.get("documentChanges", []):
        if "textDocument" in doc_change:
            change_uri = doc_change["textDocument"]["uri"]
            path = _uri_to_path(change_uri)
            edits = doc_change.get("edits", [])
            text = Path(path).read_text(encoding="utf-8")
            Path(path).write_text(_apply_text_edits(text, edits), encoding="utf-8")
            result.affected.append(path)
            continue

        kind = doc_change.get("kind")
        options = doc_change.get("options", {})
        if kind == "create":
            result.absorb(_apply_create_file(doc_change["uri"], options))
        elif kind == "rename":
            result.absorb(_apply_rename_file(doc_change["oldUri"], doc_change["newUri"], options))
        elif kind == "delete":
            result.absorb(_apply_delete_file(doc_change["uri"], options))
        else:
            raise ValueError(f"Unsupported documentChanges operation: {kind!r}")

    return result


def _collect_edit_files(result: dict) -> list[tuple[str, list[dict]]]:
    """Flatten a WorkspaceEdit into [(path, edits), ...], dropping 0-edit entries."""
    edit_files: list[tuple[str, list[dict]]] = []
    for change_uri, edits in result.get("changes", {}).items():
        if edits:
            edit_files.append((_uri_to_path(change_uri), edits))
    for doc_change in result.get("documentChanges", []):
        if "textDocument" in doc_change:
            edits = doc_change.get("edits", [])
            if edits:
                edit_files.append((_uri_to_path(doc_change["textDocument"]["uri"]), edits))
    return edit_files


def _check_move_discrepancy(from_paths: list[str]) -> str | None:
    """Heuristic: if lsp_move returned 0 edits, scan for files that mention any
    moved file's module name (basename sans extension). Catches the 'cold index' failure
    mode where the LSP returns 0 edits but regex shows actual importers exist.
    """
    if not from_paths:
        return None
    patterns = _parse_warmup_patterns() or ["*.py"]
    basenames = [Path(p).stem for p in from_paths if Path(p).stem and len(Path(p).stem) >= 3]
    if not basenames:
        return None

    folders: set[str] = set()
    for client in _chain_clients:
        if client is not None:
            folders.update(client.workspace_folders)
    if not folders:
        folders.add(os.path.abspath(os.environ.get("LSP_ROOT", os.getcwd())))

    hits: list[str] = []
    MAX_HITS = 10
    MAX_SCAN = 2000
    scanned = 0
    source_paths = {os.path.abspath(p) for p in from_paths}
    for folder in folders:
        for pattern in patterns:
            try:
                candidates = list(Path(folder).rglob(pattern))
            except OSError:
                continue
            for fp in candidates:
                if scanned >= MAX_SCAN:
                    break
                try:
                    abs_p = str(fp.resolve())
                except OSError:
                    continue
                if abs_p in source_paths:
                    continue
                scanned += 1
                try:
                    text = fp.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for name in basenames:
                    if name in text:
                        hits.append(abs_p)
                        break
                if len(hits) >= MAX_HITS:
                    return (
                        f"⚠ 0 edits returned but {len(hits)}+ files mention the module name(s). "
                        f"LSP index may be cold. First hits:\n  " + "\n  ".join(hits[:MAX_HITS])
                    )
    if hits:
        return (
            f"⚠ 0 edits returned but {len(hits)} file(s) mention the module name(s). "
            f"LSP index may be cold:\n  " + "\n  ".join(hits)
        )
    return None


async def _do_move(files: list[tuple[str, str]]) -> str:
    """Core willRenameFiles + preview staging for one or more file moves."""
    files_param = [{"oldUri": file_uri(f), "newUri": file_uri(t)} for f, t in files]

    # Open all source docs on whichever client handles the request (done by
    # _request's uri= path). Just trigger workspace auto-add for each file
    # BEFORE the request, so basedpyright/pylance see the right roots.
    # Don't pre-ensure_document across all clients — that sends redundant
    # didOpen/didChange to servers that will never process the method and
    # can confuse strict ones (pylance got unhappy with didOpen+didChange+
    # willRename in rapid succession).
    first_uri = file_uri(files[0][0])
    for f, _ in files:
        await _ensure_workspace_for(file_uri(f))

    try:
        result = await _request(
            "workspace/willRenameFiles",
            {"files": files_param},
            uri=first_uri,
        )
    except (LspError, ConnectionError, asyncio.TimeoutError) as e:
        agent_log(f"willRenameFiles failed ({e}), falling through to rewriter")
        result = {}
    if not result:
        result = {}

    # Language-specific import rewriter fallback. If the LSP returned 0 edits
    # (or crashed) but imports exist, let a language-aware rewriter inside the
    # bridge fill in. Gated by LSP_LANGUAGE so we don't Python-stuff other
    # languages' moves.
    lsp_edits = _collect_edit_files(result)
    if not lsp_edits and os.environ.get("LSP_LANGUAGE", "").strip().lower() == "python":
        workspace_folders = set(await _known_workspace_roots())

        rewriter_changes: dict = {"changes": {}}
        for f, t in files:
            edit, scanned = python_import_rewrite(f, t, sorted(workspace_folders))
            n_groups = len(edit.get("changes", {}))
            agent_log(f"python rewriter: {f} → {t} scanned {scanned} files, {n_groups} edit groups")
            rewriter_changes = merge_workspace_edits(rewriter_changes, edit)

        if rewriter_changes.get("changes"):
            result = merge_workspace_edits(result, rewriter_changes)

    edit_files = _collect_edit_files(result)
    total_edits = sum(len(e) for _, e in edit_files)

    lines: list[str] = []
    for path, edits in edit_files:
        lines.append(f"{path}: {len(edits)} edit(s)")
        for e in edits:
            lines.append(f"  {_range_str(e.get('range', {}))} → {e.get('newText', '')!r}")

    # Stage candidate: single WorkspaceEdit covering all renames, plus a list of
    # per-file move operations so _apply_candidate runs the mv after edits land.
    move_desc = (
        f"move {files[0][0]} → {files[0][1]}" if len(files) == 1
        else f"batch move {len(files)} file(s)"
    )
    description = f"{move_desc} ({len(edit_files)} file(s), {total_edits} edit(s))"
    if len(files) == 1:
        candidate = Candidate(
            kind=CandidateKind.FILE_MOVE,
            title=description,
            edit=result or {},
            from_path=files[0][0],
            to_path=files[0][1],
        )
    else:
        candidate = Candidate(
            kind=CandidateKind.FILE_MOVE_BATCH,
            title=description,
            edit=result or {},
            moves=[FileMove(from_path=f, to_path=t) for f, t in files],
        )
    _set_pending(candidate.kind.value, [candidate], description)

    lines.insert(
        0,
        f"Preview: {len(edit_files)} file(s), {total_edits} edit(s). Call lsp_confirm(0) to commit the move.",
    )

    if total_edits == 0 and len(edit_files) == 0:
        warning = _check_move_discrepancy([f for f, _ in files])
        if warning:
            lines.append("")
            lines.append(warning)
            lines.append("Options: (1) pre-warm importer files via lsp_symbol, (2) lsp_session(action='add', path=...) on the project, (3) fall back to regex rewrite if LSP is unreliable here.")

    return "\n".join(lines)


async def _resolve_symbol_to_file(symbol: str) -> str | None:
    """Find the file containing a top-level symbol via workspace/symbol.

    Prefers exact name matches; falls back to the first hit. Returns an
    absolute path or None if no match.
    """
    try:
        result = await _request("workspace/symbol", {"query": symbol})
    except LspError:
        return None
    if not result:
        return None
    exact = [s for s in result if s.get("name") == symbol]
    candidates = exact or result
    loc = candidates[0].get("location", {})
    uri = loc.get("uri", "")
    if not uri:
        return None
    path = _uri_to_path(uri)
    return os.path.abspath(path) if path else None


def _parse_moves(moves: str) -> list[tuple[str, str]]:
    """Parse the ``moves`` batch payload into ``(from, to)`` pairs.

    Format: ``from=>to`` pairs, one per line or comma-separated. Whitespace
    around paths and around ``=>`` is ignored. Blank entries are dropped so
    a trailing newline or comma is harmless. Anything that is not a single
    ``from=>to`` pair raises ``ValueError`` so the caller surfaces a clear
    error rather than silently swallowing a malformed batch.
    """
    raw_entries = [e.strip() for chunk in moves.splitlines() for e in chunk.split(",")]
    pairs: list[tuple[str, str]] = []
    for entry in raw_entries:
        if not entry:
            continue
        if entry.count("=>") != 1:
            raise ValueError(f"Bad move entry {entry!r}; expected 'from=>to'.")
        from_part, to_part = entry.split("=>", 1)
        from_part, to_part = from_part.strip(), to_part.strip()
        if not from_part or not to_part:
            raise ValueError(f"Bad move entry {entry!r}; from/to must be non-empty.")
        pairs.append((from_part, to_part))
    return pairs


async def lsp_move(
    from_path: str = "",
    to_path: str = "",
    symbol: str = "",
    moves: str = "",
) -> str:
    """Preview file/symbol moves with their import-updating edits.

    Three call shapes:

    - Single move: ``from_path`` + ``to_path``.
    - Symbol move: ``symbol`` + ``to_path`` resolves the source file via
      ``workspace/symbol`` (useful when you know the class/function but not
      its file).
    - Batch move: ``moves`` accepts a list of ``from=>to`` pairs separated
      by newlines or commas — e.g. ``"a.py=>b.py, c.py=>pkg/c.py"`` or one
      pair per line. A single ``willRenameFiles`` round-trip covers all of
      them; a single ``lsp_confirm(0)`` commits the lot atomically.

    Always previews — the resulting WorkspaceEdit + file-move metadata is
    staged under ``_pending``. ``lsp_confirm(0)`` runs the edits then the
    ``os.rename``(s) so the import rewrite and the file move stay atomic.
    """
    try:
        if moves:
            if from_path or to_path or symbol:
                return "moves= is exclusive with from_path/to_path/symbol."
            try:
                pairs = _parse_moves(moves)
            except ValueError as e:
                return str(e)
            if not pairs:
                return "No files specified."
            pairs = [(_resolve_file_path(f), t) for f, t in pairs]
            return await _do_move(pairs)

        if symbol and not from_path:
            resolved = await _resolve_symbol_to_file(symbol)
            if not resolved:
                return f"Could not resolve symbol {symbol!r} to a file via workspace/symbol."
            from_path = resolved
        if not from_path:
            return "Provide from_path, symbol, or moves."
        if not to_path:
            return "to_path is required."
        from_path = _resolve_file_path(from_path)
        return await _do_move([(from_path, to_path)])
    except (LspError, ValueError, OSError) as e:
        return f"LSP error: {e}"


def _diagnostic_sort_key(diagnostic: dict) -> tuple[int, int, str, str, str]:
    rng = diagnostic.get("range", {})
    start = rng.get("start", {}) if isinstance(rng, dict) else {}
    return (
        start.get("line", -1),
        start.get("character", -1),
        str(diagnostic.get("source", "")),
        str(diagnostic.get("code", "")),
        str(diagnostic.get("message", "")),
    )


def _diagnostics_for_line(diagnostics: list[dict], line: int) -> list[dict]:
    return sorted(
        (
            d for d in diagnostics
            if d.get("range", {}).get("start", {}).get("line", -1) == line
        ),
        key=_diagnostic_sort_key,
    )


def _code_action_kind_matches(action_kind: str, kind_prefix: str) -> bool:
    prefix = kind_prefix.strip()
    return not prefix or action_kind.startswith(prefix)


async def lsp_fix(
    target: str = "",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    diagnostic_index: int = -1,
    kind: str = "",
) -> str:
    """Surface code actions (quick fixes, refactorings) for one semantic target.

    Workflow replacement for raw ``textDocument/codeAction`` (see
    ``docs/tool-surface.md``). Resolves ``target`` via
    ``_resolve_semantic_target`` (graph index ``[N]`` from the last semantic
    result, bare ``Lxx`` from the last ``lsp_grep`` graph, ``file:Lx``, or
    ``file_path``+``symbol``/``line``).

    Diagnostics on the resolved line are read from the primary LSP client
    and rendered as ``(d0)``, ``(d1)``, … so the agent can see the verifier
    signal that motivates each fix. ``diagnostic_index`` selects which
    diagnostic is forwarded as ``CodeActionContext.diagnostics`` (-1 = all
    line diagnostics, the default). ``kind`` filters returned actions whose
    ``CodeActionKind`` starts with the given prefix (e.g. ``quickfix``,
    ``refactor.extract``).

    Edit-backed actions are staged into the module-level ``_pending`` buffer
    as ``CandidateKind.CODE_ACTION`` and numbered ``[0]``, ``[1]``, …; pick
    one with ``lsp_confirm(N)`` to apply its WorkspaceEdit. Command-only or
    no-edit actions render with a ``[-]`` marker and are not stageable —
    they require a server-side execute that this surface deliberately does
    not perform. If no edit-backed actions remain after filtering, any
    previously staged buffer is cleared.
    """
    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved

    try:
        stored = await _stored_diagnostics(resolved.uri)
        target_line = resolved.pos.get("line", 0)
        line_diagnostics = _diagnostics_for_line(stored, target_line)

        if diagnostic_index >= 0:
            if diagnostic_index >= len(line_diagnostics):
                _clear_pending()
                return (
                    f"diagnostic_index (d{diagnostic_index}) out of range; "
                    f"line has {len(line_diagnostics)} diagnostic(s)."
                )
            ctx_diagnostics = [line_diagnostics[diagnostic_index]]
        else:
            ctx_diagnostics = line_diagnostics

        sections: list[str] = []
        head = f"Fix at {resolved.path}:L{resolved.line}"
        if resolved.name:
            head = f"{head} ({resolved.name})"
        sections.append(head)

        if line_diagnostics:
            sections.append("diagnostics:")
            for di, d in enumerate(line_diagnostics):
                sev = _severity_label(d.get("severity", 0))
                msg = d.get("message", "")
                source = d.get("source", "")
                code = d.get("code", "")
                tag = f"[{source} {code}]" if source else (f"[{code}]" if code else "")
                marker = "*" if (diagnostic_index == di or diagnostic_index < 0) else " "
                sections.append(_compact_line(
                    f"  (d{di}){marker} {sev}  {msg}  {tag}".rstrip(),
                    240,
                ))
        else:
            sections.append("diagnostics: (none on target line)")

        request_range = {"start": resolved.pos, "end": resolved.pos}
        if diagnostic_index >= 0 and ctx_diagnostics:
            diag_range = ctx_diagnostics[0].get("range")
            if isinstance(diag_range, dict) and "start" in diag_range and "end" in diag_range:
                request_range = diag_range

        context: dict[str, Any] = {"diagnostics": ctx_diagnostics}
        kind_prefix = kind.strip()
        if kind_prefix:
            context["only"] = [kind_prefix]

        result = await _request("textDocument/codeAction", {
            "textDocument": {"uri": resolved.uri},
            "range": request_range,
            "context": context,
        }, uri=resolved.uri)

        if not result:
            _clear_pending()
            sections.append("actions: (none)")
            return "\n".join(sections)

        action_candidates: list[Candidate] = []
        action_lines: list[str] = []
        filtered_out = 0
        for action in result:
            action_kind = action.get("kind", "")
            if not _code_action_kind_matches(action_kind, kind_prefix):
                filtered_out += 1
                continue
            title = action.get("title", "")
            edit = action.get("edit")
            if edit:
                idx = len(action_candidates)
                parts = [f"  [{idx}] {title}"]
            else:
                parts = [f"  [-] {title}"]
            if action_kind:
                parts.append(f"[{action_kind}]")
            if edit:
                n = len(edit.get("changes", {})) + len(edit.get("documentChanges", []))
                parts.append(f"({n} file(s))")
                action_candidates.append(Candidate(
                    kind=CandidateKind.CODE_ACTION,
                    title=title,
                    edit=edit,
                ))
            elif action.get("command"):
                parts.append("(command-only; not staged)")
            else:
                parts.append("(no edit; not staged)")
            action_lines.append(" ".join(parts))

        sections.append("actions:")
        if action_lines:
            sections.extend(action_lines)
        else:
            sections.append("  (none matching filters)")
        if kind_prefix and filtered_out:
            sections.append(f"  ({filtered_out} hidden by kind={kind_prefix!r})")

        if action_candidates:
            _set_pending(
                "fix",
                action_candidates,
                f"{len(action_candidates)} code action(s) at {resolved.path}:L{resolved.line}",
            )
            sections.append("")
            sections.append(
                f"Staged {len(action_candidates)} edit action(s). Call lsp_confirm(N) to apply."
            )
        else:
            _clear_pending()
            sections.append("")
            sections.append("No edit-backed actions to stage.")
        return "\n".join(sections)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


async def lsp_session(action: str = "status", path: str = "", server: str = "") -> str:
    """Inspect and manage LSP chain sessions: status, add, warm, restart.

    One workflow surface that replaces the old ``lsp_info`` /
    ``lsp_workspaces`` / ``lsp_add_workspace`` triad. Actions:

    - ``status`` (default): compact build/version + per-server chain config,
      capability summary, registered workspace folders, and warmup ages.
      Use when tool behavior looks stale (compare the printed git SHA
      against cc-lsp-now's HEAD — Claude Code reuses the MCP subprocess
      across /reload-plugins, only a full restart spawns a fresh one).
    - ``add``: register ``path`` as a workspace folder on every server in
      the chain and run warmup. Use when LSP_PROJECT_MARKERS auto-detection
      misses an unusual layout, or to pre-index before a batch refactor.
    - ``warm``: re-run warmup for ``path`` (or every registered folder if
      omitted), bypassing the once-per-folder cache. ``server`` filters by
      chain label/command/name. Use after files appear/disappear on disk and
      the LSP's view drifts.
    - ``restart``: stop and respawn LSP clients (filter via ``server``).
      Use when a server is wedged or its in-memory state is corrupt.
    - ``stop``: stop matching live LSP clients/sessions without respawning.
    """
    act = (action or "status").lower()
    if act == "status":
        return await _session_status()
    if act == "add":
        if not path:
            return "action=add requires path"
        return await _session_add(path)
    if act == "warm":
        return await _session_warm(path, server)
    if act == "restart":
        return await _session_restart(server)
    if act == "stop":
        return await _session_stop(server)
    return f"Unknown action: {action!r}. Valid: status, add, warm, restart, stop."


async def lsp_log(
    action: str = "weather",
    message: str = "",
    files: str = "",
    symbols: str = "",
    aliases: str = "",
    id: str = "",
    timeout: str = "3m",
    kind: str = "",
    status: str = "",
    targets: str = "",
    commit: str = "",
) -> str:
    """Record and inspect agent-bus coordination events (Wave 1 surface).

    The bus is warn-only shared context for parallel agents. It appends
    workspace-scoped events, opens timed questions, records replies, and
    renders compact weather at natural boundaries; it does not claim files
    or deny edits.

    Actions (default ``weather``):

    - ``event``: append a structured event. ``kind`` becomes the stored
      ``event_type`` (e.g. ``post_edit``, ``test``).
    - ``note``: post a durable, scoped note without a timeout.
    - ``ask``: open a timed coordination question. ``timeout`` accepts
      ``30s``, ``3m``, ``1h``; default is ``3m``. ``message`` is required.
    - ``reply``: attach a reply to an open question via ``id="Q<n>"``.
    - ``recent``: show recent related bus activity (or empty-state line).
    - ``settle``: close expired questions and emit their digests.
    - ``precommit``: summarize related activity and suggest checks.
    - ``postcommit``: record a commit; ``commit`` lands in metadata.
    - ``weather``: compact workspace status — open questions + recent.

    Routing mirrors the render/lsp broker fallback policy:

    - When the broker is enabled (``CC_LSP_BROKER`` not ``off`` and an
      LSP chain is configured), call ``bus.<action>`` against the broker
      with workspace-stamped params.
    - On ``CC_LSP_BROKER=on`` any broker failure surfaces as an inline
      error string so the agent sees the misconfiguration.
    - In ``auto`` (or ``off``) mode, an unreachable broker falls back to
      the in-process :class:`AgentBus` so coordination still works for
      solo agents and broker-down recoveries.
    """
    act = (action or "weather").strip().lower()
    if act not in _BUS_ACTIONS:
        return f"Unknown action: {action!r}. Valid: {', '.join(_BUS_ACTIONS)}."

    if act == "ask" and not message.strip():
        return 'action="ask" requires message="..." (the question to open).'
    if act == "reply" and not id.strip():
        return 'action="reply" requires id="Q<n>" (the open question id).'

    timeout_seconds = _parse_bus_duration(timeout)
    if isinstance(timeout_seconds, str):
        return timeout_seconds

    params = _bus_params(
        message=message,
        kind=kind,
        files=files,
        symbols=symbols,
        aliases=aliases,
        question_id=id,
        timeout=timeout,
        status=status,
        targets=targets,
        commit=commit,
        action=act,
    )

    method = f"bus.{act}"
    if _broker_enabled():
        wire = _broker_base_params()
        wire.update(params)
        try:
            result = await _broker_bus_call(method, wire)
        except BrokerError as e:
            if _broker_mode() == "on" or not _broker_unavailable(e):
                return f"broker {method} failed: {e.code}: {e}"
            agent_log(f"broker {method} unreachable ({e.code}); using local bus")
            local = _local_bus_dispatch(act, params)
            if isinstance(local, str):
                return local
            return _render_bus_result(act, local)
        if not isinstance(result, dict):
            return f"broker {method} returned {type(result).__name__}: {result!r}"
        return _render_bus_result(act, cast(dict[str, object], result))

    local = _local_bus_dispatch(act, params)
    if isinstance(local, str):
        return local
    return _render_bus_result(act, local)


async def lsp_memory(action: str = "status", target: str = "", mode: str = "") -> str:
    """Inspect and manage render-memory aliases.

    Render memory is the persistent sidecar to the last-result graph handles:
    ``[N]`` still means "row N from the latest graph", while aliases such as
    ``A0`` / ``[A0]`` survive across graph-producing tool calls within the
    current epoch and can be passed back as semantic targets.

    Actions:
    - ``status``: compact epoch/generation/count summary.
    - ``legend``: decode active aliases. ``target`` may be a comma-separated
      alias list; empty target prints the whole active table.
    - ``recall``: substring search over alias/name/path.
    - ``reset``: clear the active epoch.
    """
    act = (action or "status").strip().lower()
    if _broker_enabled() and act in {"status", "reset"}:
        try:
            params = _broker_base_params()
            if act == "status":
                status = await _broker_call("render.status", params)
                if isinstance(status, dict):
                    status = cast(dict[str, object], status)
                    return (
                        f"render-memory epoch={status.get('epoch', 0)} "
                        f"gen={status.get('generation', 0)} aliases={status.get('aliases', 0)} "
                        f"clients={len(_wire_dict(status, 'clients') or {})} "
                        f"mode=broker"
                    )
            else:
                params.update({"reason": "lsp_memory reset"})
                status = await _broker_call("render.reset_session", params)
                if isinstance(status, dict):
                    status = cast(dict[str, object], status)
                    return (
                        f"render-memory reset: epoch={status.get('epoch', 0)} "
                        f"gen={status.get('generation', 0)} mode=broker"
                    )
        except BrokerError as e:
            if _broker_mode() == "on" or not _broker_unavailable(e):
                return f"broker render-memory {act} failed: {e.code}: {e}"

    snapshot = _render_memory.snapshot()
    records = list(snapshot.records)
    if act == "status":
        return (
            f"render-memory epoch={snapshot.epoch_id} gen={snapshot.generation} "
            f"aliases={len(records)} mode={mode or 'auto'}"
        )
    if act == "reset":
        _local_alias_coordinator.clear_epoch()
        return f"render-memory reset: epoch={_render_memory.epoch_id} gen={_render_memory.generation}"
    if act == "legend":
        selected = records
        if target.strip():
            selected = []
            for raw in target.split(","):
                result = _render_memory.lookup(raw.strip())
                if result.ok and result.record is not None:
                    selected.append(result.record)
        if not records:
            return "No render-memory aliases."
        return _render_memory.aliases_for_response(selected)
    if act == "recall":
        query = target.strip().lower()
        if not query:
            return "action=recall requires target"
        matches: list[AliasRecord] = []
        for record in records:
            ident = record.identity
            haystack = " ".join(
                str(part)
                for part in (record.alias, ident.name, ident.symbol_kind, ident.path, ident.bucket_label)
            ).lower()
            if query in haystack:
                matches.append(record)
        if not matches:
            return f"No render-memory aliases match {target!r}."
        return _render_memory.aliases_for_response(matches)
    return "Unknown action: {!r}. Valid: status, legend, recall, reset.".format(action)


def _session_resolve_indices(server: str) -> list[int] | str:
    """Map a server label/command/name filter to chain indices. Empty filter = all."""
    if not server:
        return list(range(len(_chain_configs)))
    query = server.strip()
    matches = [
        i for i, cfg in enumerate(_chain_configs)
        if query in {cfg.label, cfg.command, cfg.name}
    ]
    if not matches:
        labels = ", ".join(f"{cfg.label} ({cfg.command})" for cfg in _chain_configs) or "(none)"
        return f"No chain server matches {server!r}. Known: {labels}"
    return matches


async def _session_status() -> str:
    import importlib.metadata as _imd
    import subprocess as _subp

    module_file = Path(__file__).resolve()
    lines: list[str] = []

    try:
        pkg_root = module_file.parent.parent.parent
        git_dir = pkg_root / ".git"
        if git_dir.exists():
            sha = _subp.run(
                ["git", "-C", str(pkg_root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            lines.append(f"cc-lsp-now: {pkg_root} @ {sha or 'unknown'}")
        else:
            lines.append(f"cc-lsp-now install: {pkg_root} (no .git — installed package)")
    except Exception as e:
        lines.append(f"cc-lsp-now introspection failed: {e}")

    try:
        lines.append(f"version: {_imd.version('cc-lsp-now')}")
    except Exception:
        pass

    broker_state = "enabled" if _broker_enabled() else "disabled"
    lines.append(f"broker: {_broker_mode()} ({broker_state})")
    broker_status = await _broker_lsp_status() if _broker_enabled() else None
    if broker_status:
        lines.append(f"broker pid: {broker_status.get('pid')}")
        lines.append(f"broker socket: {broker_status.get('socket')}")
        lines.append(f"broker log: {broker_status.get('log_path')}")
        lines.append(f"broker idle_ttl: {broker_status.get('idle_ttl_seconds')}s")
        lines.append("")
        lines.append("Broker sessions:")
        sessions = _wire_list(broker_status, "sessions")
        if not sessions:
            lines.append("  (none)")
        for session_obj in sessions:
            if not isinstance(session_obj, dict):
                continue
            session = cast(dict[str, object], session_obj)
            lines.append(
                "  "
                f"{session.get('session_id')} root={session.get('root')} "
                f"hash={session.get('config_hash')} clients={session.get('client_count')}"
            )
            lsp = _wire_dict(session, "lsp")
            if lsp is None:
                continue
            lines.append(
                "    "
                f"requests={lsp.get('request_count', 0)} last={lsp.get('last_method') or '-'} "
                f"via={lsp.get('last_server_label') or '-'} {lsp.get('last_duration_ms', 0)}ms"
            )
            handlers = lsp.get("method_handlers", {})
            if isinstance(handlers, dict) and handlers:
                rendered = ", ".join(f"{k}->{v}" for k, v in sorted(handlers.items()))
                lines.append(f"    routes: {rendered}")
            for client_obj in _wire_list(lsp, "clients"):
                if not isinstance(client_obj, dict):
                    continue
                client = cast(dict[str, object], client_obj)
                label = client.get("label", "server")
                state = client.get("state", "unknown")
                pid = client.get("pid") or "-"
                open_docs = client.get("open_documents", 0)
                req_count = client.get("request_count", 0)
                folders = client.get("folders", [])
                folder_text = ", ".join(str(f) for f in folders) if isinstance(folders, list) else ""
                lines.append(
                    f"    [{label}] {state} pid={pid} open={open_docs} requests={req_count}: "
                    f"{folder_text or '(no folders)'}"
                )

    _ensure_chain_configs()
    now = time.time()
    lines.append("")
    lines.append("Chain:")
    if not _chain_configs:
        lines.append("  (no chain configured)")
        return "\n".join(lines)

    for idx, cfg in enumerate(_chain_configs):
        client = _chain_clients[idx] if idx < len(_chain_clients) else None
        state = "live" if client is not None else "not spawned"
        lines.append(f"  [{cfg.label}] {state}: {cfg.command} {' '.join(cfg.args)}")

        caps = client.capabilities if client is not None else (_probed_caps[idx] if idx < len(_probed_caps) else {})
        caps_source = "live" if client is not None else "probe"
        if caps:
            providers = sorted(k for k in caps.keys() if k.endswith("Provider") or k == "workspace")
            lines.append(f"    caps ({caps_source}): {len(caps)}; providers: {', '.join(providers)}")
            ws_caps = caps.get("workspace", {})
            file_ops = ws_caps.get("fileOperations", {}) if isinstance(ws_caps, dict) else {}
            if file_ops:
                lines.append(f"    fileOperations: {', '.join(sorted(file_ops.keys()))}")
        else:
            lines.append(f"    caps ({caps_source}): (none reported)")

        if client is None:
            lines.append("    folders: (server not yet spawned)")
            continue
        folders = sorted(client.workspace_folders)
        if not folders:
            lines.append("    folders: (none)")
        else:
            lines.append("    folders:")
            for folder in folders:
                stats = _folder_warmup_stats.get((idx, folder))
                if stats:
                    age = int(now - stats.timestamp)
                    lines.append(f"      {folder}  (warmed {stats.count} files, {age}s ago)")
                else:
                    lines.append(f"      {folder}  (not warmed)")
    return "\n".join(lines)


async def _session_add(path: str) -> str:
    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        return f"Not a directory: {abs_path}"

    if _broker_enabled():
        params = _broker_base_params()
        params["path"] = abs_path
        try:
            result = await _broker_call("lsp.add_workspace", params)
        except BrokerError as e:
            if _broker_mode() == "on":
                return f"broker add failed: {e.code}: {e}"
            agent_log(f"broker add failed ({e.code}: {e}); falling back to direct LSP")
        else:
            if isinstance(result, dict):
                result_dict = cast(dict[str, object], result)
                added = result_dict.get("added", [])
                count = len(added) if isinstance(added, list) else 0
                return f"[broker] queued {abs_path}; applied to {count} live client(s)"
            return f"[broker] queued {abs_path}"

    _ensure_chain_configs()
    for idx in range(len(_chain_configs)):
        await _get_client(idx)

    results: list[str] = []
    for idx, cfg in enumerate(_chain_configs):
        client = _chain_clients[idx]
        assert client is not None
        added = client.add_workspace_folder(abs_path)
        if added:
            warmed = await _maybe_warmup(client, idx, abs_path)
            suffix = f" — warmed {warmed} files" if warmed else ""
            results.append(f"[{cfg.label}] added{suffix}")
        else:
            results.append(f"[{cfg.label}] already present")
    return "\n".join(results)


async def _session_warm(path: str, server: str) -> str:
    abs_path = os.path.abspath(path) if path else ""
    if abs_path and not os.path.isdir(abs_path):
        return f"Not a directory: {abs_path}"

    if _broker_enabled():
        return (
            "broker mode: warmup is centralized by live broker sessions. "
            "Use action=add to register a folder; direct per-process warm is disabled."
        )

    _ensure_chain_configs()
    indices = _session_resolve_indices(server)
    if isinstance(indices, str):
        return indices

    # Spawn target servers so warm has a client to talk to.
    for idx in indices:
        await _get_client(idx)

    results: list[str] = []
    for idx in indices:
        cfg = _chain_configs[idx]
        client = _chain_clients[idx]
        assert client is not None
        if abs_path:
            if abs_path not in client.workspace_folders:
                results.append(f"[{cfg.label}] {abs_path} is not registered; use action=add first")
                continue
            targets = [abs_path]
        else:
            targets = sorted(client.workspace_folders)

        for folder in targets:
            # Drop the once-per-folder cache so warmup actually re-runs.
            _warmed_folders.discard((idx, folder))
            n = await _warmup_folder(client, folder)
            _folder_warmup_stats[(idx, folder)] = WarmupStats(count=n, timestamp=time.time())
            _warmed_folders.add((idx, folder))
            results.append(f"[{cfg.label}] {folder} — warmed {n} files")
    return "\n".join(results) if results else "No targets to warm."


async def _session_restart(server: str) -> str:
    if _broker_enabled():
        stopped = await _broker_stop_matching()
        return (
            f"[broker] stopped {len(stopped)} session(s); next request will spawn fresh"
            if stopped else
            "[broker] no matching live session to restart"
        )

    _ensure_chain_configs()
    indices = _session_resolve_indices(server)
    if isinstance(indices, str):
        return indices

    for method, handler_idx in list(_method_handler.items()):
        if handler_idx is None or handler_idx in indices:
            _method_handler.pop(method, None)

    results: list[str] = []
    for idx in indices:
        cfg = _chain_configs[idx]
        client = _chain_clients[idx]
        if client is None:
            results.append(f"[{cfg.label}] not running — will spawn fresh on next call")
            continue
        extra_folders = sorted(folder for folder in client.workspace_folders if folder != client._root_path)
        try:
            await client.stop()
            stopped = True
        except Exception as e:
            stopped = False
            results.append(f"[{cfg.label}] stop failed: {e}")
        _chain_clients[idx] = None
        # Drop warmup memo so the fresh server gets re-indexed lazily.
        for key in list(_warmed_folders):
            if key[0] == idx:
                _warmed_folders.discard(key)
        for key in list(_folder_warmup_stats):
            if key[0] == idx:
                _folder_warmup_stats.pop(key, None)
        if stopped:
            try:
                restarted = await _get_client(idx)
                restored = 0
                for folder in extra_folders:
                    if restarted.add_workspace_folder(folder):
                        restored += 1
                    await _maybe_warmup(restarted, idx, folder)
                suffix = f" — restored {restored} workspace folder(s)" if extra_folders else ""
                results.append(f"[{cfg.label}] restarted{suffix}")
            except Exception as e:
                results.append(f"[{cfg.label}] respawn failed: {e}")
    return "\n".join(results) if results else "No servers to restart."


async def _broker_stop_matching() -> list[str]:
    current = _broker_base_params()
    try:
        result = await _broker_call(
            "session.stop_matching",
            {
                "root": str(current["root"]),
                "config_hash": str(current["config_hash"]),
            },
        )
    except BrokerError as e:
        raise RuntimeError(f"broker stop failed: {e.code}: {e}") from None
    if isinstance(result, dict):
        stopped = cast(dict[str, object], result).get("stopped", [])
        if isinstance(stopped, list):
            return [sid for sid in stopped if isinstance(sid, str)]
    return []


async def _session_stop(server: str) -> str:
    if _broker_enabled():
        try:
            stopped = await _broker_stop_matching()
        except RuntimeError as e:
            return str(e)
        return (
            f"[broker] stopped {len(stopped)} session(s)"
            if stopped else
            "[broker] no matching live session to stop"
        )

    _ensure_chain_configs()
    indices = _session_resolve_indices(server)
    if isinstance(indices, str):
        return indices

    for method, handler_idx in list(_method_handler.items()):
        if handler_idx is None or handler_idx in indices:
            _method_handler.pop(method, None)

    results: list[str] = []
    for idx in indices:
        cfg = _chain_configs[idx]
        client = _chain_clients[idx]
        if client is None:
            results.append(f"[{cfg.label}] not running")
            continue
        try:
            await client.stop()
            results.append(f"[{cfg.label}] stopped")
        except Exception as e:
            results.append(f"[{cfg.label}] stop failed: {e}")
        finally:
            _chain_clients[idx] = None
    return "\n".join(results) if results else "No servers to stop."


async def lsp_confirm(index: int = 0, stage: str = "") -> str:
    """Apply one staged candidate from a pending preview.

    Companion to tools that stage previews (currently ``lsp_fix``,
    ``lsp_rename``, and ``lsp_move``). Without ``stage``, ``lsp_confirm(0)``
    targets the *active* stage — the most recently staged preview — which is
    the legacy single-slot behavior agents already rely on.

    Pass ``stage="<handle>"`` to commit a specific named stage when multiple
    previews coexist (parallel-agent / multi-stage flows; see
    ``docs/agent-tool-roadmap.md``). The targeted stage is dropped on
    successful apply, while every other stage in the pending book stays
    intact.
    """
    global _pending
    if stage:
        target = _pending_book.get(stage)
        if target is None:
            handles = _pending_book.handles()
            known = ", ".join(handles) if handles else "(none)"
            return f"No pending stage named {stage!r}. Active stages: {known}."
    else:
        target = _pending_book.active()
    if target is None:
        return "Nothing to confirm."

    candidates = target.candidates
    kind = target.kind

    if index < 0 or index >= len(candidates):
        return f"Invalid index {index}, only {len(candidates)} candidates available."

    candidate = candidates[index]
    try:
        file_count, edit_count = _apply_candidate(candidate)
    except (OSError, ValueError, KeyError) as e:
        return f"Apply failed: {e}"

    _pending_book.drop(target.handle)
    _pending = _pending_book.active()
    return f"Applied [{kind} #{index}]: {candidate.title}. {file_count} file(s), {edit_count} edit(s)."


def _call_item_to_group(item: dict) -> SemanticGrepGroup:
    """Wrap a CallHierarchyItem as a SemanticGrepGroup so call-edge targets land
    in the same nav context as ``lsp_grep`` / ``lsp_symbols_at`` results.

    The hit is anchored at the item's ``selectionRange`` start (the function's
    name token) so a follow-up ``lsp_symbol([N])`` / ``lsp_refs([N])`` hits the
    correct identifier without re-resolving via text.
    """
    name = item.get("name", "")
    uri = item.get("uri", "")
    path = _uri_to_path(uri)
    sel_range = item.get("selectionRange") or item.get("range") or {}
    sel_start = sel_range.get("start", {})
    line0 = sel_start.get("line", 0)
    char0 = sel_start.get("character", 0)
    line_text = _line_text(path, line0 + 1)
    hit = SemanticGrepHit(
        path=path,
        line=line0,
        character=char0,
        line_text=line_text,
        uri=uri,
        pos={"line": line0, "character": char0},
    )
    kind_label = _symbol_kind_label(item.get("kind", 0)).lower()
    return SemanticGrepGroup(
        key=f"{path}:{line0}:{char0}:{name}",
        name=name,
        kind=kind_label,
        type_text="",
        definition_path=path,
        definition_line=line0 + 1,
        definition_character=char0,
        hits=[hit],
    )


async def _walk_call_edges(
    direction: str,
    root_item: dict,
    max_depth: int,
    max_edges: int,
) -> list[tuple[dict, int]]:
    """BFS-expand call-hierarchy edges in one direction.

    Returns ``[(call_record, depth)]`` where ``call_record`` is the LSP
    ``CallHierarchyIncomingCall`` / ``CallHierarchyOutgoingCall`` envelope.
    Stops as soon as ``max_edges`` is reached so the caller can interleave
    incoming and outgoing under a shared budget.
    """
    if max_edges <= 0:
        return []
    method = (
        "callHierarchy/incomingCalls" if direction == "in"
        else "callHierarchy/outgoingCalls"
    )
    target_key = "from" if direction == "in" else "to"
    edges: list[tuple[dict, int]] = []
    layer: list[dict] = [root_item]
    seen: set[tuple[str, int, int]] = set()
    for depth in range(1, max_depth + 1):
        next_layer: list[dict] = []
        for item in layer:
            try:
                result = await _request(method, {"item": item})
            except LspError:
                continue
            if not result:
                continue
            for call in result:
                edges.append((call, depth))
                target = call.get(target_key, {})
                sel = target.get("selectionRange") or target.get("range") or {}
                start = sel.get("start", {})
                key = (
                    target.get("uri", ""),
                    start.get("line", 0),
                    start.get("character", 0),
                )
                if key not in seen:
                    seen.add(key)
                    next_layer.append(target)
                if len(edges) >= max_edges:
                    return edges
        layer = next_layer
        if not layer:
            break
    return edges


async def _call_graph_sections_for_target(
    resolved: SemanticTarget,
    direction_key: str,
    max_depth: int,
    max_edges: int,
    *,
    heading_prefix: str = "Calls for",
) -> tuple[list[str], list[SemanticGrepGroup]]:
    items = await _request("textDocument/prepareCallHierarchy", {
        "textDocument": {"uri": resolved.uri},
        "position": resolved.pos,
    }, uri=resolved.uri)
    anchor_name = resolved.name or (items[0].get("name", "") if items else "")
    head = f"{heading_prefix} {anchor_name} ({resolved.path}:L{resolved.line})" if anchor_name \
        else f"{heading_prefix} {resolved.path}:L{resolved.line}"
    if not items:
        return [head, "No call hierarchy item at this position."], []

    directions = ["in", "out"] if direction_key == "both" else [direction_key]
    edges_by_dir: dict[str, list[tuple[dict, int]]] = {}
    for d in directions:
        edges_by_dir[d] = await _walk_call_edges(d, items[0], max_depth, max_edges)

    total = sum(len(v) for v in edges_by_dir.values())
    groups: list[SemanticGrepGroup] = []
    sections = [head]

    if total == 0:
        sections.append("No calls.")
        return sections, groups

    for d in directions:
        edges = edges_by_dir.get(d, [])
        if not edges:
            sections.append(f"{d}: (none)")
            continue
        target_key = "from" if d == "in" else "to"
        sections.append(f"{d}:")
        for call, depth in edges:
            target_item = call.get(target_key, {})
            n_sites = max(1, len(call.get("fromRanges", [])))
            group = _call_item_to_group(target_item)
            idx = len(groups)
            groups.append(group)
            site_label = f"{n_sites} site{'s' if n_sites != 1 else ''}"
            depth_label = f" — depth {depth}" if depth > 1 else ""
            sections.append(_compact_line(
                f"  [{idx}] {Path(group.definition_path).name}:L{group.definition_line}"
                f"::{group.name} — {group.kind} — {site_label}{depth_label}",
                240,
            ))
        if len(edges) >= max_edges:
            sections.append(f"  ... stopped at {max_edges} {d} edge(s); raise max_edges to unfold.")
    return sections, groups


def _renumber_graph_rows(lines: list[str], offset: int) -> list[str]:
    """Shift ``[N]`` row handles after concatenating multiple graph sections."""
    if offset <= 0:
        return list(lines)
    renumbered: list[str] = []
    for line in lines:
        renumbered.append(
            re.sub(
                r"^(\s+)\[(\d+)\]",
                lambda m: f"{m.group(1)}[{int(m.group(2)) + offset}]",
                line,
                count=1,
            )
        )
    return renumbered


async def lsp_calls(
    target: str = "",
    direction: str = "both",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_depth: int = 1,
    max_edges: int = 50,
) -> str:
    """Show incoming and/or outgoing call graph edges for one semantic node.

    Workflow replacement for raw ``callHierarchy/incomingCalls`` /
    ``callHierarchy/outgoingCalls`` (see ``docs/tool-surface.md``). Resolves
    ``target`` via ``_resolve_semantic_target`` (graph index ``[N]``, bare
    ``Lxx`` from the last semantic graph, ``file:Lx``, or
    ``file_path``+``symbol``/``line``), runs ``prepareCallHierarchy`` once,
    then expands incoming / outgoing per ``direction`` ("in", "out", "both").

    Each rendered edge is recorded into the semantic nav context so a follow-up
    ``lsp_symbol([N])`` / ``lsp_refs([N])`` / ``lsp_calls([N])`` propagates
    through the call graph without re-resolving.
    """
    direction_key = (direction or "both").strip().lower()
    if direction_key not in {"in", "out", "both"}:
        return "direction must be one of: in, out, both."

    if not target.strip() and file_path and symbol and line <= 0:
        try:
            targets = await _resolve_symbol_targets(file_path, symbol)
        except (LspError, ValueError, RuntimeError) as e:
            return f"LSP error: {e}"
        if len(targets) > 1:
            max_depth = max(1, min(max_depth, 5))
            max_edges = max(1, min(max_edges, 500))
            all_sections: list[str] = [
                f"Calls for {len(targets)} matches of {symbol!r} in {targets[0].path}:"
            ]
            all_groups: list[SemanticGrepGroup] = []
            try:
                for idx, candidate in enumerate(targets):
                    root_group = _semantic_group_from_target(candidate)
                    root_idx = len(all_groups)
                    all_groups.append(root_group)
                    all_sections.append(_compact_line(
                        f"[{root_idx}] root {Path(candidate.path).name}:L{candidate.line}"
                        f"::{root_group.name} — {root_group.kind}",
                        240,
                    ))
                    sections, groups = await _call_graph_sections_for_target(
                        candidate,
                        direction_key,
                        max_depth,
                        max_edges,
                        heading_prefix=f"match {idx}",
                    )
                    offset = len(all_groups)
                    all_groups.extend(groups)
                    all_sections.extend(_renumber_graph_rows(sections, offset))
                _record_semantic_nav_context(f"calls:{symbol}:multi", all_groups)
                return "\n".join(all_sections)
            except (LspError, ValueError, RuntimeError) as e:
                return f"LSP error: {e}"

    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved

    max_depth = max(1, min(max_depth, 5))
    max_edges = max(1, min(max_edges, 500))

    try:
        sections, groups = await _call_graph_sections_for_target(
            resolved,
            direction_key,
            max_depth,
            max_edges,
        )

        nav_query = f"calls:{resolved.name or resolved.path}:L{resolved.line}"
        _record_semantic_nav_context(nav_query, groups)
        return "\n".join(sections)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


def _call_item_to_path_node(item: dict) -> PathNode:
    name = item.get("name", "")
    uri = item.get("uri", "")
    path = _uri_to_path(uri)
    sel_range = item.get("selectionRange") or item.get("range") or {}
    sel_start = sel_range.get("start", {})
    line0 = sel_start.get("line", 0)
    char0 = sel_start.get("character", 0)
    return PathNode(
        key=f"{uri}:{line0}:{char0}",
        name=name,
        kind=_symbol_kind_label(item.get("kind", 0)).lower(),
        path=path,
        line=line0 + 1,
        character=char0,
    )


async def _prepare_call_hierarchy_item(target: SemanticTarget, role: str) -> dict | str:
    items = await _request("textDocument/prepareCallHierarchy", {
        "textDocument": {"uri": target.uri},
        "position": target.pos,
    }, uri=target.uri)
    if not items:
        return f"No call hierarchy item at {role} endpoint {target.path}:L{target.line}."
    return items[0]


class _CallPathOracle:
    def __init__(self, items: list[dict], exclude: str = ""):
        self.items: dict[str, dict] = {}
        self.exclude_terms = [part.strip().lower() for part in re.split(r"[,;\n]+", exclude) if part.strip()]
        for item in items:
            node = _call_item_to_path_node(item)
            self.items[node.key] = item

    def group_for(self, node: PathNode) -> SemanticGrepGroup | None:
        item = self.items.get(node.key)
        if item is None:
            return None
        return _call_item_to_group(item)

    def _excluded(self, node: PathNode) -> bool:
        if not self.exclude_terms:
            return False
        haystack = f"{node.key} {node.name} {node.kind} {node.path}".lower()
        return any(term in haystack for term in self.exclude_terms)

    async def expand(self, node: PathNode, direction: PathDirection, limit: int) -> list[PathEdge]:
        item = self.items.get(node.key)
        if item is None or limit <= 0:
            return []
        directions = ["out", "in"] if direction == "any" else [direction]
        edges: list[PathEdge] = []
        for edge_direction in directions:
            method = (
                "callHierarchy/incomingCalls" if edge_direction == "in"
                else "callHierarchy/outgoingCalls"
            )
            target_key = "from" if edge_direction == "in" else "to"
            try:
                result = await _request(method, {"item": item})
            except LspError:
                continue
            if not result:
                continue
            for call in result:
                target_item = call.get(target_key, {})
                target_node = _call_item_to_path_node(target_item)
                if self._excluded(target_node):
                    continue
                self.items[target_node.key] = target_item
                n_sites = max(1, len(call.get("fromRanges", [])))
                label = f"{n_sites} site{'s' if n_sites != 1 else ''}"
                edges.append(PathEdge(
                    source=node,
                    target=target_node,
                    family="calls",
                    direction=edge_direction,
                    label=label,
                    provenance=method,
                ))
                if len(edges) >= limit:
                    return edges
        return edges


def _path_node_scope(node: PathNode) -> str:
    if node.path:
        return Path(node.path).stem
    return ""


def _path_node_label(node: PathNode) -> str:
    scope = _path_node_scope(node)
    if scope and node.name and node.line > 0:
        return f"{scope}:L{node.line}::{node.name}"
    if node.path and node.line > 0:
        return f"{Path(node.path).name}:L{node.line}"
    return node.name or node.key


def _format_path_node_row(index: int, node: PathNode) -> str:
    scope = _path_node_scope(node)
    line_label = f"L{node.line}" if node.line > 0 else "L?"
    scope_label = f" ::{scope}::" if scope else ""
    kind_label = f" {node.kind}" if node.kind else ""
    name_label = f" {node.name}" if node.name else ""
    return _compact_line(f"[{index}] {line_label}{scope_label}{kind_label}{name_label}", 240)


def _path_edge_arrow(edge: PathEdge) -> str:
    if edge.family == "calls" and edge.direction == "in":
        return "--called-by-->"
    if edge.family == "calls":
        return "--calls-->"
    return f"--{edge.family}-{edge.direction}-->"


def _path_stats_line(result: PathSearchResult) -> str:
    stats = result.stats
    bits = [f"Explored {stats.explored_edges} edges"]
    if stats.pruned_hubs or stats.pruned_branches:
        bits.append(f"pruned {stats.pruned_hubs} hubs")
        bits.append(f"{stats.pruned_branches} branches")
    if stats.budget_exhausted:
        bits.append("budget exhausted")
    return "; ".join(bits) + "."


def _format_path_result(
    result: PathSearchResult,
    *,
    via: str,
    direction: str,
    max_hops: int,
    max_edges: int,
    oracle: _CallPathOracle,
) -> str:
    node_order: list[PathNode] = [result.start]
    seen = {result.start.key}
    if not result.paths and result.goal.key not in seen:
        node_order.append(result.goal)
        seen.add(result.goal.key)
    for path in result.paths:
        for edge in path:
            for node in (edge.source, edge.target):
                if node.key in seen:
                    continue
                seen.add(node.key)
                node_order.append(node)

    groups: list[SemanticGrepGroup] = []
    node_indices: dict[str, int] = {}
    for node in node_order:
        group = oracle.group_for(node)
        if group is None:
            continue
        node_indices[node.key] = len(groups)
        groups.append(group)

    def idx(node: PathNode) -> int:
        return node_indices.get(node.key, -1)

    start_idx = idx(result.start)
    goal_idx = idx(result.goal)
    start_label = f"[{start_idx}] {_path_node_label(result.start)}" if start_idx >= 0 else _path_node_label(result.start)
    goal_label = f"[{goal_idx}] {_path_node_label(result.goal)}" if goal_idx >= 0 else _path_node_label(result.goal)

    lines: list[str] = []
    if not result.paths:
        lines.append(
            f"No path from {start_label} to {goal_label} within "
            f"max_hops={max_hops}, max_edges={max_edges} via {via}."
        )
        lines.append(f"{_path_stats_line(result)} This is not proof no runtime path exists.")
        _record_semantic_nav_context(f"path:{via}:{direction}", groups)
        return "\n".join(lines)

    lines.append(f"Paths from {start_label} to {goal_label} via {via} direction={direction}")
    for path_index, path in enumerate(result.paths):
        lines.append(f"[P{path_index}] cost {len(path)} hops {len(path)} verified")
        if not path:
            lines.append(f"  {_format_path_node_row(idx(result.start), result.start)}")
            continue
        first = path[0].source
        lines.append(f"  {_format_path_node_row(idx(first), first)}")
        for edge in path:
            target_idx = idx(edge.target)
            label = f" {edge.label}" if edge.label else ""
            lines.append(
                f"   {_path_edge_arrow(edge)} {_format_path_node_row(target_idx, edge.target)}{label}"
            )
    stats_line = _path_stats_line(result)
    if result.stats.pruned_hubs or result.stats.pruned_branches or result.stats.budget_exhausted:
        stats_line += " Raise max_edges or max_hops to unfold."
    lines.append(stats_line)
    _record_semantic_nav_context(f"path:{via}:{direction}", groups)
    return "\n".join(lines)


async def lsp_path(
    from_target: str = "",
    to_target: str = "",
    via: str = "calls",
    direction: str = "out",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_hops: int = 4,
    max_edges: int = 200,
    max_paths: int = 3,
    exclude: str = "",
) -> str:
    """Find bounded witness paths between two semantic anchors.

    First implementation slice from ``docs/lsp-path.md``: calls-only,
    explicit direction, hard budgets, and no mixed graph search.
    """
    via_key = (via or "calls").strip().lower()
    if via_key != "calls":
        return "via must be 'calls' in this implementation slice. types/refs path search is not wired yet."

    direction_key = (direction or "out").strip().lower()
    if direction_key not in {"out", "in", "any"}:
        return "direction must be one of: out, in, any."
    path_direction = cast(PathDirection, direction_key)

    if not to_target.strip():
        return "Provide to_target; lsp_path needs both endpoints."

    source = await _resolve_semantic_target(from_target, file_path, symbol, line)
    if isinstance(source, str):
        return source
    destination = await _resolve_semantic_target(to_target)
    if isinstance(destination, str):
        return destination

    max_hops = max(0, min(max_hops, 10))
    max_edges = max(0, min(max_edges, 2000))
    max_paths = max(1, min(max_paths, 10))

    try:
        source_item = await _prepare_call_hierarchy_item(source, "source")
        if isinstance(source_item, str):
            return source_item
        destination_item = await _prepare_call_hierarchy_item(destination, "destination")
        if isinstance(destination_item, str):
            return destination_item

        source_node = _call_item_to_path_node(source_item)
        destination_node = _call_item_to_path_node(destination_item)
        oracle = _CallPathOracle([source_item, destination_item], exclude=exclude)
        result = await find_paths(
            source_node,
            destination_node,
            oracle,
            direction=path_direction,
            max_hops=max_hops,
            max_edges=max_edges,
            max_paths=max_paths,
            max_branch=min(50, max_edges if max_edges > 0 else 1),
        )
        return _format_path_result(
            result,
            via=via_key,
            direction=direction_key,
            max_hops=max_hops,
            max_edges=max_edges,
            oracle=oracle,
        )
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


async def _diagnostics_single(file_path: str) -> str:
    """Get diagnostics for a single file. Returns formatted lines or '(clean)'."""
    file_path = _resolve_file_path(file_path)
    uri = file_uri(file_path)
    diagnostics = []
    try:
        result = await _request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        }, uri=uri)
        diagnostics = result.get("items", []) if result else []
    except LspError:
        diagnostics = await _stored_diagnostics(uri)
    if not diagnostics:
        return "(clean)"
    lines = []
    for d in diagnostics:
        sev = _severity_label(d.get("severity", 0))
        msg = d.get("message", "")
        r = d.get("range", {})
        sl = r.get("start", {}).get("line", 0) + 1
        source = d.get("source", "")
        code = d.get("code", "")
        tag = f"[{source} {code}]" if source else ""
        lines.append(f"{sl}  {sev}  {msg}  {tag}")
    return "\n".join(lines)


async def lsp_diagnostics(file_path: str = "", pattern: str = "") -> str:
    """Get diagnostics (errors, warnings) for one or more files.

    Supports comma-separated file_path or glob pattern for multi-file diagnostics.
    """
    paths = _resolve_paths(file_path, pattern)
    if isinstance(paths, str):
        return paths
    try:
        if len(paths) == 1:
            result = await _diagnostics_single(paths[0])
            if result == "(clean)":
                return "No diagnostics."
            return result
        sections: list[str] = []
        for p in paths:
            body = await _diagnostics_single(p)
            sections.append(f"=== {p} ===\n{body}")
        return "\n\n".join(sections)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_symbol(target: str = "", file_path: str = "", symbol: str = "", line: int = 0) -> str:
    """Inspect one semantic node.

    Accepts a graph index from the last semantic result, explicit ``file:Lx``,
    or ``file_path`` with ``symbol``/``line``. Returns the compact semantic
    bucket plus hover/signature context when available.
    """
    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved
    try:
        group = await _semantic_group_for_target(resolved)
        lines = [f"Target: {resolved.path}:L{resolved.line}:{resolved.character + 1}"]
        text = _line_text(resolved.path, resolved.line)
        if text:
            lines.append(f"  {text}")
        if group is not None:
            lines.append(_format_semantic_grep_group(0, group))

        try:
            hover = await _request("textDocument/hover", {
                "textDocument": {"uri": resolved.uri},
                "position": resolved.pos,
            }, uri=resolved.uri)
        except LspError:
            hover = None
        hover_summary = _hover_text(hover)
        if hover_summary:
            lines.append(f"hover: {_compact_line(hover_summary, 220)}")

        try:
            signature = await _request("textDocument/signatureHelp", {
                "textDocument": {"uri": resolved.uri},
                "position": resolved.pos,
            }, uri=resolved.uri)
        except LspError:
            signature = None
        signature_summary = _format_signature_summary(signature)
        if signature_summary:
            lines.append(f"signature: {signature_summary}")

        if len(lines) == 1:
            lines.append("No semantic information available.")
        return "\n".join(lines)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


async def lsp_goto(
    target: str = "",
    mode: str = "all",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
) -> str:
    """Resolve destinations for one semantic node.

    ``mode`` may be ``all``, ``def``, ``decl``, ``type``, or ``impl``.
    """
    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved

    mode_key = mode.strip().lower() or "all"
    method_map = {
        "def": ("definition", "textDocument/definition"),
        "definition": ("definition", "textDocument/definition"),
        "decl": ("declaration", "textDocument/declaration"),
        "declaration": ("declaration", "textDocument/declaration"),
        "type": ("type", "textDocument/typeDefinition"),
        "type_definition": ("type", "textDocument/typeDefinition"),
        "impl": ("implementation", "textDocument/implementation"),
        "implementation": ("implementation", "textDocument/implementation"),
    }
    if mode_key == "all":
        requests = [
            ("definition", "textDocument/definition"),
            ("declaration", "textDocument/declaration"),
            ("type", "textDocument/typeDefinition"),
            ("implementation", "textDocument/implementation"),
        ]
    elif mode_key in method_map:
        requests = [method_map[mode_key]]
    else:
        return "mode must be one of: all, def, decl, type, impl."

    lines = [f"Target: {resolved.path}:L{resolved.line}:{resolved.character + 1}"]
    found = False
    for title, method in requests:
        try:
            result = await _request(method, {
                "textDocument": {"uri": resolved.uri},
                "position": resolved.pos,
            }, uri=resolved.uri)
        except LspError as e:
            if mode_key != "all":
                return f"LSP error: {e}"
            continue
        locs = _locations_from_lsp(result)
        if not locs:
            continue
        found = True
        lines.extend(_format_location_section(title, locs))

    if not found:
        lines.append("No destinations found.")
    return "\n".join(lines)


async def lsp_refs(
    target: str = "",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    include_declaration: bool = True,
    max_refs: int = 100,
) -> str:
    """Expand references for a known semantic node or graph index."""
    max_refs = max(1, min(max_refs, 500))
    if not target.strip() and file_path and symbol and line <= 0:
        try:
            targets = await _resolve_symbol_targets(file_path, symbol)
        except (LspError, ValueError, RuntimeError) as e:
            return f"LSP error: {e}"
        if len(targets) > 1:
            lines = [
                f"References for {len(targets)} matches of {symbol!r} in {targets[0].path}:"
            ]
            groups: list[SemanticGrepGroup] = []
            try:
                for idx, candidate in enumerate(targets):
                    section, group = await _reference_section_for_target(
                        candidate,
                        include_declaration,
                        max_refs,
                        heading=f"match {idx} {candidate.name or 'symbol'} ({candidate.path}:L{candidate.line})",
                    )
                    lines.extend(section)
                    if group is not None:
                        groups.append(group)
                    else:
                        groups.append(_semantic_group_from_target(candidate))
                _record_semantic_nav_context(f"refs:{symbol}:multi", groups)
                return "\n".join(lines)
            except (LspError, ValueError, RuntimeError) as e:
                return f"LSP error: {e}"

    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved
    try:
        lines, _group = await _reference_section_for_target(
            resolved,
            include_declaration,
            max_refs,
        )
        if not lines:
            return "No references found."
        return "\n".join(lines)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


async def _reference_section_for_target(
    resolved: SemanticTarget,
    include_declaration: bool,
    max_refs: int,
    *,
    heading: str = "",
) -> tuple[list[str], SemanticGrepGroup | None]:
    result = await _request("textDocument/references", {
        "textDocument": {"uri": resolved.uri},
        "position": resolved.pos,
        "context": {"includeDeclaration": include_declaration},
    }, uri=resolved.uri)
    locs = _locations_from_lsp(result)
    if not locs:
        return ([f"{heading}: 0"] if heading else []), None

    group = await _semantic_group_for_target(resolved)
    if group is not None:
        group.reference_locs = locs
        label = f"{group.kind} {group.name}"
    else:
        label = resolved.name or "symbol"

    lines = [f"{heading}: {len(locs)}" if heading else f"References for {label}: {len(locs)}"]
    for loc in locs[:max_refs]:
        lines.append(f"  {_format_location_with_context(loc)}")
    if len(locs) > max_refs:
        lines.append(f"... {len(locs) - max_refs} more; raise max_refs to unfold.")
    return lines, group


async def _semantic_doc_symbols(path: str, uri: str, cache: dict[str, list[dict]]) -> list[dict]:
    if path in cache:
        return cache[path]
    try:
        symbols = await _request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        }, uri=uri)
    except LspError:
        symbols = []
    cache[path] = symbols or []
    return cache[path]


async def _semantic_definition_locs(hit: SemanticGrepHit, name: str) -> list[dict]:
    try:
        result = await _request("textDocument/definition", {
            "textDocument": {"uri": hit.uri},
            "position": hit.pos,
        }, uri=hit.uri)
        locs = _locations_from_lsp(result)
        if locs:
            return locs
    except LspError:
        pass
    try:
        result = await _request("textDocument/declaration", {
            "textDocument": {"uri": hit.uri},
            "position": hit.pos,
        }, uri=hit.uri)
        locs = _locations_from_lsp(result)
        if locs:
            return locs
    except LspError:
        pass
    return [{
        "uri": hit.uri,
        "range": {
            "start": hit.pos,
            "end": {"line": hit.line, "character": hit.character + _py_index_to_utf16_units(name, len(name))},
        },
    }]


async def _semantic_group_for_hit(
    name: str,
    hit: SemanticGrepHit,
    symbols_by_path: dict[str, list[dict]],
) -> SemanticGrepGroup:
    loc = (await _semantic_definition_locs(hit, name))[0]
    def_uri = loc.get("uri", hit.uri)
    def_path = _uri_to_path(def_uri)
    def_start = loc.get("range", {}).get("start", {})
    def_line = def_start.get("line", hit.line) + 1
    def_character = def_start.get("character", hit.character)
    try:
        hover = await _request("textDocument/hover", {
            "textDocument": {"uri": hit.uri},
            "position": hit.pos,
        }, uri=hit.uri)
    except LspError:
        hover = None
    kind, type_text = _semantic_kind_and_type(name, hover)
    symbols = await _semantic_doc_symbols(def_path, def_uri, symbols_by_path)
    return SemanticGrepGroup(
        key=_semantic_location_key(loc),
        name=name,
        kind=kind,
        type_text=type_text,
        definition_path=def_path,
        definition_line=def_line,
        definition_character=def_character,
        hits=[hit],
        context_symbols=symbols,
    )


async def _fill_reference_locs(group: SemanticGrepGroup) -> None:
    hit = group.hits[0]
    try:
        refs = await _request("textDocument/references", {
            "textDocument": {"uri": hit.uri},
            "position": hit.pos,
            "context": {"includeDeclaration": True},
        }, uri=hit.uri)
        group.reference_locs = _locations_from_lsp(refs)
    except LspError:
        group.reference_locs = []


async def _semantic_group_for_target(target: SemanticTarget) -> SemanticGrepGroup | None:
    if target.group is not None:
        if not target.group.reference_locs:
            await _fill_reference_locs(target.group)
        return target.group
    name = target.name or _identifier_at_position(target.path, target.pos)
    if not name:
        return None
    hit = SemanticGrepHit(
        path=target.path,
        line=target.pos.get("line", 0),
        character=target.pos.get("character", 0),
        line_text=_line_text(target.path, target.line),
        uri=target.uri,
        pos=target.pos,
    )
    group = await _semantic_group_for_hit(name, hit, {})
    await _fill_reference_locs(group)
    return group


def _format_location_with_context(loc: dict) -> str:
    path = _uri_to_path(loc.get("uri", ""))
    start = loc.get("range", {}).get("start", {})
    line = start.get("line", 0) + 1
    snippet = _line_text(path, line)
    if snippet:
        return _compact_line(f"{Path(path).name}:L{line}  {snippet}", 220)
    return f"{Path(path).name}:L{line}  {path}"


def _format_location_section(title: str, locs: list[dict]) -> list[str]:
    if not locs:
        return []
    lines = [f"{title}:"]
    lines.extend(f"  {_format_location_with_context(loc)}" for loc in locs)
    return lines


def _format_signature_summary(result: Any) -> str:
    if not result or not isinstance(result, dict) or not result.get("signatures"):
        return ""
    signatures = result.get("signatures", [])
    active_sig = result.get("activeSignature", 0)
    if active_sig < 0 or active_sig >= len(signatures):
        active_sig = 0
    label = str(signatures[active_sig].get("label", "")).strip()
    return _compact_line(label, 220)


async def lsp_grep(
    query: str,
    file_path: str = "",
    pattern: str = "",
    max_hits: int = 200,
    max_groups: int = 30,
) -> str:
    """Semantic grep for an identifier.

    Scans text candidates, asks the LSP what each occurrence binds to, groups
    by definition identity, and returns compact one-line semantic buckets.
    """
    query = query.strip()
    if not _IDENTIFIER_RE.match(query):
        return "Provide a single identifier, e.g. query='ctx'."
    max_hits = max(1, min(max_hits, 1000))
    max_groups = max(1, min(max_groups, 100))

    try:
        roots = await _known_workspace_roots()
        paths = _semantic_grep_paths(file_path, pattern, roots, _semantic_grep_max_files())
        hits = _semantic_grep_text_hits(paths, query, max_hits)
        if not hits:
            return f"No text candidates for {query!r}."

        groups: dict[str, SemanticGrepGroup] = {}
        symbols_by_path: dict[str, list[dict]] = {}

        for hit in hits:
            group_for_hit = await _semantic_group_for_hit(query, hit, symbols_by_path)
            key = group_for_hit.key
            group = groups.get(key)
            if group is None:
                group = group_for_hit
                groups[key] = group
            elif hit not in group.hits:
                group.hits.append(hit)

        for group in groups.values():
            await _fill_reference_locs(group)

        ordered = list(groups.values())
        legend = _record_semantic_nav_context(query, ordered)
        lines = [
            _format_semantic_grep_group(i, group)
            for i, group in enumerate(ordered[:max_groups])
        ]
        if legend:
            lines.append(legend)
        if len(ordered) > max_groups:
            lines.append(f"... {len(ordered) - max_groups} more group(s); raise max_groups to unfold.")
        if len(hits) >= max_hits:
            lines.append(f"... stopped after {max_hits} text hit(s); raise max_hits to search deeper.")
        return "\n".join(lines)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


async def lsp_symbols_at(target: str = "", file_path: str = "", line: int = 0) -> str:
    """List semantic symbols on a source line.

    Accepts explicit ``path:L78`` or, after ``lsp_grep``, a bare ``L78`` from
    the previous graph's refs/samples. Returns one-line symbol buckets for
    every identifier on the line, including function declaration arguments.
    """
    resolved = _resolve_line_target(target, file_path, line)
    if isinstance(resolved, str):
        return resolved
    path, target_line = resolved
    if not Path(path).exists():
        return f"File not found: {path}"

    uri = file_uri(path)
    try:
        await _request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}, uri=uri)
    except LspError:
        pass

    hits = _identifier_hits_on_line(path, target_line)
    if not hits:
        return f"No identifier tokens found at {path}:L{target_line}."

    symbols_by_path: dict[str, list[dict]] = {}
    groups: dict[str, SemanticGrepGroup] = {}
    for name, hit in hits:
        group_for_hit = await _semantic_group_for_hit(name, hit, symbols_by_path)
        if group_for_hit.key not in groups:
            groups[group_for_hit.key] = group_for_hit

    ordered = list(groups.values())
    for group in ordered:
        await _fill_reference_locs(group)
    legend = _record_semantic_nav_context(f"{Path(path).name}:L{target_line}", ordered)

    lines = [f"Target: {path}:L{target_line}"]
    try:
        line_text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[target_line - 1]
        lines.append(f"  {line_text.strip()}")
    except (OSError, IndexError):
        pass
    lines.extend(_format_semantic_grep_group(i, group) for i, group in enumerate(ordered))
    if legend:
        lines.append(legend)
    return "\n".join(lines)


async def _walk_type_edges(
    direction: str,
    root_item: dict,
    max_depth: int,
    max_edges: int,
) -> list[tuple[dict, int]]:
    """BFS-expand type-hierarchy edges in one direction.

    ``direction`` is ``"super"`` (parents) or ``"sub"`` (children). Returns
    ``[(type_item, depth)]`` — TypeHierarchyItem records, since unlike call
    hierarchy responses there is no per-edge envelope to unwrap. Stops as
    soon as ``max_edges`` is reached so the caller can interleave super and
    sub under a shared budget.
    """
    if max_edges <= 0:
        return []
    method = (
        "typeHierarchy/supertypes" if direction == "super"
        else "typeHierarchy/subtypes"
    )
    edges: list[tuple[dict, int]] = []
    layer: list[dict] = [root_item]
    seen: set[tuple[str, int, int]] = set()
    for depth in range(1, max_depth + 1):
        next_layer: list[dict] = []
        for item in layer:
            try:
                result = await _request(method, {"item": item})
            except LspError:
                continue
            if not result:
                continue
            for type_item in result:
                edges.append((type_item, depth))
                sel = type_item.get("selectionRange") or type_item.get("range") or {}
                start = sel.get("start", {})
                key = (
                    type_item.get("uri", ""),
                    start.get("line", 0),
                    start.get("character", 0),
                )
                if key not in seen:
                    seen.add(key)
                    next_layer.append(type_item)
                if len(edges) >= max_edges:
                    return edges
        layer = next_layer
        if not layer:
            break
    return edges


async def lsp_types(
    target: str = "",
    direction: str = "both",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
    max_depth: int = 1,
    max_edges: int = 50,
) -> str:
    """Show super and/or sub type hierarchy edges for one semantic node.

    Workflow replacement for raw ``typeHierarchy/supertypes`` /
    ``typeHierarchy/subtypes`` (see ``docs/tool-surface.md``). Mirrors
    ``lsp_calls``: resolves ``target`` via ``_resolve_semantic_target``
    (graph index ``[N]``, bare ``Lxx`` from the last semantic graph,
    ``file:Lx``, or ``file_path``+``symbol``/``line``), runs
    ``prepareTypeHierarchy`` once, then expands supertypes / subtypes per
    ``direction`` ("super", "sub", "both"). ``supertypes`` / ``subtypes``
    are accepted as aliases.

    Each rendered edge is recorded into the semantic nav context so a
    follow-up ``lsp_symbol([N])`` / ``lsp_refs([N])`` / ``lsp_types([N])``
    propagates through the type graph without re-resolving.
    """
    direction_raw = (direction or "both").strip().lower()
    aliases = {"supertypes": "super", "subtypes": "sub"}
    direction_key = aliases.get(direction_raw, direction_raw)
    if direction_key not in {"super", "sub", "both"}:
        return "direction must be one of: super, sub, both."

    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved

    max_depth = max(1, min(max_depth, 5))
    max_edges = max(1, min(max_edges, 500))

    try:
        items = await _request("textDocument/prepareTypeHierarchy", {
            "textDocument": {"uri": resolved.uri},
            "position": resolved.pos,
        }, uri=resolved.uri)
        if not items:
            return "No type hierarchy item at this position."

        directions = ["super", "sub"] if direction_key == "both" else [direction_key]
        edges_by_dir: dict[str, list[tuple[dict, int]]] = {}
        for d in directions:
            edges = await _walk_type_edges(d, items[0], max_depth, max_edges)
            edges_by_dir[d] = edges

        total = sum(len(v) for v in edges_by_dir.values())
        groups: list[SemanticGrepGroup] = []
        sections: list[str] = []

        anchor_name = resolved.name or items[0].get("name", "")
        head = f"Types for {anchor_name} ({resolved.path}:L{resolved.line})" if anchor_name \
            else f"Types for {resolved.path}:L{resolved.line}"
        sections.append(head)

        if total == 0:
            sections.append("No types.")
        else:
            for d in directions:
                edges = edges_by_dir.get(d, [])
                if not edges:
                    sections.append(f"{d}: (none)")
                    continue
                sections.append(f"{d}:")
                for type_item, depth in edges:
                    # TypeHierarchyItem and CallHierarchyItem share shape
                    # (name/kind/uri/range/selectionRange), so the call-edge
                    # group builder doubles as the type-edge group builder.
                    group = _call_item_to_group(type_item)
                    idx = len(groups)
                    groups.append(group)
                    depth_label = f" — depth {depth}" if depth > 1 else ""
                    sections.append(_compact_line(
                        f"  [{idx}] {Path(group.definition_path).name}:L{group.definition_line}"
                        f"::{group.name} — {group.kind}{depth_label}",
                        240,
                    ))
                if len(edges) >= max_edges:
                    sections.append(f"  ... stopped at {max_edges} {d} edge(s); raise max_edges to unfold.")

        nav_query = f"types:{anchor_name or resolved.path}:L{resolved.line}"
        _record_semantic_nav_context(nav_query, groups)
        return "\n".join(sections)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


# --- Tool registry ---

_ALL_TOOLS: dict[str, tuple[Any, str]] = {
    "diagnostics": (lsp_diagnostics, "textDocument/diagnostic"),
    "grep": (lsp_grep, "cc-lsp-now/grep"),
    "symbols_at": (lsp_symbols_at, "cc-lsp-now/symbols_at"),
    "symbol": (lsp_symbol, "cc-lsp-now/symbol"),
    "goto": (lsp_goto, "cc-lsp-now/goto"),
    "refs": (lsp_refs, "cc-lsp-now/refs"),
    "outline": (lsp_outline, "textDocument/documentSymbol"),
    "rename": (lsp_rename, "textDocument/rename"),
    "move": (lsp_move, "workspace/willRenameFiles"),
    "fix": (lsp_fix, "cc-lsp-now/fix"),
    "calls": (lsp_calls, "cc-lsp-now/calls"),
    "types": (lsp_types, "cc-lsp-now/types"),
    "path": (lsp_path, "cc-lsp-now/path"),
    "confirm": (lsp_confirm, "cc-lsp-now/confirm"),
    "session": (lsp_session, "cc-lsp-now/session"),
    "log": (lsp_log, "cc-lsp-now/log"),
    "memory": (lsp_memory, "cc-lsp-now/memory"),
}


def _wrap_with_header(func: Any, method: str) -> Any:
    import functools

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        global _last_server
        _last_server = ""
        _added_workspaces_this_call.clear()
        _just_started_this_call.clear()
        drain_agent_messages()  # clear leftovers from prior calls

        result = await func(*args, **kwargs)
        header = _header(method) if _last_server else f"[{method}]"
        prefix_lines: list[str] = [header]
        for label in _just_started_this_call:
            prefix_lines.append(f"[+started] {label}")
        for p in _added_workspaces_this_call:
            prefix_lines.append(f"[+workspace] {p}")
        prefix_lines.extend(drain_agent_messages())
        prefix = "\n".join(prefix_lines)
        return f"{prefix}\n{result}"

    return wrapper


# Tool → LSP capability path (dotted for nested keys in the initialize response).
# None means the tool is always enabled (e.g. lsp_confirm is client-side).
TOOL_CAPABILITIES: dict[str, str | None] = {
    "diagnostics": "diagnosticProvider",
    "grep": "definitionProvider",
    "symbols_at": "definitionProvider",
    "symbol": "definitionProvider",
    "goto": "definitionProvider",
    "refs": "referencesProvider",
    "outline": "documentSymbolProvider",
    "rename": "renameProvider",
    "fix": "codeActionProvider",
    "calls": "callHierarchyProvider",
    "types": "typeHierarchyProvider",
    "path": "callHierarchyProvider",
    "move": "workspace.fileOperations.willRename",
    "confirm": None,
    "session": None,
    "log": None,
    "memory": None,
}


def _has_capability(caps: dict, path: str | None) -> bool:
    if path is None:
        return True
    cur: Any = caps
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return cur is not None and cur is not False


def _sync_probe_chain_caps() -> list[dict]:
    """Spawn each server briefly, read its advertised capabilities, then shut it down.

    This is intentionally opt-in. Running it at MCP module import starts the
    configured language server before the MCP initialize handshake, which makes
    heavy servers such as csharp-ls exceed the client's startup timeout. Runtime
    negative capability caching already handles unsupported methods; this probe
    is only a context-pruning optimization for users who explicitly request it.
    """
    import asyncio as _asyncio

    raw = os.environ.get(CAPABILITY_PROBE_ENV, "").strip().lower()
    if raw not in _CAPABILITY_PROBE_ENABLED:
        return []

    try:
        chain = _parse_chain()
    except RuntimeError:
        return []

    # Guard against being called inside an already-running loop (e.g. from a test
    # harness or an async app that imports this module). Skip probing — tools stay
    # enabled and the runtime negative cache handles unsupported methods as usual.
    try:
        _asyncio.get_running_loop()
        log.info("skipping capability probe: already inside an event loop")
        return []
    except RuntimeError:
        pass

    async def probe_one(cfg: ChainServer) -> dict:
        root = os.environ.get("LSP_ROOT", os.getcwd())
        client = LspClient([cfg.command, *cfg.args], root)
        try:
            await _asyncio.wait_for(client.start(), timeout=15.0)
            caps = dict(client.capabilities)
        finally:
            try:
                await _asyncio.wait_for(client.stop(), timeout=5.0)
            except Exception:
                pass
        return caps

    async def probe_all() -> list[dict]:
        results: list[dict] = []
        for cfg in chain:
            try:
                results.append(await probe_one(cfg))
            except Exception as e:
                log.warning("capability probe failed for %s: %s", cfg.name, e)
                results.append({})  # empty caps = this server contributes nothing to the union
        return results

    try:
        return _asyncio.run(probe_all())
    except Exception as e:
        log.warning("capability probe chain failed: %s", e)
        return []


def _union_supports(chain_caps: list[dict], tool_name: str) -> bool:
    if not chain_caps:
        return True  # no probe data → don't gate
    path = TOOL_CAPABILITIES.get(tool_name)
    if path is None:
        return True
    return any(_has_capability(c, path) for c in chain_caps)


_probed_caps = _sync_probe_chain_caps()

_tools_env = os.environ.get("LSP_TOOLS", "")
_disabled_env = os.environ.get("LSP_EXCLUDE", "") or os.environ.get("LSP_DISABLED_TOOLS", "")

if _tools_env == "all":
    _enabled = set(_ALL_TOOLS)
elif _tools_env:
    _enabled = {t.strip() for t in _tools_env.split(",")}
else:
    _enabled = set(_ALL_TOOLS) - DISABLED_BY_DEFAULT

if _disabled_env:
    _enabled -= {t.strip() for t in _disabled_env.split(",")}

# Capability gating: drop tools no server in the chain supports. Saves context tokens.
_unsupported = {n for n in _enabled if not _union_supports(_probed_caps, n)}
if _unsupported:
    log.info("capability-gated (no server supports): %s", sorted(_unsupported))
    _enabled -= _unsupported

for _name, (_func, _method) in _ALL_TOOLS.items():
    if _name in _enabled:
        mcp.tool()(_wrap_with_header(_func, _method))


def run() -> None:
    mcp.run(transport="stdio")
