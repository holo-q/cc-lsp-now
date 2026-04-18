from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from cc_lsp_now.lsp import LspClient, LspError, file_uri

log = logging.getLogger(__name__)

mcp = FastMCP("lsp-bridge")

_primary: LspClient | None = None
_fallback: LspClient | None = None
_primary_unsupported: set[str] = set()

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


async def _get_primary() -> LspClient:
    global _primary, _primary_name
    if _primary is None:
        command = os.environ.get("LSP_COMMAND")
        if not command:
            raise RuntimeError("LSP_COMMAND environment variable is required")
        args = os.environ.get("LSP_ARGS", "").split() if os.environ.get("LSP_ARGS") else []
        root = os.environ.get("LSP_ROOT", os.getcwd())
        _primary_name = command
        _primary = LspClient([command, *args], root)
        await _primary.start()
    return _primary


async def _get_fallback() -> LspClient | None:
    global _fallback, _fallback_name
    fallback_cmd = os.environ.get("LSP_FALLBACK_COMMAND")
    if not fallback_cmd:
        return None
    if _fallback is None:
        args = (
            os.environ.get("LSP_FALLBACK_ARGS", "").split()
            if os.environ.get("LSP_FALLBACK_ARGS") else []
        )
        root = os.environ.get("LSP_ROOT", os.getcwd())
        _fallback_name = fallback_cmd
        _fallback = LspClient([fallback_cmd, *args], root)
        await _fallback.start()
    return _fallback


_last_server: str = ""
_primary_name: str = ""
_fallback_name: str = ""


async def _request(method: str, params: dict | None, *, uri: str | None = None) -> Any:
    global _last_server
    if method in _primary_unsupported:
        fallback = await _get_fallback()
        if fallback is None:
            raise LspError(-32601, f"{method} not supported and no fallback configured")
        if uri:
            await fallback.ensure_document(uri)
        _last_server = f"{_fallback_name} (fallback)"
        return await fallback.request(method, params)

    primary = await _get_primary()
    if uri:
        await primary.ensure_document(uri)
    try:
        result = await primary.request(method, params)
        _last_server = _primary_name
        return result
    except LspError as e:
        if e.code != -32601:
            raise
        _primary_unsupported.add(method)
        log.info("Primary LSP does not support %s, routing to fallback", method)
        fallback = await _get_fallback()
        if fallback is None:
            raise
        if uri:
            await fallback.ensure_document(uri)
        _last_server = f"{_fallback_name} (fallback)"
        return await fallback.request(method, params)


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


# --- Tool implementations ---


async def lsp_type_definition(file_path: str, line: int, col: int) -> str:
    """Go to the type definition of a symbol."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/typeDefinition", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No type definition found."
        return "\n".join(locs)
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_completion(file_path: str, line: int, col: int) -> str:
    """Get completion suggestions at a position."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_signature_help(file_path: str, line: int, col: int) -> str:
    """Get signature help at a position (function parameter info)."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/signatureHelp", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_document_symbols(file_path: str) -> str:
    """Get all symbols in a document (outline)."""
    try:
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


async def lsp_rename(file_path: str, line: int, col: int, new_name: str) -> str:
    """Rename a symbol across the workspace."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_prepare_rename(file_path: str, line: int, col: int) -> str:
    """Check if a symbol can be renamed and get its current range/name."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/prepareRename", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        if not result:
            return "Cannot rename at this position."

        if "range" in result and "placeholder" in result:
            return f"{_range_str(result['range'])} — current name: {result['placeholder']!r}"
        if "start" in result:
            return f"Renameable at {_range_str(result)}"
        return json.dumps(result, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_code_actions(
    file_path: str,
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> str:
    """Get available code actions (quick fixes, refactorings) for a range."""
    try:
        uri = file_uri(file_path)
        primary = await _get_primary()
        stored = primary.diagnostics.get(uri, [])
        range_diagnostics = []
        for d in stored:
            d_range = d.get("range", {})
            d_start = d_range.get("start", {})
            d_end = d_range.get("end", {})
            if (d_start.get("line", 0) >= start_line - 1 and
                    d_end.get("line", 0) <= end_line - 1):
                range_diagnostics.append(d)

        result = await _request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {
                "start": _pos(start_line, start_col),
                "end": _pos(end_line, end_col),
            },
            "context": {"diagnostics": range_diagnostics},
        }, uri=uri)
        if not result:
            return "No code actions available."

        lines = []
        for action in result:
            title = action.get("title", "")
            kind = action.get("kind", "")
            edit = action.get("edit")
            parts = [f"- {title}"]
            if kind:
                parts.append(f"[{kind}]")
            if edit:
                n = len(edit.get("changes", {})) + len(edit.get("documentChanges", []))
                parts.append(f"({n} file(s))")
            lines.append(" ".join(parts))
        return "\n".join(lines)
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_call_hierarchy_incoming(file_path: str, line: int, col: int) -> str:
    """Find all callers of a function/method."""
    try:
        uri = file_uri(file_path)
        items = await _request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_call_hierarchy_outgoing(file_path: str, line: int, col: int) -> str:
    """Find all functions/methods called by a function/method."""
    try:
        uri = file_uri(file_path)
        items = await _request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_diagnostics(file_path: str) -> str:
    """Get diagnostics (errors, warnings) for a file."""
    try:
        uri = file_uri(file_path)
        diagnostics = []
        try:
            result = await _request("textDocument/diagnostic", {
                "textDocument": {"uri": uri},
            }, uri=uri)
            diagnostics = result.get("items", []) if result else []
        except LspError:
            primary = await _get_primary()
            diagnostics = primary.diagnostics.get(uri, [])
        if not diagnostics:
            return "No diagnostics."
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_hover(file_path: str, line: int, col: int) -> str:
    """Get hover information (type info, docs) at a position."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
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
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_definition(file_path: str, line: int, col: int) -> str:
    """Go to the definition of a symbol."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No definition found."
        return "\n".join(locs)
    except LspError as e:
        return f"LSP error: {e}"


async def lsp_references(file_path: str, line: int, col: int, include_declaration: bool = True) -> str:
    """Find all references to a symbol."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
            "context": {"includeDeclaration": include_declaration},
        }, uri=uri)
        locs = _normalize_locations(result)
        if not locs:
            return "No references found."
        return "\n".join(locs)
    except LspError as e:
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
    "code_actions": (lsp_code_actions, "textDocument/codeAction"),
    "call_hierarchy_incoming": (lsp_call_hierarchy_incoming, "callHierarchy/incomingCalls"),
    "call_hierarchy_outgoing": (lsp_call_hierarchy_outgoing, "callHierarchy/outgoingCalls"),
}


def _wrap_with_header(func: Any, method: str) -> Any:
    import functools

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        global _last_server
        _last_server = ""
        result = await func(*args, **kwargs)
        header = _header(method) if _last_server else f"[{method}]"
        return f"{header}\n{result}"

    return wrapper


_tools_env = os.environ.get("LSP_TOOLS", "")
_disabled_env = os.environ.get("LSP_DISABLED_TOOLS", "")

if _tools_env == "all":
    _enabled = set(_ALL_TOOLS)
elif _tools_env:
    _enabled = {t.strip() for t in _tools_env.split(",")}
else:
    _enabled = set(_ALL_TOOLS) - DISABLED_BY_DEFAULT

if _disabled_env:
    _enabled -= {t.strip() for t in _disabled_env.split(",")}

for _name, (_func, _method) in _ALL_TOOLS.items():
    if _name in _enabled:
        mcp.tool()(_wrap_with_header(_func, _method))


def run() -> None:
    mcp.run(transport="stdio")
