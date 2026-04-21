from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from cc_lsp_now.lsp import LspClient, LspError, file_uri
from cc_lsp_now.python_refactor import merge_workspace_edits, python_import_rewrite

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

_chain_configs: list[dict] = []  # [{command, args, name}, ...] parsed from env at first use
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
# Warmup metadata for status reporting: (chain_idx, folder) -> {count, timestamp}
_folder_warmup_stats: dict[tuple[int, str], dict] = {}

# --- Preview/confirm buffer --------------------------------------------------
#
# Several tools (code_actions, move_file, ...) now emit previews instead of
# applying edits immediately. The preview populates a module-level buffer that
# the agent can then commit via `lsp_confirm(index)`.
#
# Shape:
#   {
#     "kind": str,           # e.g. "code_action", "file_move"
#     "candidates": list[dict],  # each is a WorkspaceEdit or { edit: WorkspaceEdit, ... }
#     "description": str,    # short human-readable preview summary
#   }
#
# The buffer is single-slot — any new preview displaces the previous one.
# This matches the preview→confirm-or-replace flow the agent drives.
_pending: dict | None = None


def _set_pending(kind: str, candidates: list[dict], description: str) -> None:
    """Stage a set of candidate WorkspaceEdits for later confirmation.

    Overwrites any previous pending state. The agent issues `lsp_confirm(index)`
    to pick one candidate out of ``candidates`` and apply it.
    """
    global _pending
    _pending = {"kind": kind, "candidates": candidates, "description": description}


def _apply_candidate(candidate: dict) -> tuple[int, int]:
    """Apply a single preview candidate's WorkspaceEdit.

    A candidate may either BE a WorkspaceEdit directly (has ``changes`` or
    ``documentChanges``) or wrap one under an ``"edit"`` field (LSP CodeAction
    shape). Special-cased: if candidate has ``"kind": "file_move"`` with
    ``from_path`` / ``to_path``, the actual ``os.rename`` happens after edits
    are written — this keeps the import-rewrite + file-move atomic per the
    move_file flow.

    Returns (file_count, edit_count) for the summary line.
    """
    # Unwrap: CodeAction-shaped candidates carry the WorkspaceEdit under "edit"
    if "edit" in candidate and isinstance(candidate["edit"], dict):
        edit = candidate["edit"]
    else:
        edit = candidate

    affected: list[str] = []
    if edit.get("changes") or edit.get("documentChanges"):
        affected = _apply_workspace_edit(edit)

    edit_count = 0
    for _uri, edits in edit.get("changes", {}).items():
        edit_count += len(edits)
    for doc_change in edit.get("documentChanges", []):
        if "textDocument" in doc_change:
            edit_count += len(doc_change.get("edits", []))

    # file_move finishes with the rename itself — after any import edits landed.
    if candidate.get("kind") == "file_move":
        from_path = candidate.get("from_path")
        to_path = candidate.get("to_path")
        if from_path and to_path:
            to_dir = os.path.dirname(os.path.abspath(to_path))
            if to_dir:
                os.makedirs(to_dir, exist_ok=True)
            os.rename(from_path, to_path)

    # file_move_batch: replay the list of renames after the single WorkspaceEdit
    # covers all import fixups. Order doesn't matter since edits are in other
    # files, and the destinations are unique per call.
    if candidate.get("kind") == "file_move_batch":
        for move in candidate.get("moves", []):
            fp, tp = move.get("from_path"), move.get("to_path")
            if fp and tp:
                to_dir = os.path.dirname(os.path.abspath(tp))
                if to_dir:
                    os.makedirs(to_dir, exist_ok=True)
                try:
                    os.rename(fp, tp)
                except OSError as e:
                    log.warning("file_move_batch rename failed %s → %s: %s", fp, tp, e)

    # file_create: after any side-effect edits (new imports, __init__ entries)
    # land in sibling modules, materialize the empty file itself. Wrapped in
    # try/except so a filesystem-level failure doesn't crash the confirm path —
    # the edits already wrote successfully and agent can recover manually.
    if candidate.get("kind") == "file_create":
        create_path = candidate.get("create_path")
        if create_path:
            try:
                target = Path(create_path)
                parent = target.parent
                if str(parent):
                    parent.mkdir(parents=True, exist_ok=True)
                target.touch(exist_ok=True)
            except OSError as e:
                log.warning("file_create touch failed for %s: %s", create_path, e)

    # file_delete: cleanup edits have fixed up imports/registrations in siblings;
    # now unlink the file itself. missing_ok so re-confirm is idempotent.
    if candidate.get("kind") == "file_delete":
        delete_path = candidate.get("delete_path")
        if delete_path:
            try:
                Path(delete_path).unlink(missing_ok=True)
            except OSError as e:
                log.warning("file_delete unlink failed for %s: %s", delete_path, e)

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


