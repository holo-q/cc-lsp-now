from __future__ import annotations

import os
import shutil
from pathlib import Path


INSTALL_HINTS: dict[str, str] = {
    "rust-analyzer": "Install with `rustup component add rust-analyzer` or your system package manager.",
    "ty": "Install with `uv tool install ty` or ensure `ty` is on PATH.",
    "basedpyright-langserver": "Install with `npm install -g basedpyright` or ensure `basedpyright-langserver` is on PATH.",
    "csharp-ls": "Install with `dotnet tool install --global csharp-ls` or ensure `csharp-ls` is on PATH.",
}


def lsp_command_available(command: str) -> bool:
    if not command:
        return False
    if "/" in command:
        path = Path(command)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


def missing_lsp_binary_message(
    command: str,
    *,
    route_id: str = "",
    language: str = "",
    server_label: str = "",
) -> str:
    owner = route_id or language
    subject = f"{owner} route" if owner else "LSP chain"
    label = f" ({server_label})" if server_label and server_label != command else ""
    hint = INSTALL_HINTS.get(command, f"Install `{command}` or put it on PATH visible to HSP.")
    return (
        f"Missing LSP server binary for {subject}: `{command}`{label}. "
        f"{hint} Restart Codex/HSP if PATH changed after the session started."
    )
