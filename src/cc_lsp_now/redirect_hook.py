"""PreToolUse hook that redirects the built-in LSP tool to the cc-lsp-now MCP tools.

Wired from a plugin's plugin.json:

    "hooks": {
      "PreToolUse": [
        {
          "matcher": "LSP",
          "hooks": [{"type": "command", "command": "cc-lsp-now-redirect-hook"}]
        }
      ]
    }

Claude Code's built-in LSP tool is incomplete and sometimes buggy (e.g. workspaceSymbol
returning 0 results when the server clearly supports it). This hook denies every LSP()
call with a redirect message, steering the model to the lsp_* MCP tools instead.
"""
from __future__ import annotations

import json
import sys

REDIRECT_MESSAGE = (
    "The built-in LSP tool is disabled in favor of the cc-lsp-now MCP tools "
    "(lsp_hover, lsp_definition, lsp_references, lsp_workspace_symbols, lsp_rename, "
    "lsp_code_actions, lsp_diagnostics, lsp_document_symbols, lsp_completion, "
    "lsp_signature_help, lsp_type_definition, lsp_call_hierarchy_incoming, "
    "lsp_call_hierarchy_outgoing, lsp_prepare_rename). "
    "They accept symbol names directly (e.g. symbol='MyClass'), batch with commas "
    "(symbols='Foo,Bar'), and route through the primary LSP server with automatic "
    "fallback. Reconstruct your call using the appropriate lsp_* MCP tool."
)


def main() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REDIRECT_MESSAGE,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
