from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path

from hsp import server


RUN_CSHARP_FIXTURE = os.environ.get("HSP_RUN_CSHARP_FIXTURE") == "1"
DEFAULT_FIXTURE_ROOT = Path("tmp/csharp-fixtures/Dapper")
SMOKE_PROJECT_ROOT = Path("tmp/csharp-fixtures/dapper-smoke")
SMOKE_HANDLER_SOURCE = """using Dapper;

namespace Dapper.Smoke;

public readonly record struct SmokeValue(string Value);

public sealed class SmokeStringHandler : SqlMapper.StringTypeHandler<SmokeValue>
{
    protected override SmokeValue Parse(string xml) => new(xml);

    protected override string Format(SmokeValue xml) => xml.Value;
}
"""


def _fixture_root() -> Path:
    return Path(os.environ.get("HSP_CSHARP_FIXTURE_ROOT", str(DEFAULT_FIXTURE_ROOT))).resolve()


def _smoke_root() -> Path:
    return SMOKE_PROJECT_ROOT.resolve()


def _reset_lsp_state() -> None:
    server._chain_configs.clear()
    server._chain_clients.clear()
    server._method_handler.clear()
    server._added_workspaces_this_call.clear()
    server._pending_workspace_adds.clear()
    server._just_started_this_call.clear()
    server._warmed_folders.clear()
    server._folder_warmup_stats.clear()
    server._clear_pending()
    server._last_semantic_nav.clear()
    server._last_semantic_groups.clear()
    server._last_semantic_nav_query = ""
    server._last_server = ""


async def _stop_lsp_clients() -> None:
    for client in list(server._chain_clients):
        if client is not None:
            await client.stop()


