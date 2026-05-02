from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from hsp import main as hsp_main
from hsp import server
from hsp.bus_event import BusEventKind


class CliLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_bus = server._local_bus
        server._local_bus = None

    def tearDown(self) -> None:
        server._local_bus = self._previous_bus

    def test_project_keeps_single_hsp_entrypoint_for_log(self) -> None:
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        self.assertIn("hsp", scripts)
        self.assertNotIn("hsp-log", scripts)
        self.assertNotIn("hsp-hook", scripts)

    def test_entrypoint_dispatches_log_weather_without_starting_mcp_stdio(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run(["log", "weather"], root=root)

        self.assertIn("workspace:", out)
        self.assertIn("open questions: 0", out)
        self.assertIn("recent: 0", out)

    def test_log_hook_requires_kind(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch.dict(os.environ, {"HSP_BROKER": "off", "LSP_ROOT": root}, clear=False):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as cm:
                        hsp_main(["log", "hook", "--files", "src/server.py"])

        self.assertEqual(cm.exception.code, 2)
        self.assertIn("requires --kind", stderr.getvalue())

    def test_log_hook_records_hook_kind_and_scope(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run([
                "log",
                "hook",
                "--kind",
                "edit.after",
                "--files",
                "src/server.py",
                "--symbols",
                "lsp_log",
                "--status",
                "done",
                "--targets",
                "tests/test_cli_log.py",
                "--commit",
                "abc1234",
            ], root=root)
            event = self._read_last_event(root)
            scope = self._require_dict(event, "scope")
            metadata = self._require_dict(event, "metadata")

        self.assertIn("logged E1 edit.after", out)
        self.assertEqual(event["kind"], "edit.after")
        self.assertEqual(scope["files"], ["src/server.py"])
        self.assertEqual(scope["symbols"], ["lsp_log"])
        self.assertEqual(metadata["status"], "done")
        self.assertEqual(metadata["targets"], "['tests/test_cli_log.py']")
        self.assertEqual(metadata["commit"], "abc1234")

    def test_log_ask_and_reply_round_trip_on_one_local_bus(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            ask = self._run([
                "log",
                "ask",
                "--message",
                "Anyone touching server.py?",
                "--timeout",
                "30s",
            ], root=root)
            qid = next(token.strip("():,") for token in ask.split() if token.startswith("Q"))
            reply = self._run([
                "log",
                "reply",
                "--id",
                qid,
                "--message",
                "done",
            ], root=root)

        self.assertIn("opened", ask)
        self.assertIn(qid, reply)
        self.assertIn("reply recorded", reply)

    def test_hook_kind_aliases_normalize_to_canonical_kind(self) -> None:
        self.assertIs(BusEventKind.from_wire("test.result"), BusEventKind.TEST)
        self.assertIs(BusEventKind.from_wire("lsp_confirm.after"), BusEventKind.CONFIRM_AFTER)
        self.assertIs(BusEventKind.from_wire("git.push"), BusEventKind.PUSH_AFTER)

    def test_bundled_hook_command_is_noop_until_env_enabled(self) -> None:
        payload = json.dumps({
            "hookEventName": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "--kind", "edit.after"], root=root, stdin=payload, enabled=False)
            path = Path(root) / "tmp" / "hsp-bus.jsonl"

        self.assertEqual(out, "")
        self.assertFalse(path.exists())

    def test_bundled_hook_records_harness_payload_when_env_enabled(self) -> None:
        payload = json.dumps({
            "hookEventName": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
            "tool_response": {"success": True},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "--kind", "edit.after"], root=root, stdin=payload, enabled=True)
            event = self._read_last_event(root)
            scope = self._require_dict(event, "scope")

        self.assertEqual(out, "")
        self.assertEqual(event["kind"], "edit.after")
        self.assertEqual(event["message"], "PostToolUse Edit")
        self.assertEqual(scope["files"], ["src/hsp/server.py"])

    def test_claude_plugin_bundles_env_gated_bus_hooks(self) -> None:
        data = json.loads(Path(".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        hooks = data["hooks"]
        self.assertIn("SessionStart", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PreToolUse", hooks)
        self.assertIn("PostToolUse", hooks)
        commands = "\n".join(_plugin_hook_commands(hooks))
        self.assertIn("hsp hook --kind session.start", commands)
        self.assertIn("hsp hook --kind prompt", commands)
        self.assertIn("hsp hook --kind edit.before", commands)
        self.assertIn("hsp hook --kind edit.after", commands)
        self.assertIn("HSP_HOOKS", commands)
        self.assertIn("cat >/dev/null", commands)
        self.assertNotIn("hsp-hook", commands)

    def _run(self, argv: list[str], *, root: str) -> str:
        out = io.StringIO()
        with patch.dict(os.environ, {"HSP_BROKER": "off", "LSP_ROOT": root}, clear=False):
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    hsp_main(argv)
        self.assertEqual(cm.exception.code, 0)
        return out.getvalue()

    def _run_hook(self, argv: list[str], *, root: str, stdin: str, enabled: bool) -> str:
        out = io.StringIO()
        env = {
            "HSP_BROKER": "off",
            "LSP_ROOT": root,
            "HSP_HOOKS": "1" if enabled else "0",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("sys.stdin", io.StringIO(stdin)):
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as cm:
                        hsp_main(argv)
        self.assertEqual(cm.exception.code, 0)
        return out.getvalue()

    def _read_last_event(self, root: str) -> dict[str, object]:
        path = Path(root) / "tmp" / "hsp-bus.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines, f"no bus events written to {path}")
        event = json.loads(lines[-1])
        self.assertIsInstance(event, dict)
        return event

    def _require_dict(self, container: dict[str, object], key: str) -> dict[str, object]:
        value = container[key]
        self.assertIsInstance(value, dict)
        return cast(dict[str, object], value)


def _plugin_hook_commands(hooks: dict[str, object]) -> list[str]:
    commands: list[str] = []
    for entries_obj in hooks.values():
        if not isinstance(entries_obj, list):
            continue
        for entry_obj in entries_obj:
            if not isinstance(entry_obj, dict):
                continue
            entry = cast(dict[str, object], entry_obj)
            hook_list = entry.get("hooks")
            if not isinstance(hook_list, list):
                continue
            for hook_obj in hook_list:
                if not isinstance(hook_obj, dict):
                    continue
                hook = cast(dict[str, object], hook_obj)
                command = hook.get("command")
                if isinstance(command, str):
                    commands.append(command)
    return commands


if __name__ == "__main__":
    unittest.main()
