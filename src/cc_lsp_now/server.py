from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from cc_lsp_now.agent_log import agent_log, drain_agent_messages
from cc_lsp_now.lsp import LspClient, LspError, file_uri
from cc_lsp_now.python_refactor import merge_workspace_edits, python_import_rewrite
from cc_lsp_now.candidate import Candidate
from cc_lsp_now.candidate_kind import CandidateKind
from cc_lsp_now.chain_server import ChainServer
from cc_lsp_now.file_move import FileMove
from cc_lsp_now.pending_buffer import PendingBuffer
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

COMPLETION_KIND_LABELS = {
    1: "Text", 2: "Method", 3: "Function", 4: "Constructor", 5: "Field",
    6: "Variable", 7: "Class", 8: "Interface", 9: "Module", 10: "Property",
    11: "Unit", 12: "Value", 13: "Enum", 14: "Keyword", 15: "Snippet",
    16: "Color", 17: "File", 18: "Reference", 19: "Folder", 20: "EnumMember",
    21: "Constant", 22: "Struct", 23: "Event", 24: "Operator", 25: "TypeParameter",
}

DISABLED_BY_DEFAULT = {"formatting"}


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

# --- Preview/confirm buffer --------------------------------------------------
#
# Several tools (rename, code_actions, move_file, ...) now emit previews instead of
# applying edits immediately. The preview populates a module-level buffer that
# the agent can then commit via `lsp_confirm(index)`.
#
# The buffer is single-slot — any new preview displaces the previous one.
# This matches the preview→confirm-or-replace flow the agent drives.
_pending: PendingBuffer | None = None

# Last semantic-grep graph, used by lsp_symbols_at("L78") to bounce from a
# compact samples field into the referenced line without repeating the path.
_last_semantic_nav: list["SemanticNavEntry"] = []
_last_semantic_nav_query: str = ""
_last_semantic_groups: list["SemanticGrepGroup"] = []


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


def _set_pending(kind: str, candidates: list[Candidate], description: str) -> None:
    """Stage a set of candidate WorkspaceEdits for later confirmation.

    Overwrites any previous pending state. The agent issues `lsp_confirm(index)`
    to pick one candidate out of ``candidates`` and apply it.
    """
    global _pending
    _pending = PendingBuffer(kind=kind, candidates=candidates, description=description)


def _clear_pending() -> None:
    global _pending
    _pending = None