def _parse_chain() -> list[dict]:
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
        chain: list[dict] = []
        for i, entry in enumerate(s.strip() for s in servers_env.split(";")):
            if not entry:
                continue
            tokens = entry.split()
            cmd, args = _sub(tokens[0]), tokens[1:]
            label = cmd if i == 0 else f"{cmd} (fallback{f' {i}' if i > 1 else ''})"
            chain.append({"command": cmd, "args": args, "name": cmd, "label": label})
        if not chain:
            raise RuntimeError("LSP_SERVERS is empty or malformed")
        return chain

    # Legacy path
    primary_cmd = os.environ.get("LSP_COMMAND")
    if not primary_cmd:
        raise RuntimeError("LSP_SERVERS or LSP_COMMAND environment variable is required")
    primary_cmd = _sub(primary_cmd)

    chain = [{
        "command": primary_cmd,
        "args": os.environ.get("LSP_ARGS", "").split() if os.environ.get("LSP_ARGS") else [],
        "name": primary_cmd,
        "label": primary_cmd,
    }]

    first_fb = os.environ.get("LSP_FALLBACK_COMMAND")
    if first_fb:
        first_fb = _sub(first_fb)
        chain.append({
            "command": first_fb,
            "args": os.environ.get("LSP_FALLBACK_ARGS", "").split() if os.environ.get("LSP_FALLBACK_ARGS") else [],
            "name": first_fb,
            "label": f"{first_fb} (fallback)",
        })

    i = 2
    while True:
        cmd = os.environ.get(f"LSP_FALLBACK_{i}_COMMAND")
        if not cmd:
            break
        cmd = _sub(cmd)
        chain.append({
            "command": cmd,
            "args": os.environ.get(f"LSP_FALLBACK_{i}_ARGS", "").split() if os.environ.get(f"LSP_FALLBACK_{i}_ARGS") else [],
            "name": cmd,
            "label": f"{cmd} (fallback {i})",
        })
        i += 1

    return chain


def _parse_prefer(chain: list[dict]) -> dict[str, int]:
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
            if cfg["command"] == cmd:
                result[method] = idx
                break
    return result


def _ensure_chain_configs() -> list[dict]:
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


async def _warmup_folder(client: LspClient, folder: str) -> int:
    """Bulk-didOpen files matching LSP_WARMUP_PATTERNS under folder. Returns files warmed."""
    patterns = _parse_warmup_patterns()
    if not patterns:
        return 0
    limit = _warmup_max_files()
    if limit <= 0:
        return 0
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
    _folder_warmup_stats[key] = {"count": n, "timestamp": time.time()}
    if n > 0:
        log.info("Warmed %d files in %s for %s", n, folder, _chain_configs[chain_idx]["label"])
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
        client = LspClient([cfg["command"], *cfg["args"]], root)
        await client.start()
        _chain_clients[idx] = client
        if cfg["label"] not in _just_started_this_call:
            _just_started_this_call.append(cfg["label"])
        # Flush any pending workspace adds that were queued before this client existed
        for pending in list(_pending_workspace_adds):
            if client.add_workspace_folder(pending):
                await _maybe_warmup(client, idx, pending)
        # Warm up the primary root too
        await _maybe_warmup(client, idx, client._root_path)
    client = _chain_clients[idx]
    assert client is not None
    return client


async def _request(method: str, params: dict | None, *, uri: str | None = None) -> Any:
    """Route a request through the chain. Caches which server handles each method."""
    global _last_server
    _ensure_chain_configs()
    empty_fallback = _parse_empty_fallback_methods()

    # Fast path: method already resolved to a specific chain index
    if method in _method_handler:
        idx = _method_handler[method]
        if idx is None:
            raise LspError(-32601, f"{method} not supported by any server in the chain")
        client = await _get_client(idx)
        await _ensure_workspace_for(uri)
        if uri:
            await client.ensure_document(uri)
        _last_server = _chain_configs[idx]["label"]
        return await client.request(method, params)

    # Cold path: try each server in order
    last_err: LspError | None = None
    last_empty: Any = None
    last_empty_idx: int | None = None

    for idx in range(len(_chain_configs)):
        client = await _get_client(idx)
        await _ensure_workspace_for(uri)
        if uri:
            await client.ensure_document(uri)
        try:
            result = await client.request(method, params)
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
                _chain_configs[idx]["label"], method,
            )
            continue

        _method_handler[method] = idx
        _last_server = _chain_configs[idx]["label"]
        if idx > 0:
            log.info("Routing %s to %s", method, _chain_configs[idx]["label"])
        return result

    # All servers tried. If one returned an empty result (and no server had an actual
    # match), return the empty result rather than raising — downstream tool formats
    # it as "no results".
    if last_empty_idx is not None:
        _method_handler[method] = last_empty_idx
        _last_server = _chain_configs[last_empty_idx]["label"]
        return last_empty

    _method_handler[method] = None
    raise last_err or LspError(-32601, f"{method} not supported by any server in the chain")


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