def _write_file(path: Path, text: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8")


def _build_smoke_project(dapper_root: Path, smoke_root: Path) -> None:
    source_root = dapper_root / "Dapper"
    if not (source_root / "SqlMapper.TypeHandler.cs").is_file():
        raise FileNotFoundError(source_root / "SqlMapper.TypeHandler.cs")

    smoke_root.mkdir(parents=True, exist_ok=True)
    dapper_smoke_root = smoke_root / "Dapper"
    dapper_smoke_root.mkdir(parents=True, exist_ok=True)

    for name in ("SqlMapper.ITypeHandler.cs", "SqlMapper.TypeHandler.cs"):
        shutil.copyfile(source_root / name, dapper_smoke_root / name)

    _write_file(
        smoke_root / "DapperSmoke.csproj",
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>
""",
    )
    _write_file(smoke_root / "SmokeStringHandler.cs", SMOKE_HANDLER_SOURCE)


class CSharpDapperIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Opt-in smoke tests against Dapper-backed C# project state.

    The suite clones a real upstream project, then derives a small ``net10.0``
    project from selected Dapper source files. That keeps Roslyn on real
    symbols while avoiding full-solution target-pack noise from Dapper's
    broader CI matrix.
    """

    def setUp(self) -> None:
        if not RUN_CSHARP_FIXTURE:
            self.skipTest("set HSP_RUN_CSHARP_FIXTURE=1 to run csharp-ls fixture tests")
        if shutil.which("csharp-ls") is None:
            self.skipTest("csharp-ls is not on PATH")
        dapper_root = _fixture_root()
        if not (dapper_root / "Dapper.sln").is_file():
            self.skipTest(
                "Dapper fixture missing; clone with "
                "`git clone --depth 1 https://github.com/DapperLib/Dapper.git "
                "tmp/csharp-fixtures/Dapper`"
            )

        self.root = _smoke_root()
        _build_smoke_project(dapper_root, self.root)
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "LSP_COMMAND",
                "LSP_ARGS",
                "LSP_ROOT",
                "LSP_PROJECT_MARKERS",
                "LSP_WARMUP_PATTERNS",
                "LSP_WARMUP_MAX_FILES",
                "LSP_WARMUP_EXCLUDE",
                "LSP_EMPTY_FALLBACK",
            )
        }
        os.environ.update({
            "LSP_COMMAND": "csharp-ls",
            "LSP_ARGS": "",
            "LSP_ROOT": str(self.root),
            "LSP_PROJECT_MARKERS": "DapperSmoke.csproj,.csproj,.git",
            "LSP_WARMUP_PATTERNS": ",".join((
                "Dapper/*.cs",
                "SmokeStringHandler.cs",
            )),
            "LSP_WARMUP_MAX_FILES": "20",
            "LSP_WARMUP_EXCLUDE": "bin,obj,artifacts,TestResults",
            "LSP_EMPTY_FALLBACK": "textDocument/references,workspace/symbol",
        })
        _reset_lsp_state()

    async def asyncTearDown(self) -> None:
        if hasattr(self, "_saved_env"):
            await _stop_lsp_clients()
            _reset_lsp_state()
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def fixture_file(self, *parts: str) -> str:
        return str(self.root.joinpath(*parts))

    async def eventually(
        self,
        label: str,
        call: Callable[[], Awaitable[str]],
        accept: Callable[[str], bool],
        attempts: int = 8,
        delay: float = 2.0,
    ) -> str:
        last = ""
        for attempt in range(attempts):
            last = await call()
            if accept(last):
                return last
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        self.fail(f"{label} did not stabilize after {attempts} attempt(s):\n{last}")
        raise AssertionError("unreachable")

    async def test_outline_resolves_dapper_type_handler_source(self) -> None:
        output = await self.eventually(
            "SqlMapper.TypeHandler outline",
            lambda: server.lsp_outline(file_path=self.fixture_file("Dapper", "SqlMapper.TypeHandler.cs")),
            lambda text: "Class TypeHandler<T>" in text,
        )

        self.assertIn("Class TypeHandler<T>", output)
        self.assertIn("Class StringTypeHandler<T>", output)
        self.assertIn("Method SetValue", output)
        self.assertIn("Method Parse", output)

    async def test_semantic_grep_and_symbols_at_cover_method_args(self) -> None:
        grep_output = await self.eventually(
            "SmokeStringHandler semantic grep",
            lambda: server.lsp_grep(
                query="Parse",
                file_path=self.fixture_file("SmokeStringHandler.cs"),
                max_hits=20,
                max_groups=8,
            ),
            lambda text: "SmokeStringHandler.cs" in text and "samples " in text,
        )
        symbols_output = await server.lsp_symbols_at(target="SmokeStringHandler.cs:L9")

        self.assertIn("SmokeStringHandler.cs", grep_output)
        self.assertIn("refs ", grep_output)
        self.assertIn("samples ", grep_output)
        self.assertIn("symbol Parse", symbols_output)
        self.assertIn("symbol xml", symbols_output)
        self.assertIn("SmokeValue", symbols_output)

    async def test_type_hierarchy_finds_dapper_base_type(self) -> None:
        output = await self.eventually(
            "SmokeStringHandler type hierarchy",
            lambda: server.lsp_types(
                file_path="SmokeStringHandler.cs",
                line=7,
                direction="super",
                max_depth=3,
                max_edges=12,
            ),
            lambda text: "Types for" in text and "No type hierarchy item" not in text,
        )

        self.assertIn("Types for SmokeStringHandler", output)
        self.assertIn("super:", output)
        self.assertIn("StringTypeHandler", output)

    async def test_rename_preview_stages_edits_without_touching_fixture(self) -> None:
        path = Path(self.fixture_file("SmokeStringHandler.cs"))
        before = path.read_text(encoding="utf-8")

        output = await self.eventually(
            "SmokeStringHandler rename preview",
            lambda: server.lsp_rename(
                file_path=str(path),
                line=7,
                symbol="SmokeStringHandler",
                new_name="SmokeStringHandlerProbe",
            ),
            lambda text: "Preview:" in text,
        )
        after = path.read_text(encoding="utf-8")

        self.assertEqual(before, after)
        self.assertIn("Preview:", output)
        self.assertIn("SmokeStringHandlerProbe", output)
        self.assertIsNotNone(server._pending)


if __name__ == "__main__":
    unittest.main()