def _apply_candidate(candidate: Candidate) -> tuple[int, int]:
    """Apply a single preview candidate's WorkspaceEdit.

    The candidate's ``edit`` dict holds the WorkspaceEdit. Special-cased:
    if candidate kind is ``FILE_MOVE`` with ``from_path`` / ``to_path``, the
    actual ``os.rename`` happens after edits are written — this keeps the
    import-rewrite + file-move atomic per the move_file flow.

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


def _completion_kind_label(n: int | None) -> str:
    if n is None:
        return ""
    return COMPLETION_KIND_LABELS.get(n, "")


def _normalize_locations(result: dict | list | None) -> list[str]:
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]
    return [_loc_str(loc) for loc in result]


def _format_symbol_tree(sym: dict, indent: int = 0) -> list[str]:
    kind = _symbol_kind_label(sym.get("kind", 0))
    name = sym.get("name", "")
    loc = sym.get("location", sym.get("range", {}))
    if "uri" in loc:
        line = loc.get("range", {}).get("start", {}).get("line", 0) + 1
    else:
        line = loc.get("start", {}).get("line", 0) + 1
    pad = "  " * indent
    lines = [f"{line}  {pad}{kind}  {name}"]
    for child in sym.get("children", []):
        lines.extend(_format_symbol_tree(child, indent + 1))
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
        f"[{index}] {group.kind} {group.name}{type_suffix} — {scope} — refs {ref_count} — def {def_label} — samples {samples}",
        240,
    )


def _record_semantic_nav_context(query: str, groups: list[SemanticGrepGroup]) -> None:
    """Remember the last semantic graph so a later bare ``L78`` has context."""
    global _last_semantic_nav_query
    _last_semantic_groups.clear()
    _last_semantic_groups.extend(groups)
    _last_semantic_nav.clear()
    _last_semantic_nav_query = query
    seen: set[tuple[str, int, int, int]] = set()
    for group_index, group in enumerate(groups):
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


async def _resolve_semantic_target(
    target: str = "",
    file_path: str = "",
    symbol: str = "",
    line: int = 0,
) -> SemanticTarget | str:
    target = target.strip()
    if target:
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


# --- Multi-symbol batching ---


async def _batch(
    file_path: str,
    symbol: str,
    symbols: str,
    line: int,
    fn: Callable[[str, dict], Awaitable[str]],
) -> str:
    """Batch-resolve multiple symbols and run an LSP callback on each.

    When ``symbols`` is non-empty (comma-separated), each symbol is resolved
    independently via ``_resolve`` and passed through ``fn``. Results are labeled
    per-symbol with ``--- {symbol} ---`` headers. Resolution or LSP errors
    for individual symbols are captured inline without aborting the batch.

    When ``symbols`` is empty, falls back to single-target mode: resolves
    ``(file_path, symbol, line)`` once and calls ``fn`` -- no label block.
    """
    if not symbols:
        # Single-target fallback -- no batching, no label header.
        try:
            uri, pos = await _resolve(file_path, symbol, line)
            return await fn(uri, pos)
        except AmbiguousSymbol as e:
            return _ambiguous_msg(e)
        except (LspError, ValueError) as e:
            return f"LSP error: {e}"

    parts: list[str] = []
    for sym in (s.strip() for s in symbols.split(",")):
        if not sym:
            continue
        header = f"--- {sym} ---"
        try:
            uri, pos = await _resolve(file_path, sym, line)
            body = await fn(uri, pos)
            parts.append(f"{header}\n{body}")
        except AmbiguousSymbol as e:
            parts.append(f"{header}\n{_ambiguous_msg(e)}")
        except (LspError, ValueError) as e:
            parts.append(f"{header}\nLSP error: {e}")
    return "\n\n".join(parts)


# --- Tool implementations ---


async def lsp_type_definition(file_path: str, symbol: str = "", symbols: str = "", line: int = 0) -> str:
    """Go to the type definition of a symbol. Pass symbol name or line number.
    Use symbols (comma-separated) to batch multiple lookups at once."""

    async def _do(uri: str, pos: dict) -> str:
        result = await _request("textDocument/typeDefinition", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No type definition found."
        return "\n".join(locs)

    return await _batch(file_path, symbol, symbols, line, _do)


async def lsp_completion(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Get completion suggestions. Pass symbol name or line number for position."""
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        result = await _request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not result:
            return "No completions."

        items = result if isinstance(result, list) else result.get("items", [])
        lines = []
        for item in items[:50]:
            label = item.get("label", "")
            kind = _completion_kind_label(item.get("kind"))
            detail = item.get("detail", "")
            parts = [label]
            if kind:
                parts.append(f"[{kind}]")
            if detail:
                parts.append(f"— {detail}")
            lines.append(" ".join(parts))
        return "\n".join(lines) if lines else "No completions."
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_signature_help(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Get function signature and parameter info. Pass symbol name or line number."""
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        result = await _request("textDocument/signatureHelp", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not result or not result.get("signatures"):
            return "No signature help available."

        signatures = result["signatures"]
        active_sig = result.get("activeSignature", 0)
        active_param = result.get("activeParameter", 0)

        output = []
        for i, sig in enumerate(signatures):
            marker = ">>> " if i == active_sig else "    "
            label = sig.get("label", "")
            output.append(f"{marker}{label}")
            doc = sig.get("documentation")
            if doc:
                doc_text = doc.get("value", doc) if isinstance(doc, dict) else doc
                output.append(f"    {doc_text}")
            params = sig.get("parameters", [])
            if params and i == active_sig:
                for j, p in enumerate(params):
                    active = " *" if j == active_param else ""
                    p_label = p.get("label", "")
                    p_doc = p.get("documentation", "")
                    if isinstance(p_doc, dict):
                        p_doc = p_doc.get("value", "")
                    output.append(f"      param: {p_label}{active}  {p_doc}")
        return "\n".join(output)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def _document_symbols_single(file_path: str) -> str:
    """Get symbols for a single file. Returns formatted tree or 'No symbols found.'."""
    file_path = _resolve_file_path(file_path)
    uri = file_uri(file_path)
    result = await _request("textDocument/documentSymbol", {
        "textDocument": {"uri": uri},
    }, uri=uri)
    if not result:
        return "No symbols found."
    lines: list[str] = []
    for sym in result:
        lines.extend(_format_symbol_tree(sym))
    return "\n".join(lines)


async def lsp_document_symbols(file_path: str = "", pattern: str = "") -> str:
    """Get all symbols in one or more documents (outline).

    Supports comma-separated file_path or glob pattern for multi-file symbols.
    """
    paths = _resolve_paths(file_path, pattern)
    if isinstance(paths, str):
        return paths
    try:
        if len(paths) == 1:
            return await _document_symbols_single(paths[0])
        sections: list[str] = []
        for p in paths:
            body = await _document_symbols_single(p)
            sections.append(f"=== {p} ===\n{body}")
        return "\n\n".join(sections)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_formatting(file_path: str, tab_size: int = 4, insert_spaces: bool = True) -> str:
    """Format an entire document."""
    try:
        file_path = _resolve_file_path(file_path)
        uri = file_uri(file_path)
        result = await _request("textDocument/formatting", {
            "textDocument": {"uri": uri},
            "options": {
                "tabSize": tab_size,
                "insertSpaces": insert_spaces,
            },
        }, uri=uri)
        if not result:
            return "No formatting changes needed."
        return json.dumps([{
            "range": _range_str(e.get("range", {})),
            "newText": e.get("newText", ""),
        } for e in result], indent=2)
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
    """Heuristic: if move_file returned 0 edits, scan for files that mention any
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
        return None

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
        workspace_folders: set[str] = set()
        for client in _chain_clients:
            if client is not None:
                workspace_folders.update(client.workspace_folders)
        if not workspace_folders:
            workspace_folders.add(os.environ.get("LSP_ROOT", os.getcwd()))

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
            lines.append("Options: (1) pre-warm importer files via lsp_symbol, (2) lsp_add_workspace on the project, (3) fall back to regex rewrite if LSP is unreliable here.")

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


async def lsp_move_file(from_path: str = "", to_path: str = "", symbol: str = "") -> str:
    """Move/rename a file and preview the import-updating edits.

    Pass either ``from_path`` directly, or ``symbol=<name>`` to have the
    bridge resolve the source file via workspace/symbol (useful when you
    know the class/function but not the file).

    Always previews — the resulting WorkspaceEdit + file-move metadata is
    staged under ``_pending``. Call ``lsp_confirm(0)`` to commit both the
    edits and the ``os.rename`` atomically.

    For bulk reorgs, prefer ``lsp_move_files``.
    """
    try:
        if symbol and not from_path:
            resolved = await _resolve_symbol_to_file(symbol)
            if not resolved:
                return f"Could not resolve symbol {symbol!r} to a file via workspace/symbol."
            from_path = resolved
        if not from_path:
            return "Provide from_path or symbol."
        if not to_path:
            return "to_path is required."
        from_path = _resolve_file_path(from_path)
        return await _do_move([(from_path, to_path)])
    except (LspError, ValueError, OSError) as e:
        return f"LSP error: {e}"


async def lsp_move_files(from_paths: str, to_paths: str) -> str:
    """Batch-move multiple files in one willRenameFiles call.

    Pass comma-separated lists; the i-th from-path moves to the i-th to-path.
    Single preview covers all renames; single lsp_confirm(0) commits them
    atomically. Much faster than N individual move_file calls for reorgs.
    """
    try:
        froms = [p.strip() for p in from_paths.split(",") if p.strip()]
        tos = [p.strip() for p in to_paths.split(",") if p.strip()]
        if len(froms) != len(tos):
            return f"Mismatch: {len(froms)} from-paths vs {len(tos)} to-paths"
        if not froms:
            return "No files specified."
        froms = [_resolve_file_path(path) for path in froms]
        return await _do_move(list(zip(froms, tos)))
    except (LspError, ValueError, OSError) as e:
        return f"LSP error: {e}"


async def lsp_prepare_rename(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Check if a symbol can be renamed. Pass symbol name or line number."""
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        try:
            result = await _request("textDocument/prepareRename", {
                "textDocument": {"uri": uri},
                "position": pos,
            }, uri=uri)
        except (LspError, asyncio.TimeoutError, ConnectionError) as e:
            trace = await _rename_trace(
                file_path=file_path,
                uri=uri,
                pos=pos,
                new_name="",
                operation="prepareRename",
                error=e,
                include_prepare=False,
            )
            return f"Cannot rename at this position.\n\n{trace}"
        if not result:
            trace = await _rename_trace(
                file_path=file_path,
                uri=uri,
                pos=pos,
                new_name="",
                operation="prepareRename",
                rename_result=result,
                include_prepare=False,
            )
            return f"Cannot rename at this position.\n\n{trace}"

        if "range" in result and "placeholder" in result:
            return f"{_range_str(result['range'])} — current name: {result['placeholder']!r}"
        if "start" in result:
            return f"Renameable at {_range_str(result)}"
        return json.dumps(result, indent=2)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_code_actions(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Get available code actions (quick fixes, refactorings). Pass symbol name or line number.

    Also stages all returned actions into the module-level ``_pending`` buffer
    so the agent can pick one by index via ``lsp_confirm(N)``. Each line in the
    output is prefixed with ``[N]`` — that N is the index to pass to confirm.
    """
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        primary = await _get_client(0)
        stored = primary.diagnostics.get(uri, [])
        target_line = pos.get("line", 0)
        range_diagnostics = [
            d for d in stored
            if d.get("range", {}).get("start", {}).get("line", -1) == target_line
        ]

        result = await _request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": pos, "end": pos},
            "context": {"diagnostics": range_diagnostics},
        }, uri=uri)
        if not result:
            _clear_pending()
            return "No code actions available."

        lines = []
        action_candidates: list[Candidate] = []
        for action in result:
            title = action.get("title", "")
            kind = action.get("kind", "")
            edit = action.get("edit")
            if edit:
                idx = len(action_candidates)
                parts = [f"[{idx}] {title}"]
            else:
                parts = [f"[-] {title}"]
            if kind:
                parts.append(f"[{kind}]")
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
            lines.append(" ".join(parts))

        if action_candidates:
            _set_pending(
                "code_action",
                action_candidates,
                f"{len(action_candidates)} code action(s) at {_uri_to_path(uri)}:{target_line + 1}",
            )
            lines.append("")
            lines.append(f"Staged {len(action_candidates)} edit action(s). Call lsp_confirm(N) to apply.")
        else:
            _clear_pending()
            lines.append("")
            lines.append("No edit-backed actions to stage.")
        return "\n".join(lines)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_info() -> str:
    """Report the running cc-lsp-now build and the probed capabilities of each chain server.

    Useful when tool behavior is confusing — compare the displayed git SHA
    against cc-lsp-now's current HEAD to confirm the MCP process isn't stale
    from a prior Claude Code session. Stale MCPs are the #1 reason new features
    appear to not work after a plugin update: Claude Code reuses the subprocess
    across /reload-plugins; only a full Claude Code restart spawns a fresh one.
    """
    import importlib.metadata as _imd
    import subprocess as _subp

    module_file = Path(__file__).resolve()
    info_lines: list[str] = []

    # Try to detect install path + git commit
    try:
        pkg_root = module_file.parent.parent.parent  # .../src/cc_lsp_now/server.py → .../
        git_dir = pkg_root / ".git"
        if git_dir.exists():
            sha = _subp.run(
                ["git", "-C", str(pkg_root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            info_lines.append(f"cc-lsp-now: {pkg_root} @ {sha or 'unknown'}")
        else:
            info_lines.append(f"cc-lsp-now install: {pkg_root} (no .git — probably installed package)")
    except Exception as e:
        info_lines.append(f"cc-lsp-now introspection failed: {e}")

    try:
        version = _imd.version("cc-lsp-now")
        info_lines.append(f"version: {version}")
    except Exception:
        pass

    _ensure_chain_configs()
    info_lines.append("")
    info_lines.append("Chain:")
    for cfg in _chain_configs:
        info_lines.append(f"  {cfg.label}: {cfg.command} {' '.join(cfg.args)}")

    if _probed_caps:
        info_lines.append("")
        info_lines.append("Probed capabilities (at module load):")
        for cfg, caps in zip(_chain_configs, _probed_caps):
            if not caps:
                info_lines.append(f"  [{cfg.label}] (probe failed or no caps reported)")
                continue
            key_caps = [k for k in caps.keys() if k.endswith("Provider") or k == "workspace"]
            info_lines.append(f"  [{cfg.label}] {len(caps)} caps; providers: {', '.join(sorted(key_caps))}")
            ws_caps = caps.get("workspace", {})
            file_ops = ws_caps.get("fileOperations", {}) if isinstance(ws_caps, dict) else {}
            if file_ops:
                info_lines.append(f"    fileOperations: {', '.join(sorted(file_ops.keys()))}")
    else:
        info_lines.append("")
        info_lines.append("Capabilities probe was skipped (empty chain caps).")

    return "\n".join(info_lines)


async def lsp_workspaces() -> str:
    """List workspace folders registered with each LSP, plus warmup stats.

    Proactively spawns every server in the chain (reporting dead state isn't
    useful). Each folder line shows files warmed and seconds since warmup —
    a folder with no warmup count means didOpen hasn't been bulk-fired there,
    so operations touching files in that folder may hit an unindexed LSP.
    """
    _ensure_chain_configs()
    for idx in range(len(_chain_configs)):
        await _get_client(idx)

    now = time.time()
    lines: list[str] = []
    for idx, cfg in enumerate(_chain_configs):
        client = _chain_clients[idx]
        assert client is not None
        lines.append(f"[{cfg.label}]")
        for folder in sorted(client.workspace_folders):
            stats = _folder_warmup_stats.get((idx, folder))
            if stats:
                age = int(now - stats.timestamp)
                lines.append(f"  {folder}  (warmed {stats.count} files, {age}s ago)")
            else:
                lines.append(f"  {folder}  (not warmed)")
    return "\n".join(lines) if lines else "No chain configured."


async def lsp_add_workspace(path: str) -> str:
    """Explicitly add a workspace folder. Applies to every LSP in the chain.

    Proactively spawns every chain server (if any aren't already) and then
    registers the folder + runs warmup on each. Use this when auto-detection
    via LSP_PROJECT_MARKERS doesn't find the root (unusual layout, no marker
    files) or you want to pre-index before a batch refactor.
    """
    _ensure_chain_configs()
    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        return f"Not a directory: {abs_path}"

    # Spawn every chain client — "queued (applied on spawn)" is a terrible UX
    # when the operation the caller invoked is specifically about workspaces.
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


async def lsp_confirm(index: int = 0) -> str:
    """Apply one staged candidate from the preview buffer.

    Companion to tools that stage previews (currently ``lsp_code_actions``
    ``lsp_rename``, and ``lsp_move_file``). Index into the ``candidates`` list
    shown by the most recent preview. Clears ``_pending`` on success so the buffer is
    single-shot — a stale preview can't be re-committed after context drifts.
    """
    global _pending
    if _pending is None:
        return "Nothing to confirm."

    candidates = _pending.candidates
    kind = _pending.kind

    if index < 0 or index >= len(candidates):
        return f"Invalid index {index}, only {len(candidates)} candidates available."

    candidate = candidates[index]
    try:
        file_count, edit_count = _apply_candidate(candidate)
    except (OSError, ValueError, KeyError) as e:
        return f"Apply failed: {e}"

    _pending = None
    return f"Applied [{kind} #{index}]: {candidate.title}. {file_count} file(s), {edit_count} edit(s)."


async def lsp_call_hierarchy_incoming(file_path: str, symbol: str = "", symbols: str = "", line: int = 0) -> str:
    """Find all callers of a function/method. Pass symbol name or line number.
    Use symbols (comma-separated) to batch multiple lookups at once."""

    async def _do(uri: str, pos: dict) -> str:
        items = await _request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not items:
            return "No call hierarchy item found at this position."

        result = await _request("callHierarchy/incomingCalls", {"item": items[0]})
        if not result:
            return "No incoming calls found."

        lines = []
        for call in result:
            from_item = call.get("from", {})
            name = from_item.get("name", "")
            kind = _symbol_kind_label(from_item.get("kind", 0))
            path = _uri_to_path(from_item.get("uri", ""))
            start = from_item.get("range", {}).get("start", {})
            line_n = start.get("line", 0) + 1
            n_sites = len(call.get("fromRanges", []))
            lines.append(f"{line_n}  {kind}  {name}  {path}  ({n_sites} call site{'s' if n_sites != 1 else ''})")
        return "\n".join(lines)

    return await _batch(file_path, symbol, symbols, line, _do)


async def lsp_call_hierarchy_outgoing(file_path: str, symbol: str = "", symbols: str = "", line: int = 0) -> str:
    """Find all functions/methods called by a function/method. Pass symbol name or line number.
    Use symbols (comma-separated) to batch multiple lookups at once."""

    async def _do(uri: str, pos: dict) -> str:
        items = await _request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not items:
            return "No call hierarchy item found at this position."

        result = await _request("callHierarchy/outgoingCalls", {"item": items[0]})
        if not result:
            return "No outgoing calls found."

        lines = []
        for call in result:
            to_item = call.get("to", {})
            name = to_item.get("name", "")
            kind = _symbol_kind_label(to_item.get("kind", 0))
            path = _uri_to_path(to_item.get("uri", ""))
            start = to_item.get("range", {}).get("start", {})
            line_n = start.get("line", 0) + 1
            n_sites = len(call.get("fromRanges", []))
            lines.append(f"{line_n}  {kind}  {name}  {path}  ({n_sites} call site{'s' if n_sites != 1 else ''})")
        return "\n".join(lines)

    return await _batch(file_path, symbol, symbols, line, _do)


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
        primary = await _get_client(0)
        diagnostics = primary.diagnostics.get(uri, [])
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
            _record_semantic_nav_context(group.name, [group])
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
    resolved = await _resolve_semantic_target(target, file_path, symbol, line)
    if isinstance(resolved, str):
        return resolved
    max_refs = max(1, min(max_refs, 500))
    try:
        result = await _request("textDocument/references", {
            "textDocument": {"uri": resolved.uri},
            "position": resolved.pos,
            "context": {"includeDeclaration": include_declaration},
        }, uri=resolved.uri)
        locs = _locations_from_lsp(result)
        if not locs:
            return "No references found."

        group = await _semantic_group_for_target(resolved)
        if group is not None:
            group.reference_locs = locs
            _record_semantic_nav_context(group.name, [group])
            label = f"{group.kind} {group.name}"
        else:
            label = resolved.name or "symbol"

        lines = [f"References for {label}: {len(locs)}"]
        for loc in locs[:max_refs]:
            lines.append(f"  {_format_location_with_context(loc)}")
        if len(locs) > max_refs:
            lines.append(f"... {len(locs) - max_refs} more; raise max_refs to unfold.")
        return "\n".join(lines)
    except (LspError, ValueError, RuntimeError) as e:
        return f"LSP error: {e}"


async def lsp_hover(file_path: str, symbol: str = "", symbols: str = "", line: int = 0) -> str:
    """Get type info and docs for a symbol. Pass symbol name or line number.
    Use symbols (comma-separated) to batch multiple lookups at once."""

    async def _do(uri: str, pos: dict) -> str:
        result = await _request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not result:
            return "No hover information available."
        contents = result.get("contents", "")
        if isinstance(contents, dict):
            return contents.get("value", str(contents))
        if isinstance(contents, list):
            return "\n\n".join(
                c.get("value", str(c)) if isinstance(c, dict) else str(c)
                for c in contents
            )
        return str(contents)

    return await _batch(file_path, symbol, symbols, line, _do)


async def lsp_definition(file_path: str, symbol: str = "", symbols: str = "", line: int = 0) -> str:
    """Go to the definition of a symbol. Pass symbol name or line number.
    Use symbols (comma-separated) to batch multiple lookups at once."""

    async def _do(uri: str, pos: dict) -> str:
        result = await _request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No definition found."
        return "\n".join(locs)

    return await _batch(file_path, symbol, symbols, line, _do)


async def lsp_references(file_path: str, symbol: str = "", symbols: str = "", line: int = 0, include_declaration: bool = True) -> str:
    """Find all references to a symbol. Pass symbol name or line number.
    Use symbols (comma-separated) to batch multiple lookups at once."""

    async def _do(uri: str, pos: dict) -> str:
        result = await _request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": pos,
            "context": {"includeDeclaration": include_declaration},
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No references found."
        return "\n".join(locs)

    return await _batch(file_path, symbol, symbols, line, _do)


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
        client = await _get_client(0)
        await client.resync_open_documents()
        roots = sorted(client.workspace_folders) or [os.environ.get("LSP_ROOT", os.getcwd())]
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
        _record_semantic_nav_context(query, ordered)
        lines = [
            _format_semantic_grep_group(i, group)
            for i, group in enumerate(ordered[:max_groups])
        ]
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
    _record_semantic_nav_context(f"{Path(path).name}:L{target_line}", ordered)

    lines = [f"Target: {path}:L{target_line}"]
    try:
        line_text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[target_line - 1]
        lines.append(f"  {line_text.strip()}")
    except (OSError, IndexError):
        pass
    lines.extend(_format_semantic_grep_group(i, group) for i, group in enumerate(ordered))
    return "\n".join(lines)


async def lsp_implementation(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Go to the implementation of a symbol (interfaces, abstract methods).

    Unlike lsp_definition, which jumps to where a symbol is declared, this jumps
    to concrete implementations — e.g. the classes implementing an interface, or
    subclass overrides of an abstract method. Pass symbol name or line number.
    """
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        result = await _request("textDocument/implementation", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No implementations found."
        return "\n".join(locs)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_declaration(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Go to the declaration of a symbol. Pass symbol name or line number.

    Distinct from lsp_definition: some languages (C/C++) separate declaration
    (header) from definition (impl). For languages without that split, this
    typically mirrors lsp_definition.
    """
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        result = await _request("textDocument/declaration", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No declaration found."
        return "\n".join(locs)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


def _format_type_hierarchy_item(item: dict) -> str:
    """Format a TypeHierarchyItem as a compact source location."""
    name = item.get("name", "")
    kind = _symbol_kind_label(item.get("kind", 0))
    path = _uri_to_path(item.get("uri", ""))
    start = item.get("range", {}).get("start", {})
    line_n = start.get("line", 0) + 1
    return f"{line_n}  {kind}  {name}  {path}"


async def lsp_type_hierarchy_supertypes(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Find the supertypes (parents) of a type. Pass symbol name or line number.

    Two-step LSP flow: textDocument/prepareTypeHierarchy then typeHierarchy/supertypes
    on the first resolved item. Useful for climbing a class/interface chain.
    """
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        items = await _request("textDocument/prepareTypeHierarchy", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not items:
            return "No type hierarchy item at this position."

        result = await _request("typeHierarchy/supertypes", {"item": items[0]})
        if not result:
            return "No supertypes found."

        return "\n".join(_format_type_hierarchy_item(item) for item in result)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_type_hierarchy_subtypes(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Find the subtypes (children) of a type. Pass symbol name or line number.

    Two-step LSP flow: textDocument/prepareTypeHierarchy then typeHierarchy/subtypes
    on the first resolved item. Useful for discovering implementors/derivatives.
    """
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        items = await _request("textDocument/prepareTypeHierarchy", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not items:
            return "No type hierarchy item at this position."

        result = await _request("typeHierarchy/subtypes", {"item": items[0]})
        if not result:
            return "No subtypes found."

        return "\n".join(_format_type_hierarchy_item(item) for item in result)
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_inlay_hint(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Get inlay hints (inferred types, parameter names) for a region.

    Without symbol/line → whole-file hints (range [0..99999]). With a symbol or
    line → a 50-line window starting at the resolved position — enough context
    to surface type annotations and param labels around the target without
    flooding output.

    Output: `{line}:{col}  {label}  [{kind}]` per hint, where kind is
    Type (1) or Parameter (2).
    """
    try:
        file_path = _resolve_file_path(file_path)
        uri = file_uri(file_path)
        # Whole-file path: no symbol/line given — skip _resolve and scan everything.
        if not symbol and line == 0:
            hint_range = {
                "start": {"line": 0, "character": 0},
                "end": {"line": 99999, "character": 0},
            }
        else:
            uri, pos = await _resolve(file_path, symbol, line)
            start_line = pos.get("line", 0)
            hint_range = {
                "start": {"line": start_line, "character": 0},
                "end": {"line": start_line + 50, "character": 0},
            }

        result = await _request("textDocument/inlayHint", {
            "textDocument": {"uri": uri},
            "range": hint_range,
        }, uri=uri)
        if not result:
            return "No inlay hints."

        kind_labels = {1: "Type", 2: "Parameter"}
        lines: list[str] = []
        for hint in result:
            pos_field = hint.get("position", {})
            hline = pos_field.get("line", 0) + 1
            hcol = pos_field.get("character", 0) + 1
            label = hint.get("label", "")
            # Spec allows label as string or list of InlayHintLabelPart.
            if isinstance(label, list):
                label = "".join(
                    part.get("value", "") if isinstance(part, dict) else str(part)
                    for part in label
                )
            kind = kind_labels.get(hint.get("kind"), "")
            kind_str = f"  [{kind}]" if kind else ""
            lines.append(f"{hline}:{hcol}  {label}{kind_str}")
        return "\n".join(lines) if lines else "No inlay hints."
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def _folding_range_single(file_path: str) -> str:
    """Folding regions for a single file. Each region reports its 1-based line
    span and its classifying kind (``comment`` / ``imports`` / ``region``).
    """
    file_path = _resolve_file_path(file_path)
    uri = file_uri(file_path)
    result = await _request("textDocument/foldingRange", {
        "textDocument": {"uri": uri},
    }, uri=uri)
    if not result:
        return "No folding ranges."
    lines: list[str] = []
    for region in result:
        start_line = region.get("startLine", 0) + 1
        end_line = region.get("endLine", 0) + 1
        kind = region.get("kind") or "region"
        lines.append(f"L{start_line}-L{end_line}  {kind}")
    return "\n".join(lines) if lines else "No folding ranges."


async def lsp_folding_range(file_path: str = "", pattern: str = "") -> str:
    """Get folding regions (imports, comments, blocks) for one or more files.

    Supports comma-separated file_path or glob pattern for multi-file requests.
    Mirrors the batching shape of ``lsp_diagnostics`` / ``lsp_document_symbols``.
    """
    paths = _resolve_paths(file_path, pattern)
    if isinstance(paths, str):
        return paths
    try:
        if len(paths) == 1:
            return await _folding_range_single(paths[0])
        sections: list[str] = []
        for p in paths:
            body = await _folding_range_single(p)
            sections.append(f"=== {p} ===\n{body}")
        return "\n\n".join(sections)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_range_formatting(
    file_path: str,
    start_line: int,
    end_line: int,
    tab_size: int = 4,
    insert_spaces: bool = True,
) -> str:
    """Format a specific line range within a document.

    Line numbers are 1-based (user-facing convention). The end position uses
    character=99999 to reliably span to end-of-line without needing to measure
    — the server clamps it to the actual line length.
    """
    try:
        file_path = _resolve_file_path(file_path)
        uri = file_uri(file_path)
        result = await _request("textDocument/rangeFormatting", {
            "textDocument": {"uri": uri},
            "range": {
                "start": {"line": start_line - 1, "character": 0},
                "end": {"line": end_line - 1, "character": 99999},
            },
            "options": {
                "tabSize": tab_size,
                "insertSpaces": insert_spaces,
            },
        }, uri=uri)
        if not result:
            return "No formatting changes needed."
        return json.dumps([{
            "range": _range_str(e.get("range", {})),
            "newText": e.get("newText", ""),
        } for e in result], indent=2)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_code_lens(file_path: str) -> str:
    """List code lenses (inline actionable hints: run/debug/references/etc) for a file."""
    try:
        file_path = _resolve_file_path(file_path)
        uri = file_uri(file_path)
        result = await _request("textDocument/codeLens", {
            "textDocument": {"uri": uri},
        }, uri=uri)
        if not result:
            return "No code lenses."

        lines: list[str] = []
        for lens in result:
            start = lens.get("range", {}).get("start", {})
            line_n = start.get("line", 0) + 1
            cmd = lens.get("command") or {}
            title = cmd.get("title", "")
            command = cmd.get("command", "")
            lines.append(f"L{line_n}  {title} [{command}]")
        return "\n".join(lines) if lines else "No code lenses."
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_create_file(file_path: str) -> str:
    """Preview side effects of creating a file (new imports, __init__ entries, ...).

    Sends ``workspace/willCreateFiles`` to surface sibling edits the server
    would like to perform when this file is introduced. Stages a ``file_create``
    candidate under ``_pending`` — the actual empty-file write happens in
    ``_apply_candidate`` after ``lsp_confirm()`` commits the edits.
    """
    try:
        file_path = _resolve_file_path(file_path, must_exist=False)
        uri = file_uri(file_path)
        result = await _request(
            "workspace/willCreateFiles",
            {"files": [{"uri": uri}]},
        )
        if not result:
            result = {}

        edit_count = 0
        for _u, edits in result.get("changes", {}).items():
            edit_count += len(edits)
        for doc_change in result.get("documentChanges", []):
            if "textDocument" in doc_change:
                edit_count += len(doc_change.get("edits", []))

        description = f"Create {file_path}"
        candidate = Candidate(
            kind=CandidateKind.FILE_CREATE,
            title=description,
            edit=result or {},
            from_path=file_path,
        )
        _set_pending("create_file", [candidate], description)

        return (
            f"Preview: create {file_path} with {edit_count} side-effect edit(s). "
            f"Re-call lsp_confirm() to commit."
        )
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


async def lsp_delete_file(file_path: str) -> str:
    """Preview cleanup edits for deleting a file (remove imports, registrations, ...).

    Sends ``workspace/willDeleteFiles``. Stages a ``file_delete`` candidate;
    ``_apply_candidate`` performs ``unlink(missing_ok=True)`` after cleanup
    edits land so re-confirming is idempotent.
    """
    try:
        file_path = _resolve_file_path(file_path)
        uri = file_uri(file_path)
        result = await _request(
            "workspace/willDeleteFiles",
            {"files": [{"uri": uri}]},
        )
        if not result:
            result = {}

        edit_count = 0
        for _u, edits in result.get("changes", {}).items():
            edit_count += len(edits)
        for doc_change in result.get("documentChanges", []):
            if "textDocument" in doc_change:
                edit_count += len(doc_change.get("edits", []))

        description = f"Delete {file_path}"
        candidate = Candidate(
            kind=CandidateKind.FILE_DELETE,
            title=description,
            edit=result or {},
            from_path=file_path,
        )
        _set_pending("delete_file", [candidate], description)

        return (
            f"Preview: delete {file_path} with {edit_count} cleanup edit(s). "
            f"Re-call lsp_confirm() to commit."
        )
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


# --- Tool registry ---

_ALL_TOOLS: dict[str, tuple[Any, str]] = {
    "diagnostics": (lsp_diagnostics, "textDocument/diagnostic"),
    "grep": (lsp_grep, "cc-lsp-now/grep"),
    "symbols_at": (lsp_symbols_at, "cc-lsp-now/symbols_at"),
    "symbol": (lsp_symbol, "cc-lsp-now/symbol"),
    "goto": (lsp_goto, "cc-lsp-now/goto"),
    "refs": (lsp_refs, "cc-lsp-now/refs"),
    "completion": (lsp_completion, "textDocument/completion"),
    "document_symbols": (lsp_document_symbols, "textDocument/documentSymbol"),
    "formatting": (lsp_formatting, "textDocument/formatting"),
    "rename": (lsp_rename, "textDocument/rename"),
    "prepare_rename": (lsp_prepare_rename, "textDocument/prepareRename"),
    "move_file": (lsp_move_file, "workspace/willRenameFiles"),
    "code_actions": (lsp_code_actions, "textDocument/codeAction"),
    "call_hierarchy_incoming": (lsp_call_hierarchy_incoming, "callHierarchy/incomingCalls"),
    "call_hierarchy_outgoing": (lsp_call_hierarchy_outgoing, "callHierarchy/outgoingCalls"),
    "type_hierarchy_supertypes": (lsp_type_hierarchy_supertypes, "typeHierarchy/supertypes"),
    "type_hierarchy_subtypes": (lsp_type_hierarchy_subtypes, "typeHierarchy/subtypes"),
    "inlay_hint": (lsp_inlay_hint, "textDocument/inlayHint"),
    "folding_range": (lsp_folding_range, "textDocument/foldingRange"),
    "range_formatting": (lsp_range_formatting, "textDocument/rangeFormatting"),
    "code_lens": (lsp_code_lens, "textDocument/codeLens"),
    "create_file": (lsp_create_file, "workspace/willCreateFiles"),
    "delete_file": (lsp_delete_file, "workspace/willDeleteFiles"),
    "confirm": (lsp_confirm, "cc-lsp-now/confirm"),
    "info": (lsp_info, "cc-lsp-now/info"),
    "workspaces": (lsp_workspaces, "cc-lsp-now/workspaces"),
    "add_workspace": (lsp_add_workspace, "cc-lsp-now/add_workspace"),
    "move_files": (lsp_move_files, "workspace/willRenameFiles"),
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
    "completion": "completionProvider",
    "document_symbols": "documentSymbolProvider",
    "formatting": "documentFormattingProvider",
    "rename": "renameProvider",
    "prepare_rename": "renameProvider",
    "code_actions": "codeActionProvider",
    "call_hierarchy_incoming": "callHierarchyProvider",
    "call_hierarchy_outgoing": "callHierarchyProvider",
    "move_file": "workspace.fileOperations.willRename",
    "type_hierarchy_supertypes": "typeHierarchyProvider",
    "type_hierarchy_subtypes": "typeHierarchyProvider",
    "inlay_hint": "inlayHintProvider",
    "folding_range": "foldingRangeProvider",
    "range_formatting": "documentRangeFormattingProvider",
    "code_lens": "codeLensProvider",
    "create_file": "workspace.fileOperations.willCreate",
    "delete_file": "workspace.fileOperations.willDelete",
    "confirm": None,
    "info": None,
    "workspaces": None,
    "add_workspace": None,
    "move_files": "workspace.fileOperations.willRename",
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

    Runs at module load. Failure is non-fatal — a server that refuses to probe
    is treated as supporting everything (no gating applied against it).
    """
    import asyncio as _asyncio

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