# --- Symbol resolution ---


class AmbiguousSymbol(Exception):
    def __init__(self, matches: list[tuple[int, str, str]]):
        self.matches = matches


async def _resolve(
    file_path: str,
    symbol: str = "",
    line: int = 0,
) -> tuple[str, dict]:
    """Resolve a symbol name or line number to a URI + LSP position.

    Resolution pipeline:
    1. If only line given → use it directly (col 0)
    2. If symbol given → documentSymbol search, then text fallback
    3. Multiple matches + line → disambiguate by closest line
    4. Multiple matches, no line → raise AmbiguousSymbol with all matches
    """
    uri = file_uri(file_path)

    if not symbol and line > 0:
        return uri, {"line": line - 1, "character": 0}

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


def _resolve_paths(file_path: str, pattern: str) -> list[str] | str:
    """Resolve multi-file arguments into a list of paths.

    Supports comma-separated file_path and glob patterns.
    Returns a list of paths on success, or an error string if inputs are empty.
    """
    if file_path and "," in file_path:
        return [p.strip() for p in file_path.split(",") if p.strip()]
    if file_path:
        return [file_path]
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_formatting(file_path: str, tab_size: int = 4, insert_spaces: bool = True) -> str:
    """Format an entire document."""
    try:
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_rename(file_path: str, new_name: str, symbol: str = "", line: int = 0) -> str:
    """Rename a symbol across the workspace. Pass symbol name or line number."""
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        result = await _request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": pos,
            "newName": new_name,
        }, uri=uri)
        if not result:
            return "No rename edits returned."

        lines: list[str] = []
        for change_uri, edits in result.get("changes", {}).items():
            path = _uri_to_path(change_uri)
            lines.append(f"{path}: {len(edits)} edit(s)")
            for e in edits:
                lines.append(f"  {_range_str(e.get('range', {}))} → {e.get('newText', '')!r}")

        for doc_change in result.get("documentChanges", []):
            change_uri = doc_change.get("textDocument", {}).get("uri", "")
            path = _uri_to_path(change_uri)
            edits = doc_change.get("edits", [])
            lines.append(f"{path}: {len(edits)} edit(s)")
            for e in edits:
                lines.append(f"  {_range_str(e.get('range', {}))} → {e.get('newText', '')!r}")

        return "\n".join(lines) if lines else "No changes."
    except AmbiguousSymbol as e:
        return _ambiguous_msg(e)
    except (LspError, ValueError) as e:
        return f"LSP error: {e}"


def _apply_text_edits(text: str, edits: list[dict]) -> str:
    """Apply LSP TextEdits to a string. Edits are applied end-to-start to keep offsets valid."""
    # Precompute line start byte offsets
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    sorted_edits = sorted(
        edits,
        key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]),
        reverse=True,
    )

    result = text
    for edit in sorted_edits:
        start = edit["range"]["start"]
        end = edit["range"]["end"]
        s_line = start["line"]
        e_line = end["line"]
        if s_line >= len(line_starts) or e_line >= len(line_starts):
            continue
        start_offset = line_starts[s_line] + start["character"]
        end_offset = line_starts[e_line] + end["character"]
        result = result[:start_offset] + edit["newText"] + result[end_offset:]
    return result


