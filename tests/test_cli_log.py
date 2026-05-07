from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from hsp import cli as hsp_cli
from hsp import main as hsp_main
from hsp import server
from hsp.agent_bus import AgentBus
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
        self.assertEqual(scripts["hsp"], "hsp:main")
        self.assertEqual(scripts["hsp-ty"], "hsp:mcp_main")
        self.assertEqual(scripts["hsp-csharp"], "hsp:mcp_main")
        self.assertNotIn("hsp-log", scripts)
        self.assertNotIn("hsp-hook", scripts)
        self.assertNotIn("hsp-run", scripts)

    def test_plugin_configs_run_mcp_subcommand(self) -> None:
        configs = [
            Path(".mcp.json"),
            Path("plugins/hsp/.mcp.json"),
            Path(".claude-plugin/plugin.json"),
        ]
        for path in configs:
            with self.subTest(path=str(path)):
                data = json.loads(path.read_text(encoding="utf-8"))
                server_cfg = data["mcpServers"]["hsp"]

                self.assertEqual(server_cfg["command"], "uvx")
                self.assertEqual(server_cfg["args"][-2:], ["hsp", "mcp"])

    def test_bare_entrypoint_defaults_to_workgroup_status(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with contextlib.chdir(root):
                out = self._run([], root=root)

        self.assertIn(f"workgroup: {Path(root).resolve()}", out)
        self.assertIn("workspace_id:", out)
        self.assertIn("broker: disabled", out)

    def test_mcp_subcommand_runs_stdio_server(self) -> None:
        run = Mock()
        with patch("hsp.cli._server", return_value=SimpleNamespace(run=run)):
            with self.assertRaises(SystemExit) as cm:
                hsp_main(["mcp"])

        self.assertEqual(cm.exception.code, 0)
        run.assert_called_once_with()

    def test_entrypoint_dispatches_log_weather_without_starting_mcp_stdio(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run(["log", "weather"], root=root)

        self.assertIn("workspace:", out)
        self.assertIn("open questions: 0", out)
        self.assertIn("recent: 0", out)

    def test_workgroup_command_reports_root_and_bus_logs(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_with_env(
                ["workgroup", root],
                {
                    "HSP_BROKER": "off",
                    "LSP_ROOT": root,
                    "HSP_WORKGROUP_BOUNDARY": root,
                },
            )

        self.assertIn(f"workgroup: {Path(root).resolve()}", out)
        self.assertIn("workgroup_source: fallback", out)
        self.assertIn("gate policy: build=project checker=file/project journal=workgroup", out)
        self.assertIn("workspace_id:", out)
        self.assertIn("append log:", out)
        self.assertIn("broker: disabled", out)

    def test_workgroup_command_reports_nested_stack_and_project_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            umbrella = Path(root)
            domain = umbrella / "domain"
            project = domain / "app"
            project.mkdir(parents=True)
            (umbrella / "workgroup.toml").write_text(
                "[workgroup]\nname = 'umbrella'\nlevel = 'umbrella'\n",
                encoding="utf-8",
            )
            (domain / "workgroup.toml").write_text(
                "[workgroup]\nname = 'domain'\nlevel = 'domain'\n",
                encoding="utf-8",
            )
            (project / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")

            out = self._run_with_env(
                ["workgroup", str(project)],
                {
                    "HSP_BROKER": "off",
                    "LSP_ROOT": root,
                    "HSP_WORKGROUP_BOUNDARY": root,
                },
            )

        self.assertIn(f"workgroup: {domain.resolve()}", out)
        self.assertIn("workgroup_source: marker", out)
        self.assertIn(f"project: {project.resolve()}", out)
        self.assertIn(f"parent umbrella umbrella: {umbrella.resolve()}", out)
        self.assertIn(f"active domain domain: {domain.resolve()}", out)

    def test_workgroup_command_queries_weather_by_default(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                if method == "bus.status":
                    return {"event_count": 2, "last_event_id": "E2", "open_question_count": 0}
                return {
                    "workspace_root": "/workspace/domain",
                    "agents": [],
                    "open_questions": [],
                    "recent": [],
                }

        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch("hsp.cli._open_cli_broker", return_value=FakeBroker()):
                out = self._run_with_env(
                    ["workgroup", root],
                    {
                        "HSP_BROKER": "auto",
                        "LSP_ROOT": root,
                        "HSP_WORKGROUP_ROOT": root,
                    },
                )

        self.assertIn("broker: reachable", out)
        self.assertIn("weather:", out)
        self.assertIn("events=2", out)

    def test_workgroup_weather_uses_lightweight_broker_renderer(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                if method == "bus.status":
                    return {"event_count": 2, "last_event_id": "E2", "open_question_count": 0}
                return {
                    "workspace_root": "/workspace/domain",
                    "agents": [{"agent_id": "agent-a", "state": "awake", "idle_seconds": 1}],
                    "open_questions": [],
                    "recent": [{"event_id": "E2", "event_type": "note.posted", "message": "done"}],
                }

        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch("hsp.cli._open_cli_broker", return_value=FakeBroker()):
                with patch("hsp.cli._server", side_effect=AssertionError("server import not allowed")):
                    out = self._run_with_env(
                        ["workgroup", "--weather", root],
                        {
                            "HSP_BROKER": "auto",
                            "LSP_ROOT": root,
                            "HSP_WORKGROUP_ROOT": root,
                        },
                    )

        self.assertIn("broker: reachable", out)
        self.assertIn("agents: 1", out)
        self.assertIn("E2 note.posted done", out)

    def test_watch_once_renders_workgroup_subtree_events(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                self.method = method
                self.params = params
                return {
                    "events": [
                        {
                            "event_id": "E7",
                            "event_type": "tool.before",
                            "workspace_root": "/workspace/domain/app",
                            "agent_id": "agent-a",
                            "message": "Bash cargo check",
                            "files": ["src/lib.py"],
                        }
                    ],
                    "last_event_id": "E7",
                }

        broker = FakeBroker()
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            Path(root, "workgroup.toml").write_text(
                "[workgroup]\nname = 'workspace'\nlevel = 'umbrella'\n",
                encoding="utf-8",
            )
            with patch("hsp.cli._open_cli_broker", return_value=broker):
                out = self._run_with_env(
                    ["watch", "--once", root],
                    {
                        "HSP_BROKER": "auto",
                        "LSP_ROOT": root,
                        "HSP_WORKGROUP_BOUNDARY": root,
                    },
                )

        self.assertEqual(broker.method, "bus.recent_tree")
        self.assertIn("watch: broker=", out)
        self.assertIn("scope=", out)
        self.assertIn("subtree", out)
        self.assertIn("/workspace/domain/app E7 tool.before Bash cargo check", out)
        self.assertIn("[files=src/lib.py]", out)

    def test_watch_exact_uses_workspace_scoped_recent(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                self.method = method
                self.params = params
                return {"events": [], "last_event_id": ""}

        broker = FakeBroker()
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch("hsp.cli._open_cli_broker", return_value=broker):
                out = self._run_with_env(
                    ["watch", "--exact", "--once", root],
                    {
                        "HSP_BROKER": "auto",
                        "LSP_ROOT": root,
                        "HSP_WORKGROUP_ROOT": root,
                    },
                )

        self.assertEqual(broker.method, "bus.recent")
        self.assertIn("exact", out)
        self.assertIn("watch: no events", out)

    def test_watch_global_once_uses_broker_wide_recent(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                self.method = method
                self.params = params
                return {
                    "events": [
                        {
                            "event_id": "E8",
                            "event_type": "hook.received",
                            "workspace_root": "/workspace/domain",
                            "message": "PostToolUse Edit",
                        }
                    ],
                    "last_event_id": "E8",
                }

        broker = FakeBroker()
        with patch("hsp.cli._open_cli_broker", return_value=broker):
            out = self._run_with_env(["watch", "--global", "--once"], {"HSP_BROKER": "auto"})

        self.assertEqual(broker.method, "bus.recent_all")
        self.assertIn("scope=global", out)
        self.assertIn("/workspace/domain E8 hook.received PostToolUse Edit", out)

    def test_watch_uses_workgroup_observation_roots(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
                self.method = method
                self.params = params
                return {"events": [], "last_event_id": ""}

        broker = FakeBroker()
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            umbrella = Path(root)
            domain = umbrella / "domain"
            sibling = umbrella / "sibling"
            domain.mkdir()
            sibling.mkdir()
            (domain / "workgroup.toml").write_text(
                "[workgroup]\nname = 'domain'\nlevel = 'domain'\n"
                "[observe]\nmode = 'network'\nroots = ['../sibling']\n",
                encoding="utf-8",
            )
            with patch("hsp.cli._open_cli_broker", return_value=broker):
                out = self._run_with_env(
                    ["watch", "--once", str(domain)],
                    {
                        "HSP_BROKER": "auto",
                        "LSP_ROOT": root,
                        "HSP_WORKGROUP_BOUNDARY": root,
                    },
                )

        self.assertEqual(broker.method, "bus.recent_tree")
        self.assertEqual(
            broker.params["workspace_roots"],
            [str(domain.resolve()), str(sibling.resolve())],
        )
        self.assertIn("network", out)

    def test_global_command_reports_lsp_sources(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def connect(self) -> None:
                pass

            def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                if method != "lsp.status":
                    raise AssertionError(method)
                return {
                    "pid": 123,
                    "uptime": 4.0,
                    "idle_ttl_seconds": 300.0,
                    "bus": {"event_count": 2, "last_event_id": "E2", "open_question_count": 0},
                    "devtools": {"enabled": False, "running": False, "n_clients": 0},
                    "babel_bridge": {"enabled": False, "running": False},
                    "sessions": [
                        {
                            "session_id": "s1",
                            "root": "/workspace/domain/app",
                            "config_hash": "abc",
                            "client_count": 1,
                            "lsp": {
                                "route_id": "rust",
                                "language": "rust",
                                "route_reason": "extension",
                                "project_markers": ["Cargo.toml"],
                                "request_count": 3,
                                "last_method": "textDocument/definition",
                                "last_server_label": "rust-analyzer",
                                "last_duration_ms": 5,
                                "clients": [
                                    {
                                        "label": "rust-analyzer",
                                        "command": "rust-analyzer",
                                        "args": [],
                                        "state": "live",
                                        "pid": 456,
                                        "open_documents": 1,
                                        "request_count": 3,
                                        "folders": ["/workspace/domain/app"],
                                    }
                                ],
                            },
                        }
                    ],
                }

        with patch("hsp.cli._open_cli_broker", return_value=FakeBroker()):
            out = self._run_with_env(["global"], {"HSP_BROKER": "auto"})

        self.assertIn("global:", out)
        self.assertIn("sessions: 1", out)
        self.assertIn("source: route=rust language=rust reason=extension", out)
        self.assertIn("rust-analyzer live pid=456", out)

    def test_global_command_warns_about_split_tmp_broker(self) -> None:
        class FakeBroker:
            hsp_started = False

            def __enter__(self) -> FakeBroker:
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                pass

            def connect(self) -> None:
                pass

            def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                if method != "lsp.status":
                    raise AssertionError(method)
                return {"pid": 1, "uptime": 1.0, "idle_ttl_seconds": 1.0, "sessions": []}

        class SplitBroker(FakeBroker):
            def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                if method != "lsp.status":
                    raise AssertionError(method)
                return {"pid": 2, "sessions": [{"session_id": "s1"}]}

        with tempfile.TemporaryDirectory(dir="tmp") as root:
            runtime = Path(root) / "runtime"
            legacy_parent = Path(root) / "legacy"
            runtime.mkdir()
            legacy_parent.mkdir()
            legacy = legacy_parent / "hsp-broker.sock"
            legacy.touch()
            with patch("hsp.cli._broker_socket_path", return_value=runtime / "hsp-broker.sock"):
                with patch("hsp.cli._legacy_tmp_broker_socket_path", return_value=legacy):
                    with patch("hsp.cli._open_cli_broker", return_value=FakeBroker()):
                        with patch("hsp.cli._CliBrokerClient", return_value=SplitBroker()):
                            out = self._run_with_env(["global"], {"HSP_BROKER": "auto"})

        self.assertIn("split_broker_warning:", out)
        self.assertIn("reachable alternate socket:", out)
        self.assertIn("pid: 2 sessions: 1", out)

    def test_workgroup_command_counts_append_log_events(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            path = Path(root) / "tmp" / "hsp-bus.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"event_id": "E1", "kind": "note.posted"}) + "\n"
                + json.dumps({"event_id": "E2", "kind": "note.posted"}) + "\n",
                encoding="utf-8",
            )

            out = self._run(["workgroup", root], root=root)

        self.assertIn("append log:", out)
        self.assertIn("2 event(s), last=E2", out)

    def test_command_gate_detector_covers_common_checker_ecosystems(self) -> None:
        cases = [
            "cargo check",
            "cargo clippy --all-targets",
            "go test ./...",
            "go vet ./pkg",
            "uv run ruff check src",
            "python -m pytest tests/test_cli_log.py",
            "npm test",
            "pnpm run lint",
            "yarn build",
            "dotnet test",
            "rk test",
            "make test",
            "just lint",
            "mvn test",
            "gradle check",
            "eslint src/hsp",
            "npx eslint src/hsp",
            "biome check src",
            "prettier --check src/hsp/cli.py",
            "shellcheck scripts/hsp.sh",
            "deno lint src",
            "bun test src",
            "tox",
            "nox",
            "spaceship build",
            "spaceship check",
            "spaceship upgrade 42",
        ]

        for command in cases:
            with self.subTest(command=command):
                self.assertTrue(hsp_cli._is_build_command(command))

    def test_command_gate_detector_marks_cargo_check_as_workspace_wide(self) -> None:
        spec = hsp_cli._command_gate_spec("cargo check")

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertTrue(spec.full_workspace)
        self.assertEqual(spec.files, ())

    def test_command_gate_detector_extracts_checker_paths(self) -> None:
        spec = hsp_cli._command_gate_spec("ruff check src/hsp/cli.py")

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertFalse(spec.full_workspace)
        self.assertEqual(spec.files, ("src/hsp/cli.py",))

    def test_command_gate_detector_marks_dot_scopes_as_workspace_wide(self) -> None:
        for command in ("ruff check .", "go test ./..."):
            with self.subTest(command=command):
                spec = hsp_cli._command_gate_spec(command)

                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertTrue(spec.full_workspace)
                self.assertEqual(spec.files, ())

    def test_log_hook_requires_kind(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch.dict(
                os.environ,
                {"HSP_BROKER": "off", "LSP_ROOT": root, "HSP_WORKGROUP_ROOT": root},
                clear=False,
            ):
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

        self.assertRegex(out, r"logged E1 \d{2}:\d{2}:\d{2} edit\.after")
        self.assertEqual(event["kind"], "edit.after")
        self.assertEqual(scope["files"], ["src/server.py"])
        self.assertEqual(scope["symbols"], ["lsp_log"])
        self.assertEqual(metadata["status"], "done")
        self.assertEqual(metadata["targets"], "['tests/test_cli_log.py']")
        self.assertEqual(metadata["commit"], "abc1234")

    def test_log_ask_and_reply_round_trip_on_one_local_bus(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            self._run([
                "log",
                "ticket",
                "--message",
                "editing server",
            ], root=root)
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

    def test_run_waits_for_gate_executes_command_and_records_result(self) -> None:
        completed = subprocess.CompletedProcess(["python", "-m", "tests"], 0)
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch("hsp.cli.subprocess.run", return_value=completed) as run:
                out = self._run(["run", "--", "python", "-m", "tests"], root=root)
            event = self._read_last_event(root)
            metadata = self._require_dict(event, "metadata")

        self.assertEqual(out, "")
        run.assert_called_once_with(["python", "-m", "tests"], check=False)
        self.assertEqual(event["kind"], "test.ran")
        self.assertEqual(event["message"], "python -m tests")
        self.assertEqual(metadata["status"], "passed")

    def test_run_records_failed_command_status_and_returns_exit_code(self) -> None:
        completed = subprocess.CompletedProcess(["cargo", "test"], 101)
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            with patch("hsp.cli.subprocess.run", return_value=completed):
                code, _out, _err = self._run_code(["run", "--", "cargo", "test"], root=root)
            event = self._read_last_event(root)
            metadata = self._require_dict(event, "metadata")

        self.assertEqual(code, 101)
        self.assertEqual(event["kind"], "test.ran")
        self.assertEqual(metadata["status"], "failed")

    def test_run_timeout_does_not_execute_command_or_write_result(self) -> None:
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "other-agent",
                "message": "editing shared state",
            })
            with patch("hsp.cli.subprocess.run") as run:
                code, _out, err = self._run_code([
                    "run",
                    "--timeout",
                    "1ms",
                    "--",
                    "cargo",
                    "test",
                ], root=root)
            event = self._read_last_event(root)

        self.assertEqual(code, 124)
        self.assertIn("build gate timed out", err)
        run.assert_not_called()
        self.assertEqual(event["kind"], "ticket.started")

    def test_hook_kind_aliases_normalize_to_canonical_kind(self) -> None:
        self.assertIs(BusEventKind.from_wire("test.result"), BusEventKind.TEST)
        self.assertIs(BusEventKind.from_wire("lsp_confirm.after"), BusEventKind.CONFIRM_AFTER)
        self.assertIs(BusEventKind.from_wire("git.push"), BusEventKind.PUSH_AFTER)

    def test_bundled_hook_command_can_be_disabled_by_env(self) -> None:
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

    def test_bundled_hook_records_by_default_when_env_unset(self) -> None:
        payload = json.dumps({
            "hookEventName": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "--kind", "edit.after"], root=root, stdin=payload, enabled=None)
            event = self._read_last_event(root)

        self.assertEqual(out, "")
        self.assertEqual(event["kind"], "edit.after")
        self.assertEqual(event["message"], "PostToolUse Edit")

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

    def test_bundled_hook_stdin_form_records_harness_payload(self) -> None:
        payload = json.dumps({
            "hookEventName": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "stdin", "edit.after"], root=root, stdin=payload, enabled=None)
            event = self._read_last_event(root)

        self.assertEqual(out, "")
        self.assertEqual(event["kind"], "edit.after")
        self.assertEqual(event["message"], "PostToolUse Edit")

    def test_read_before_hook_injects_file_scoped_context(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.note({
                "workspace_root": root,
                "agent_id": "agent-a",
                "message": "changed renderer contract",
                "files": ["src/hsp/server.py"],
            })
            out = self._run_hook(["hook", "--kind", "tool.before"], root=root, stdin=payload, enabled=True)
            event = self._read_last_event(root)

        self.assertIn("hsp context for src/hsp/server.py:", out)
        self.assertIn("recent: 1", out)
        self.assertIn("note.posted changed renderer contract @agent-a", out)
        self.assertEqual(event["kind"], "tool.before")

    def test_edit_before_hook_injects_active_ticket_context(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "agent-a",
                "message": "refactor server bus rendering",
                "files": ["src/hsp/server.py"],
            })
            out = self._run_hook(["hook", "--kind", "edit.before"], root=root, stdin=payload, enabled=True)
            event = self._read_last_event(root)

        self.assertIn("hsp context for src/hsp/server.py:", out)
        self.assertIn("tickets: 1", out)
        self.assertIn("T1 refactor server bus rendering [agent-a]", out)
        self.assertEqual(event["kind"], "edit.before")

    def test_hook_context_can_be_disabled(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.note({
                "workspace_root": root,
                "agent_id": "agent-a",
                "message": "quiet note",
                "files": ["src/hsp/server.py"],
            })
            out = self._run_hook(
                ["hook", "--kind", "tool.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={"HSP_HOOK_CONTEXT": "0"},
            )

        self.assertEqual(out, "")

    def test_build_before_hook_waits_at_gate_without_writing_board_event(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cargo test"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "--kind", "tool.before"], root=root, stdin=payload, enabled=True)
            path = Path(root) / "tmp" / "hsp-bus.jsonl"

        self.assertEqual(out, "")
        self.assertFalse(path.exists())

    def test_build_before_hook_recognizes_cargo_check(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cargo check"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "--kind", "tool.before"], root=root, stdin=payload, enabled=True)

        self.assertEqual(out, "")

    def test_scoped_checker_hook_does_not_wait_on_unrelated_ticket(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ruff check src"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            Path(root, "src").mkdir()
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "other-agent",
                "message": "docs edit",
                "files": ["docs/readme.md"],
            })
            code, out, err = self._run_hook_code(
                ["hook", "--kind", "tool.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={"HSP_BUILD_GATE_TIMEOUT": "1ms"},
            )

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_build_before_hook_timeout_blocks_command_without_new_board_event(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cargo test"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "other-agent",
                "message": "editing shared state",
            })
            code, _out, err = self._run_hook_code(
                ["hook", "--kind", "tool.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={"HSP_BUILD_GATE_TIMEOUT": "1ms"},
            )
            event = self._read_last_event(root)

        self.assertEqual(code, 124)
        self.assertIn("build gate timed out", err)
        self.assertEqual(event["kind"], "ticket.started")

    def test_scoped_checker_hook_waits_on_overlapping_ticket(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ruff check src"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            Path(root, "src").mkdir()
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "other-agent",
                "message": "src edit",
                "files": ["src/hsp/server.py"],
            })
            code, _out, err = self._run_hook_code(
                ["hook", "--kind", "tool.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={"HSP_BUILD_GATE_TIMEOUT": "1ms"},
            )

        self.assertEqual(code, 124)
        self.assertIn("build gate timed out", err)
        self.assertIn("scope: src", err)

    def test_all_waiting_build_hook_runs_once_and_denies_original_bash(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cargo check"},
        })
        completed = subprocess.CompletedProcess("cargo check", 0, stdout="finished\n", stderr="")
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            Path(root, "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "agent-a",
                "message": "edit a",
                "project_roots": [root],
            })
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "agent-b",
                "message": "edit b",
                "project_roots": [root],
            })
            server._local_bus.build_gate({"workspace_root": root, "agent_id": "agent-b", "project_roots": [root]})
            with patch("hsp.cli.subprocess.run", return_value=completed) as run:
                code, out, err = self._run_hook_code(
                    ["hook", "--kind", "tool.before"],
                    root=root,
                    stdin=payload,
                    enabled=True,
                    extra_env={
                        "HSP_AGENT_ID": "agent-a",
                        "HSP_BUILD_GATE_TIMEOUT": "1ms",
                    },
                )
            event = self._read_last_event(root)
            metadata = self._require_dict(event, "metadata")
            denial = json.loads(out)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run.assert_called_once_with(
            "cargo check",
            shell=True,
            cwd=str(Path(root).resolve()),
            check=False,
            capture_output=True,
            text=True,
        )
        hook_output = self._require_dict(denial, "hookSpecificOutput")
        self.assertEqual(hook_output["permissionDecision"], "deny")
        reason = str(hook_output["permissionDecisionReason"])
        self.assertIn("ran this command once", reason)
        self.assertIn("finished", reason)
        self.assertEqual(event["kind"], "test.ran")
        self.assertEqual(event["message"], "cargo check")
        self.assertEqual(metadata["status"], "passed")

    def test_all_waiting_build_hook_reuses_batched_result_for_second_agent(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cargo check"},
        })
        completed = subprocess.CompletedProcess("cargo check", 0, stdout="shared\n", stderr="")
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            Path(root, "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "agent-a",
                "message": "edit a",
                "project_roots": [root],
            })
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "agent-b",
                "message": "edit b",
                "project_roots": [root],
            })
            server._local_bus.build_gate({"workspace_root": root, "agent_id": "agent-b", "project_roots": [root]})
            with patch("hsp.cli.subprocess.run", return_value=completed):
                first_code, first_out, _first_err = self._run_hook_code(
                    ["hook", "--kind", "tool.before"],
                    root=root,
                    stdin=payload,
                    enabled=True,
                    extra_env={
                        "HSP_AGENT_ID": "agent-a",
                        "HSP_BUILD_GATE_TIMEOUT": "1ms",
                    },
                )
            with patch("hsp.cli.subprocess.run") as run:
                second_code, second_out, second_err = self._run_hook_code(
                    ["hook", "--kind", "tool.before"],
                    root=root,
                    stdin=payload,
                    enabled=True,
                    extra_env={
                        "HSP_AGENT_ID": "agent-b",
                        "HSP_BUILD_GATE_TIMEOUT": "1ms",
                    },
                )

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        run.assert_not_called()
        first_reason = str(self._require_dict(json.loads(first_out), "hookSpecificOutput")["permissionDecisionReason"])
        second_reason = str(self._require_dict(json.loads(second_out), "hookSpecificOutput")["permissionDecisionReason"])
        self.assertIn("ran this command once", first_reason)
        self.assertIn("reused the batched result", second_reason)
        self.assertIn("shared", second_reason)

    def test_build_after_hook_records_test_result(self) -> None:
        payload = json.dumps({
            "hookEventName": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "uv run python -m unittest"},
            "tool_response": {"success": True},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            out = self._run_hook(["hook", "--kind", "tool.after"], root=root, stdin=payload, enabled=True)
            event = self._read_last_event(root)
            metadata = self._require_dict(event, "metadata")

        self.assertEqual(out, "")
        self.assertEqual(event["kind"], "test.ran")
        self.assertEqual(event["message"], "uv run python -m unittest")
        self.assertEqual(metadata["status"], "passed")

    def test_edit_before_hook_denies_without_ticket_when_policy_enabled(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            code, out, err = self._run_hook_code(
                ["hook", "--kind", "edit.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={"HSP_REQUIRE_TICKET_FOR_EDITS": "1"},
            )
            path = Path(root) / "tmp" / "hsp-bus.jsonl"
            denial = json.loads(out)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(path.exists())
        hook_output = self._require_dict(denial, "hookSpecificOutput")
        self.assertEqual(hook_output["permissionDecision"], "deny")
        self.assertIn("no active ticket", str(hook_output["permissionDecisionReason"]))

    def test_edit_before_hook_allows_with_workgroup_ticket(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "other-agent",
                "message": "editing shared state",
            })
            out = self._run_hook(
                ["hook", "--kind", "edit.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={"HSP_REQUIRE_TICKET_FOR_EDITS": "1"},
            )
            event = self._read_last_event(root)

        self.assertIn("hsp context for src/hsp/server.py:", out)
        self.assertIn("T1 editing shared state [other-agent]", out)
        self.assertEqual(event["kind"], "edit.before")

    def test_edit_before_hook_agent_scope_requires_matching_agent_id(self) -> None:
        payload = json.dumps({
            "hookEventName": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/hsp/server.py"},
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            server._local_bus = AgentBus()
            server._local_bus.ticket({
                "workspace_root": root,
                "agent_id": "agent-a",
                "message": "editing shared state",
            })
            code, out, _err = self._run_hook_code(
                ["hook", "--kind", "edit.before"],
                root=root,
                stdin=payload,
                enabled=True,
                extra_env={
                    "HSP_REQUIRE_TICKET_FOR_EDITS": "1",
                    "HSP_EDIT_GATE_SCOPE": "agent",
                    "HSP_AGENT_ID": "agent-b",
                },
            )
            denial = json.loads(out)

        self.assertEqual(code, 0)
        hook_output = self._require_dict(denial, "hookSpecificOutput")
        self.assertEqual(hook_output["permissionDecision"], "deny")

    def test_prompt_end_command_records_session_stop(self) -> None:
        payload = json.dumps({
            "hookEventName": "UserPromptSubmit",
            "prompt": ".end",
        })
        with tempfile.TemporaryDirectory(dir="tmp") as root:
            self._run_hook(["hook", "--kind", "prompt"], root=root, stdin=payload, enabled=True)
            event = self._read_last_event(root)

        self.assertEqual(event["kind"], "session.stop")
        self.assertEqual(event["message"], ".end")

    def test_claude_plugin_bundles_default_on_bus_hooks(self) -> None:
        data = json.loads(Path(".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"], "./hooks/claude.json")
        hooks = json.loads(Path("hooks/claude.json").read_text(encoding="utf-8"))["hooks"]
        self.assertIn("SessionStart", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("Stop", hooks)
        self.assertIn("Notification", hooks)
        self.assertIn("SubagentStop", hooks)
        self.assertIn("SubagentStart", hooks)
        self.assertIn("PreCompact", hooks)
        self.assertIn("PostCompact", hooks)
        self.assertIn("SessionEnd", hooks)
        self.assertIn("StopFailure", hooks)
        self.assertIn("PermissionRequest", hooks)
        self.assertIn("PreToolUse", hooks)
        self.assertIn("PostToolUse", hooks)
        commands = "\n".join(_plugin_hook_commands(hooks))
        self.assertIn("hsp hook stdin session.start", commands)
        self.assertIn("hsp hook stdin session.end", commands)
        self.assertIn("hsp hook stdin prompt", commands)
        self.assertIn("hsp hook stdin session.stop", commands)
        self.assertIn("hsp hook stdin notification", commands)
        self.assertIn("hsp hook stdin subagent.stop", commands)
        self.assertIn("hsp hook stdin subagent.start", commands)
        self.assertIn("hsp hook stdin compact.before", commands)
        self.assertIn("hsp hook stdin compact.after", commands)
        self.assertIn("hsp hook stdin stop.failure", commands)
        self.assertIn("hsp hook stdin permission.request", commands)
        self.assertIn("hsp hook stdin tool.before", commands)
        self.assertIn("hsp hook stdin tool.after", commands)
        self.assertIn("hsp hook stdin edit.before", commands)
        self.assertIn("hsp hook stdin edit.after", commands)
        self.assertNotIn("sh -c", commands)
        self.assertNotIn("cat >/dev/null", commands)
        self.assertNotIn("hsp-hook", commands)

    def _run(self, argv: list[str], *, root: str) -> str:
        code, out, _err = self._run_code(argv, root=root)
        self.assertEqual(code, 0)
        return out

    def _run_with_env(self, argv: list[str], env: dict[str, str]) -> str:
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stdout(out):
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        hsp_main(argv)
        code = cm.exception.code
        self.assertEqual(code if isinstance(code, int) else int(code or 0), 0, err.getvalue())
        return out.getvalue()

    def _run_code(self, argv: list[str], *, root: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(
            os.environ,
            {"HSP_BROKER": "off", "LSP_ROOT": root, "HSP_WORKGROUP_ROOT": root},
            clear=False,
        ):
            with contextlib.redirect_stdout(out):
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        hsp_main(argv)
        code = cm.exception.code
        return code if isinstance(code, int) else int(code or 0), out.getvalue(), err.getvalue()

    def _run_hook(
        self,
        argv: list[str],
        *,
        root: str,
        stdin: str,
        enabled: bool | None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        code, out, _err = self._run_hook_code(
            argv,
            root=root,
            stdin=stdin,
            enabled=enabled,
            extra_env=extra_env,
        )
        self.assertEqual(code, 0)
        return out

    def _run_hook_code(
        self,
        argv: list[str],
        *,
        root: str,
        stdin: str,
        enabled: bool | None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        env = {
            "HSP_BROKER": "off",
            "LSP_ROOT": root,
            "HSP_WORKGROUP_ROOT": root,
        }
        if enabled is not None:
            env["HSP_HOOKS"] = "1" if enabled else "0"
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env, clear=False):
            with patch("sys.stdin", io.StringIO(stdin)):
                with contextlib.redirect_stdout(out):
                    with contextlib.redirect_stderr(err):
                        with self.assertRaises(SystemExit) as cm:
                            hsp_main(argv)
        code = cm.exception.code
        return code if isinstance(code, int) else int(code or 0), out.getvalue(), err.getvalue()

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
