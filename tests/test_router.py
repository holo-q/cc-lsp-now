from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from hsp import server
from hsp.lsp import file_uri
from hsp.router import BUILTIN_ROUTES, find_project_root, resolve_route_id_for_path


class HspRouterResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1] / "tmp" / "test_router"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        server._bind_route_runtime("legacy")

    def test_extension_selects_builtin_language_route(self) -> None:
        self.assertEqual(resolve_route_id_for_path(str(self.root / "pkg" / "model.py")), "python")
        self.assertEqual(resolve_route_id_for_path(str(self.root / "src" / "Program.cs")), "csharp")
        self.assertEqual(resolve_route_id_for_path(str(self.root / "src" / "lib.rs")), "rust")

    def test_marker_globs_detect_csharp_project_roots(self) -> None:
        project = self.root / "dotnet"
        src = project / "src"
        src.mkdir(parents=True)
        (project / "Harness.csproj").write_text("<Project />", encoding="utf-8")
        target = src / "Worker.txt"
        target.write_text("", encoding="utf-8")

        self.assertEqual(find_project_root(str(target), ("*.csproj",)), str(project))

        with patch.dict("os.environ", {"LSP_PROJECT_MARKERS": "*.csproj"}, clear=False):
            server._bind_route_runtime("legacy")
            self.assertEqual(server._find_project_root(str(target)), str(project))

    def test_rust_markers_detect_cargo_project_roots(self) -> None:
        project = self.root / "rust"
        src = project / "src"
        src.mkdir(parents=True)
        (project / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
        target = src / "query.txt"
        target.write_text("", encoding="utf-8")

        self.assertEqual(find_project_root(str(target), ("Cargo.toml", "rust-project.json")), str(project))
        self.assertEqual(resolve_route_id_for_path(str(target)), "rust")

    def test_rust_route_excludes_project_tmp_from_warmup(self) -> None:
        exclude = BUILTIN_ROUTES["rust"].env["LSP_WARMUP_EXCLUDE"].split(",")
        self.assertIn("references", exclude)
        self.assertIn("tmp", exclude)

    def test_generic_git_marker_does_not_make_workspace_route_ambiguous(self) -> None:
        project = self.root / "python"
        package = project / "pkg"
        package.mkdir(parents=True)
        (project / ".git").mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

        self.assertEqual(resolve_route_id_for_path(str(package)), "python")

    def test_router_sends_python_csharp_and_rust_to_separate_chains(self) -> None:
        with patch.dict("os.environ", {"HSP_ROUTER": "1"}, clear=True):
            py_uri = file_uri(str(self.root / "pkg" / "model.py"))
            cs_uri = file_uri(str(self.root / "src" / "Program.cs"))
            rs_uri = file_uri(str(self.root / "src" / "lib.rs"))

            server._activate_route_for_uri(py_uri)
            python_chain = server._ensure_chain_configs()
            self.assertEqual([cfg.command for cfg in python_chain], ["ty", "basedpyright-langserver"])
            self.assertEqual(server._method_handler["workspace/willRenameFiles"], 1)

            server._activate_route_for_uri(cs_uri)
            csharp_chain = server._ensure_chain_configs()
            self.assertEqual([cfg.command for cfg in csharp_chain], ["csharp-ls"])
            self.assertNotIn("workspace/willRenameFiles", server._method_handler)

            server._activate_route_for_uri(rs_uri)
            rust_chain = server._ensure_chain_configs()
            self.assertEqual([cfg.command for cfg in rust_chain], ["rust-analyzer"])
            self.assertNotIn("workspace/willRenameFiles", server._method_handler)

            server._activate_route_for_uri(py_uri)
            self.assertIs(server._ensure_chain_configs(), python_chain)
            self.assertEqual(server._method_handler["workspace/willRenameFiles"], 1)

    def test_explicit_lsp_servers_keep_legacy_single_chain_mode(self) -> None:
        with patch.dict("os.environ", {"HSP_ROUTER": "1", "LSP_SERVERS": "fake-ls"}, clear=True):
            route_id = server._select_route_id_for_uri(file_uri(str(self.root / "pkg" / "model.py")))
            self.assertEqual(route_id, "legacy")
