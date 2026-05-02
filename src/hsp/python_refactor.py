"""Python-specific import rewriter for file moves.

Invoked when ``LSP_LANGUAGE=python`` and the LSP's ``workspace/willRenameFiles``
doesn't return enough edits. Pyright (and basedpyright) only rewrites explicit
re-exports; ordinary ``from X import Y`` imports are ignored. This module fills
that gap with regex-driven line rewrites, matched against all ``*.py`` files
in known workspace folders.

Module-path inference handles two common Python layouts:
    - ``src/`` layout: ``repo/src/pkg/mod.py`` → ``pkg.mod``
    - flat layout: walks up from the file while ``__init__.py`` exists

Resulting ``WorkspaceEdit`` merges cleanly with the LSP's own edits — the bridge
caller can union both dicts.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _python_module_for(path: str, py_root: str) -> str | None:
    """Convert a filesystem path under py_root into a Python dotted module name."""
    abs_p = os.path.abspath(path)
    abs_root = os.path.abspath(py_root)
    if not abs_p.startswith(abs_root + os.sep):
        return None
    rel = os.path.relpath(abs_p, abs_root)
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith(os.sep + "__init__"):
        rel = rel[: -(len(os.sep) + len("__init__"))]
    return rel.replace(os.sep, ".")


def _find_python_root(file_path: str, workspace_folders: list[str]) -> str | None:
    """Find the Python 'package root' a file lives under. First match wins.

    Tries each workspace folder with these heuristics:
    1. `<folder>/src/` exists and file is under it → src is the root
    2. File is under folder: walk up from file's dir while ``__init__.py`` exists;
       the first parent WITHOUT ``__init__.py`` is the root.
    """
    abs_file = os.path.abspath(file_path)
    for folder in workspace_folders:
        abs_folder = os.path.abspath(folder)
        src_root = os.path.join(abs_folder, "src")
        if os.path.isdir(src_root) and abs_file.startswith(src_root + os.sep):
            return src_root
        if abs_file.startswith(abs_folder + os.sep) or abs_file == abs_folder:
            # Walk up from the file's dir
            p = Path(abs_file).parent
            limit = Path(abs_folder)
            while p != limit and p != p.parent:
                if not (p / "__init__.py").exists():
                    return str(p)
                p = p.parent
            return str(limit)
    return None


def _module_paths(from_path: str, to_path: str, workspace_folders: list[str]) -> tuple[str | None, str | None]:
    """Return (old_module, new_module) as dotted names, or (None, None) if unresolved.

    Both paths must resolve under the same Python root for the rewrite to make sense.
    """
    py_root = _find_python_root(from_path, workspace_folders)
    if not py_root:
        return None, None
    # New path might not exist yet — use the same root for module inference
    old = _python_module_for(from_path, py_root)
    new = _python_module_for(to_path, py_root)
    if old == new:
        return None, None
    return old, new


def _file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def python_import_rewrite(
    from_path: str,
    to_path: str,
    workspace_folders: list[str],
    source_patterns: list[str] | None = None,
    max_files: int = 5000,
) -> tuple[dict, int]:
    """Build a WorkspaceEdit that rewrites imports after moving from_path → to_path.

    Returns (workspace_edit, files_scanned). The edit uses the 'changes' dict
    shape: ``{"changes": {uri: [TextEdit, ...]}}``.
    """
    old_module, new_module = _module_paths(from_path, to_path, workspace_folders)
    if not old_module or not new_module:
        return {"changes": {}}, 0

    old_esc = re.escape(old_module)
    basename = Path(from_path).stem  # quick substring filter

    # Patterns order matters: more-specific first.
    # Each matches a whole import line and captures the parts we want to preserve.
    patterns: list[tuple[re.Pattern, str]] = [
        # from old_module.sub import ...
        (re.compile(rf"(^\s*from\s+){old_esc}(\.[A-Za-z_][\w.]*)(\s+import\b.*$)"),
         rf"\1{new_module}\2\3"),
        # from old_module import ...
        (re.compile(rf"(^\s*from\s+){old_esc}(\s+import\b.*$)"),
         rf"\1{new_module}\2"),
        # import old_module.sub (as X)? (, more)?
        (re.compile(rf"(^\s*import\s+){old_esc}(\.[A-Za-z_][\w.]*)(\s+as\s+[A-Za-z_]\w*)?(.*)$"),
         rf"\1{new_module}\2\3\4"),
        # import old_module (as X)? (, more)?
        (re.compile(rf"(^\s*import\s+){old_esc}(\s+as\s+[A-Za-z_]\w*)?(.*)$"),
         rf"\1{new_module}\2\3"),
    ]

    # Module-attribute import: `from parent_pkg import module_name [as alias]`
    # Only safe to rewrite when the leaf is the SOLE import on the line —
    # multi-import lines like `from foo import helper, Config` would lose Config.
    if "." in old_module and "." in new_module:
        old_parent, old_leaf = old_module.rsplit(".", 1)
        new_parent, new_leaf = new_module.rsplit(".", 1)
        if old_leaf == new_leaf:
            leaf_esc = re.escape(old_leaf)
            old_parent_esc = re.escape(old_parent)
            # `from old_parent import leaf` (end of line or trailing comment)
            patterns.append((
                re.compile(
                    rf"(^\s*from\s+){old_parent_esc}(\s+import\s+){leaf_esc}(\s*(?:#.*)?)$"
                ),
                rf"\1{new_parent}\2{new_leaf}\3",
            ))
            # `from old_parent import leaf as alias`
            patterns.append((
                re.compile(
                    rf"(^\s*from\s+){old_parent_esc}(\s+import\s+){leaf_esc}(\s+as\s+[A-Za-z_]\w*\s*(?:#.*)?)$"
                ),
                rf"\1{new_parent}\2{new_leaf}\3",
            ))

    patterns_glob = source_patterns or ["*.py"]
    from_abs = os.path.abspath(from_path)

    changes: dict[str, list[dict]] = {}
    scanned = 0

    for folder in workspace_folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        for pattern_glob in patterns_glob:
            try:
                candidates = list(root.rglob(pattern_glob))
            except OSError:
                continue
            for fp in candidates:
                if scanned >= max_files:
                    break
                try:
                    fp_abs = str(fp.resolve())
                except OSError:
                    continue
                if fp_abs == from_abs:
                    continue
                scanned += 1
                try:
                    text = fp.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if basename not in text:
                    continue  # quick-reject: module name never appears

                edits: list[dict] = []
                for line_no, line in enumerate(text.splitlines()):
                    rewritten = line
                    for pat, repl in patterns:
                        new_line = pat.sub(repl, rewritten)
                        if new_line != rewritten:
                            rewritten = new_line
                    if rewritten != line:
                        edits.append({
                            "range": {
                                "start": {"line": line_no, "character": 0},
                                "end": {"line": line_no, "character": len(line)},
                            },
                            "newText": rewritten,
                        })

                if edits:
                    changes[_file_uri(fp_abs)] = edits

    return {"changes": changes}, scanned


def merge_workspace_edits(a: dict, b: dict) -> dict:
    """Union two WorkspaceEdits. Both must be in 'changes' shape.

    For URIs in both, concatenate edits. No overlap detection — caller is
    expected to drop (or not produce) overlapping edits.
    """
    merged_changes: dict[str, list[dict]] = {}
    for src in (a, b):
        for uri, edits in (src or {}).get("changes", {}).items():
            merged_changes.setdefault(uri, []).extend(edits)
    result: dict = {"changes": merged_changes}
    # Preserve documentChanges from the first dict if any
    doc_changes = (a or {}).get("documentChanges", []) + (b or {}).get("documentChanges", [])
    if doc_changes:
        result["documentChanges"] = doc_changes
    return result