def _apply_workspace_edit(edit: dict) -> list[str]:
    """Apply a WorkspaceEdit to the filesystem. Returns list of affected paths."""
    affected: list[str] = []

    for change_uri, edits in edit.get("changes", {}).items():
        path = _uri_to_path(change_uri)
        text = Path(path).read_text(encoding="utf-8")
        Path(path).write_text(_apply_text_edits(text, edits), encoding="utf-8")
        affected.append(path)

    for doc_change in edit.get("documentChanges", []):
        if "textDocument" in doc_change:
            change_uri = doc_change["textDocument"]["uri"]
            path = _uri_to_path(change_uri)
            edits = doc_change.get("edits", [])
            text = Path(path).read_text(encoding="utf-8")
            Path(path).write_text(_apply_text_edits(text, edits), encoding="utf-8")
            affected.append(path)

    return affected


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
    except (LspError, ConnectionError) as e:
        log.warning("willRenameFiles failed (%s), falling through to rewriter", e)
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
            log.info("python rewriter: %s → %s scanned %d files, %d edit groups",
                     f, t, scanned, len(edit.get("changes", {})))
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
    candidate: dict = {
        "kind": "file_move" if len(files) == 1 else "file_move_batch",
        "title": description,
    }
    if len(files) == 1:
        candidate["from_path"] = files[0][0]
        candidate["to_path"] = files[0][1]
    else:
        candidate["moves"] = [{"from_path": f, "to_path": t} for f, t in files]
    if result:
        candidate["edit"] = result
    _set_pending(candidate["kind"], [candidate], description)

    lines.insert(
        0,
        f"Preview: {len(edit_files)} file(s), {total_edits} edit(s). Call lsp_confirm(0) to commit the move.",
    )

    if total_edits == 0 and len(edit_files) == 0:
        warning = _check_move_discrepancy([f for f, _ in files])
        if warning:
            lines.append("")
            lines.append(warning)
            lines.append("Options: (1) pre-warm importer files via lsp_hover, (2) lsp_add_workspace on the project, (3) fall back to regex rewrite if LSP is unreliable here.")

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
        return await _do_move(list(zip(froms, tos)))
    except (LspError, ValueError, OSError) as e:
        return f"LSP error: {e}"


async def lsp_prepare_rename(file_path: str, symbol: str = "", line: int = 0) -> str:
    """Check if a symbol can be renamed. Pass symbol name or line number."""
    try:
        uri, pos = await _resolve(file_path, symbol, line)
        result = await _request("textDocument/prepareRename", {
            "textDocument": {"uri": uri},
            "position": pos,
        }, uri=uri)
        if not result:
            return "Cannot rename at this position."

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
            return "No code actions available."

        lines = []
        for idx, action in enumerate(result):
            title = action.get("title", "")
            kind = action.get("kind", "")
            edit = action.get("edit")
            parts = [f"[{idx}] {title}"]
            if kind:
                parts.append(f"[{kind}]")
            if edit:
                n = len(edit.get("changes", {})) + len(edit.get("documentChanges", []))
                parts.append(f"({n} file(s))")
            lines.append(" ".join(parts))

        # Stage the raw action objects so lsp_confirm can unwrap their .edit
        # field via _apply_candidate. Some actions have no edit (command-only)
        # — those apply as 0 file(s)/0 edit(s) and are effectively a noop here.
        _set_pending(
            "code_action",
            list(result),
            f"{len(result)} code action(s) at {_uri_to_path(uri)}:{target_line + 1}",
        )

        lines.append("")
        lines.append(f"Staged {len(result)} action(s). Call lsp_confirm(N) to apply.")
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
        info_lines.append(f"  {cfg['label']}: {cfg['command']} {' '.join(cfg['args'])}")

    if _probed_caps:
        info_lines.append("")
        info_lines.append("Probed capabilities (at module load):")
        for cfg, caps in zip(_chain_configs, _probed_caps):
            if not caps:
                info_lines.append(f"  [{cfg['label']}] (probe failed or no caps reported)")
                continue
            key_caps = [k for k in caps.keys() if k.endswith("Provider") or k == "workspace"]
            info_lines.append(f"  [{cfg['label']}] {len(caps)} caps; providers: {', '.join(sorted(key_caps))}")
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
        lines.append(f"[{cfg['label']}]")
        for folder in sorted(client.workspace_folders):
            stats = _folder_warmup_stats.get((idx, folder))
            if stats:
                age = int(now - stats["timestamp"])
                lines.append(f"  {folder}  (warmed {stats['count']} files, {age}s ago)")
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
            results.append(f"[{cfg['label']}] added{suffix}")
        else:
            results.append(f"[{cfg['label']}] already present")
    return "\n".join(results)


async def lsp_confirm(index: int = 0) -> str:
    """Apply one staged candidate from the preview buffer.

    Companion to tools that stage previews (currently ``lsp_code_actions``
    and ``lsp_move_file``). Index into the ``candidates`` list shown by the
    most recent preview. Clears ``_pending`` on success so the buffer is
    single-shot — a stale preview can't be re-committed after context drifts.
    """
    global _pending
    if _pending is None:
        return "Nothing to confirm."

    candidates = _pending.get("candidates", [])
    kind = _pending.get("kind", "")

    if index >= len(candidates):
        return f"Invalid index {index}, only {len(candidates)} candidates available."

    candidate = candidates[index]
    try:
        file_count, edit_count = _apply_candidate(candidate)
    except (OSError, ValueError, KeyError) as e:
        return f"Apply failed: {e}"

    title = candidate.get("title", "")
    _pending = None
    return f"Applied [{kind} #{index}]: {title}. {file_count} file(s), {edit_count} edit(s)."


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
    except LspError as e:
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
    """Format a TypeHierarchyItem to match the workspace_symbols line shape."""
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


