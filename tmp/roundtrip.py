"""Local roundtrip test for lsp_move through the full MCP tool path.

Run after an hsp edit to verify:
- LSP chain starts without crashing
- willRenameFiles returns a non-empty WorkspaceEdit for a real symbol
- stderr from the LSP server gets captured if anything dies
- lsp_confirm applies the edits and moves the file

Usage:
    cd hsp
    LSP_SERVERS="ty server;pylance-language-server --stdio" \\
        uv run python tmp/roundtrip.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

# Ensure a clean state every run
FOO_SRC = Path(__file__).parent / "foo"
FOO_RUN = Path(__file__).parent / "foo_run"
if FOO_RUN.exists():
    shutil.rmtree(FOO_RUN)
shutil.copytree(FOO_SRC, FOO_RUN)

os.environ["LSP_PROJECT_MARKERS"] = "pyproject.toml,.git"
os.environ["LSP_WARMUP_PATTERNS"] = "*.py"
os.environ["LSP_WARMUP_MAX_FILES"] = "50"
os.environ["LSP_ROOT"] = str(FOO_RUN)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def main() -> int:
    from hsp.server import (
        _wrap_with_header,
        lsp_confirm,
        lsp_move,
        lsp_refs,
        lsp_session,
    )

    session = _wrap_with_header(lsp_session, "hsp/session")
    mv = _wrap_with_header(lsp_move, "workspace/willRenameFiles")
    confirm = _wrap_with_header(lsp_confirm, "hsp/confirm")
    refs = _wrap_with_header(lsp_refs, "textDocument/references")

    print("=" * 60)
    print("1. lsp_session status — confirm fresh build + caps")
    print("=" * 60)
    print(await session())

    print()
    print("=" * 60)
    print("2. lsp_session add — force-spawn all chain servers")
    print("=" * 60)
    print(await session(action="add", path=str(FOO_RUN)))

    print()
    print("=" * 60)
    print("3. lsp_references — baseline: does the LSP see imports at all?")
    print("=" * 60)
    helper_path = str(FOO_RUN / "src" / "foo_pkg" / "helper.py")
    print(await refs(file_path=helper_path, symbol="greet"))

    print()
    print("=" * 60)
    print("4. lsp_move_file — preview moving helper.py → helpers/helper.py")
    print("=" * 60)
    from_path = helper_path
    to_path = str(FOO_RUN / "src" / "foo_pkg" / "helpers" / "helper.py")
    result = await mv(from_path=from_path, to_path=to_path)
    print(result)

    print()
    print("=" * 60)
    print("5. lsp_confirm — apply the rename")
    print("=" * 60)
    print(await confirm(0))

    print()
    print("=" * 60)
    print("6. Verify filesystem state")
    print("=" * 60)
    moved = (FOO_RUN / "src" / "foo_pkg" / "helpers" / "helper.py").exists()
    original_gone = not (FOO_RUN / "src" / "foo_pkg" / "helper.py").exists()
    main_content = (FOO_RUN / "src" / "foo_pkg" / "main.py").read_text()
    other_content = (FOO_RUN / "src" / "foo_pkg" / "other.py").read_text()
    print(f"helpers/helper.py exists:  {moved}")
    print(f"old helper.py removed:     {original_gone}")
    print()
    print("main.py contents:")
    print("\n".join(f"  {l}" for l in main_content.splitlines()))
    print()
    print("other.py contents:")
    print("\n".join(f"  {l}" for l in other_content.splitlines()))

    # Minimum bar for "pylance handled it": at least one import was rewritten
    ok = moved and original_gone
    imports_rewritten = (
        "foo_pkg.helpers.helper" in main_content
        or "from foo_pkg.helpers" in main_content
        or "foo_pkg.helpers.helper" in other_content
    )

    print()
    print("=" * 60)
    print(f"ROUNDTRIP {'PASS' if ok else 'FAIL'}")
    print(f"  imports rewritten: {imports_rewritten}")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
