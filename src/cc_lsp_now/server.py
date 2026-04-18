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
# Methods the primary returned -32601 for — skip straight to fallback next time
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


async def _get_primary() -> LspClient:
    global _primary
    if _primary is None:
        command = os.environ.get("LSP_COMMAND")
        if not command:
            raise RuntimeError("LSP_COMMAND environment variable is required")
        args = os.environ.get("LSP_ARGS", "").split() if os.environ.get("LSP_ARGS") else []
        root = os.environ.get("LSP_ROOT", os.getcwd())
        _primary = LspClient([command, *args], root)
        await _primary.start()
    return _primary


async def _get_fallback() -> LspClient | None:
    global _fallback
    fallback_cmd = os.environ.get("LSP_FALLBACK_COMMAND")
    if not fallback_cmd:
        return None
    if _fallback is None:
        args = (
            os.environ.get("LSP_FALLBACK_ARGS", "").split()
            if os.environ.get("LSP_FALLBACK_ARGS") else []
        )
        root = os.environ.get("LSP_ROOT", os.getcwd())
        _fallback = LspClient([fallback_cmd, *args], root)
        await _fallback.start()
    return _fallback


async def _request(method: str, params: dict | None, *, uri: str | None = None) -> Any:
    """Route a request through primary, falling back on -32601 (method not found).

    Caches unsupported methods so subsequent calls skip the primary entirely.
    """
    if method in _primary_unsupported:
        fallback = await _get_fallback()
        if fallback is None:
            raise LspError(-32601, f"{method} not supported and no fallback configured")
        if uri:
            await fallback.ensure_document(uri)
        return await fallback.request(method, params)

    primary = await _get_primary()
    if uri:
        await primary.ensure_document(uri)
    try:
        return await primary.request(method, params)
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
        return await fallback.request(method, params)


def _pos(line: int, col: int) -> dict:
    return {"line": line - 1, "character": col - 1}


def _format_location(loc: dict) -> dict:
    uri = loc.get("uri", "")
    path = uri.removeprefix("file://") if uri.startswith("file://") else uri
    start = loc.get("range", {}).get("start", {})
    return {
        "file_path": path,
        "line": start.get("line", 0) + 1,
        "col": start.get("character", 0) + 1,
    }


def _format_range(r: dict) -> dict:
    s = r.get("start", {})
    e = r.get("end", {})
    return {
        "start": {"line": s.get("line", 0) + 1, "col": s.get("character", 0) + 1},
        "end": {"line": e.get("line", 0) + 1, "col": e.get("character", 0) + 1},
    }


def _severity_label(n: int) -> str:
    return SEVERITY_LABELS.get(n, f"Unknown({n})")


def _symbol_kind_label(n: int) -> str:
    return SYMBOL_KIND_LABELS.get(n, f"Unknown({n})")


def _format_diagnostic(d: dict) -> dict:
    return {
        "severity": _severity_label(d.get("severity", 0)),
        "message": d.get("message", ""),
        "range": _format_range(d.get("range", {})),
        "source": d.get("source"),
        "code": d.get("code"),
    }


def _normalize_locations(result: dict | list | None) -> list[dict]:
    if result is None:
        return []
    if isinstance(result, dict):
        result = [result]
    return [_format_location(loc) for loc in result]


def _format_symbol(sym: dict) -> dict:
    out: dict = {
        "name": sym.get("name", ""),
        "kind": _symbol_kind_label(sym.get("kind", 0)),
    }
    if "range" in sym:
        out["range"] = _format_range(sym["range"])
    elif "location" in sym:
        out["location"] = _format_location(sym["location"])
    children = sym.get("children")
    if children:
        out["children"] = [_format_symbol(c) for c in children]
    return out