async def lsp_workspace_symbols(query: str) -> str:
    """Search for symbols across the entire workspace."""
    try:
        result = await _request("workspace/symbol", {"query": query})
        if not result:
            return "No symbols found."
        lines = []
        for sym in result:
            name = sym.get("name", "")
            kind = _symbol_kind_label(sym.get("kind", 0))
            loc = sym.get("location", {})
            path = _uri_to_path(loc.get("uri", ""))
            sl = loc.get("range", {}).get("start", {}).get("line", 0) + 1
            lines.append(f"{sl}  {kind}  {name}  {path}")
        return "\n".join(lines)
    except LspError as e:
        return f"LSP error: {e}"


async def _folding_range_single(file_path: str) -> str:
    """Folding regions for a single file. Each region reports its 1-based line
    span and its classifying kind (``comment`` / ``imports`` / ``region``).
    """
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
        candidate: dict = {
            "kind": "file_create",
            "create_path": file_path,
            "title": description,
        }
        if result:
            candidate["edit"] = result
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
        candidate: dict = {
            "kind": "file_delete",
            "delete_path": file_path,
            "title": description,
        }
        if result:
            candidate["edit"] = result
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
    "hover": (lsp_hover, "textDocument/hover"),
    "definition": (lsp_definition, "textDocument/definition"),
    "references": (lsp_references, "textDocument/references"),
    "workspace_symbols": (lsp_workspace_symbols, "workspace/symbol"),
    "type_definition": (lsp_type_definition, "textDocument/typeDefinition"),
    "completion": (lsp_completion, "textDocument/completion"),
    "signature_help": (lsp_signature_help, "textDocument/signatureHelp"),
    "document_symbols": (lsp_document_symbols, "textDocument/documentSymbol"),
    "formatting": (lsp_formatting, "textDocument/formatting"),
    "rename": (lsp_rename, "textDocument/rename"),
    "prepare_rename": (lsp_prepare_rename, "textDocument/prepareRename"),
    "move_file": (lsp_move_file, "workspace/willRenameFiles"),
    "code_actions": (lsp_code_actions, "textDocument/codeAction"),
    "call_hierarchy_incoming": (lsp_call_hierarchy_incoming, "callHierarchy/incomingCalls"),
    "call_hierarchy_outgoing": (lsp_call_hierarchy_outgoing, "callHierarchy/outgoingCalls"),
    "implementation": (lsp_implementation, "textDocument/implementation"),
    "declaration": (lsp_declaration, "textDocument/declaration"),
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
        result = await func(*args, **kwargs)
        header = _header(method) if _last_server else f"[{method}]"
        prefix_lines: list[str] = [header]
        for label in _just_started_this_call:
            prefix_lines.append(f"[+started] {label}")
        for p in _added_workspaces_this_call:
            prefix_lines.append(f"[+workspace] {p}")
        prefix = "\n".join(prefix_lines)
        return f"{prefix}\n{result}"

    return wrapper


# Tool → LSP capability path (dotted for nested keys in the initialize response).
# None means the tool is always enabled (e.g. lsp_confirm is client-side).
TOOL_CAPABILITIES: dict[str, str | None] = {
    "diagnostics": "diagnosticProvider",
    "hover": "hoverProvider",
    "definition": "definitionProvider",
    "references": "referencesProvider",
    "workspace_symbols": "workspaceSymbolProvider",
    "type_definition": "typeDefinitionProvider",
    "completion": "completionProvider",
    "signature_help": "signatureHelpProvider",
    "document_symbols": "documentSymbolProvider",
    "formatting": "documentFormattingProvider",
    "rename": "renameProvider",
    "prepare_rename": "renameProvider",
    "code_actions": "codeActionProvider",
    "call_hierarchy_incoming": "callHierarchyProvider",
    "call_hierarchy_outgoing": "callHierarchyProvider",
    "move_file": "workspace.fileOperations.willRename",
    "implementation": "implementationProvider",
    "declaration": "declarationProvider",
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

    async def probe_one(cfg: dict) -> dict:
        root = os.environ.get("LSP_ROOT", os.getcwd())
        client = LspClient([cfg["command"], *cfg["args"]], root)
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
                log.warning("capability probe failed for %s: %s", cfg.get("name"), e)
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