@mcp.tool()
async def lsp_type_definition(file_path: str, line: int, col: int) -> str:
    """Go to the type definition of a symbol."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/typeDefinition", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        return json.dumps(_normalize_locations(result), indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
async def lsp_completion(file_path: str, line: int, col: int) -> str:
    """Get completion suggestions at a position."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        if not result:
            return json.dumps([], indent=2)

        items = result if isinstance(result, list) else result.get("items", [])
        completions = []
        for item in items[:50]:
            completions.append({
                "label": item.get("label", ""),
                "kind": item.get("kind"),
                "detail": item.get("detail"),
                "insertText": item.get("insertText") or item.get("label", ""),
            })
        return json.dumps(completions, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
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


@mcp.tool()
async def lsp_document_symbols(file_path: str) -> str:
    """Get all symbols in a document (outline)."""
    try:
        uri = file_uri(file_path)
        result = await _request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        }, uri=uri)
        if not result:
            return json.dumps([], indent=2)
        return json.dumps([_format_symbol(s) for s in result], indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
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
            return json.dumps([], indent=2)

        edits = []
        for edit in result:
            edits.append({
                "range": _format_range(edit.get("range", {})),
                "newText": edit.get("newText", ""),
            })
        return json.dumps(edits, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
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
            return json.dumps({"changes": {}}, indent=2)

        changes: dict[str, list] = {}
        raw_changes = result.get("changes", {})
        for change_uri, edits in raw_changes.items():
            path = change_uri.removeprefix("file://") if change_uri.startswith("file://") else change_uri
            changes[path] = [
                {"range": _format_range(e.get("range", {})), "newText": e.get("newText", "")}
                for e in edits
            ]

        doc_changes = result.get("documentChanges", [])
        for doc_change in doc_changes:
            text_doc = doc_change.get("textDocument", {})
            change_uri = text_doc.get("uri", "")
            path = change_uri.removeprefix("file://") if change_uri.startswith("file://") else change_uri
            edits = doc_change.get("edits", [])
            changes[path] = [
                {"range": _format_range(e.get("range", {})), "newText": e.get("newText", "")}
                for e in edits
            ]

        return json.dumps({"changes": changes}, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
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
            return json.dumps({
                "range": _format_range(result["range"]),
                "placeholder": result["placeholder"],
            }, indent=2)
        if "start" in result:
            return json.dumps({"range": _format_range(result)}, indent=2)
        return json.dumps(result, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
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
            return json.dumps([], indent=2)

        actions = []
        for action in result:
            edit = action.get("edit")
            edit_summary = None
            if edit:
                file_count = len(edit.get("changes", {})) + len(edit.get("documentChanges", []))
                edit_summary = f"{file_count} file(s) affected"
            actions.append({
                "title": action.get("title", ""),
                "kind": action.get("kind"),
                "edit_summary": edit_summary,
            })
        return json.dumps(actions, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
async def lsp_call_hierarchy_incoming(file_path: str, line: int, col: int) -> str:
    """Find all callers of a function/method."""
    try:
        uri = file_uri(file_path)
        items = await _request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        if not items:
            return json.dumps([], indent=2)

        result = await _request("callHierarchy/incomingCalls", {"item": items[0]})
        if not result:
            return json.dumps([], indent=2)

        callers = []
        for call in result:
            from_item = call.get("from", {})
            callers.append({
                "name": from_item.get("name", ""),
                "kind": _symbol_kind_label(from_item.get("kind", 0)),
                "location": _format_location({
                    "uri": from_item.get("uri", ""),
                    "range": from_item.get("range", {}),
                }),
                "call_sites": [_format_range(r) for r in call.get("fromRanges", [])],
            })
        return json.dumps(callers, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


@mcp.tool()
async def lsp_call_hierarchy_outgoing(file_path: str, line: int, col: int) -> str:
    """Find all functions/methods called by a function/method."""
    try:
        uri = file_uri(file_path)
        items = await _request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": _pos(line, col),
        }, uri=uri)
        if not items:
            return json.dumps([], indent=2)

        result = await _request("callHierarchy/outgoingCalls", {"item": items[0]})
        if not result:
            return json.dumps([], indent=2)

        callees = []
        for call in result:
            to_item = call.get("to", {})
            callees.append({
                "name": to_item.get("name", ""),
                "kind": _symbol_kind_label(to_item.get("kind", 0)),
                "location": _format_location({
                    "uri": to_item.get("uri", ""),
                    "range": to_item.get("range", {}),
                }),
                "call_sites": [_format_range(r) for r in call.get("fromRanges", [])],
            })
        return json.dumps(callees, indent=2)
    except LspError as e:
        return f"LSP error: {e}"


def run() -> None:
    mcp.run(transport="stdio")
