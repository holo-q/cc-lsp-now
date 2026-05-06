from __future__ import annotations

import asyncio
import shutil
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from hsp import server
from hsp.alias_coordinator import alias_identity_to_wire
from hsp.broker import BrokerDaemon
from hsp.broker_lsp import BrokerLspManager, BrokerLspSession, chain_config_hash, chain_from_wire, chain_to_wire
from hsp.broker_session import SessionRegistry
from hsp.chain_server import ChainServer
from hsp.lsp import LspClient
from hsp.render_memory import AliasIdentity, AliasKind


class FakeLspClient:
    def __init__(self, command: list[str], root: str, result: Any = None) -> None:
        self.command = command
        self.root = root
        self.result = result if result is not None else {"ok": True}
        self.started = 0
        self.requests: list[tuple[str, dict | None, float]] = []
        self.ensured: list[str] = []
        self.resynced = 0
        self.workspace_folders: set[str] = {root}
        self.capabilities: dict[str, object] = {"definitionProvider": True}
        self.diagnostics: dict[str, list] = {}
        self._open_documents: dict[str, int] = {}

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        pass

    async def request(self, method: str, params: dict | None, *, timeout: float = 30.0) -> Any:
        self.requests.append((method, params, timeout))
        return self.result

    async def ensure_document(self, uri: str) -> None:
        self.ensured.append(uri)
        self._open_documents[uri] = self._open_documents.get(uri, -1) + 1

    async def resync_open_documents(self) -> int:
        self.resynced += 1
        return 0

    def add_workspace_folder(self, folder_path: str) -> bool:
        if folder_path in self.workspace_folders:
            return False
        self.workspace_folders.add(folder_path)
        return True

    def notify_files_renamed(self, _renames: list[tuple[str, str]]) -> None:
        pass

    def notify_files_created(self, _paths: list[str]) -> None:
        pass

    def notify_files_deleted(self, _paths: list[str]) -> None:
        pass


class BrokerLspSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_starts_client_once_and_caches_method_handler(self) -> None:
        clients: list[FakeLspClient] = []

        def factory(command: list[str], root: str) -> LspClient:
            client = FakeLspClient(command, root)
            clients.append(client)
            return cast(LspClient, client)

        chain = [ChainServer(command="fake-ls", args=["--stdio"], name="fake", label="fake")]
        session = BrokerLspSession("/repo", chain, client_factory=factory)

        first = await session.request(
            "textDocument/definition",
            {"x": 1},
            uri="file:///repo/a.cs",
            empty_fallback_methods=set(),
        )
        second = await session.request(
            "textDocument/definition",
            {"x": 2},
            uri="file:///repo/a.cs",
            empty_fallback_methods=set(),
        )

        self.assertEqual(first.to_wire()["server_label"], "fake")
        self.assertEqual(second.to_wire()["result"], {"ok": True})
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].started, 1)
        self.assertEqual([r[0] for r in clients[0].requests], ["textDocument/definition", "textDocument/definition"])
        self.assertEqual(session.method_handler["textDocument/definition"], 0)

    async def test_add_workspace_queues_before_spawn_then_flushes_on_start(self) -> None:
        clients: list[FakeLspClient] = []

        def factory(command: list[str], root: str) -> LspClient:
            client = FakeLspClient(command, root)
            clients.append(client)
            return cast(LspClient, client)

        chain = [ChainServer(command="fake-ls", args=[], name="fake", label="fake")]
        session = BrokerLspSession("/repo", chain, client_factory=factory)

        await session.add_workspace("/repo/sub")
        await session.request("workspace/symbol", {}, uri=None, empty_fallback_methods=set())

        self.assertEqual(clients[0].workspace_folders, {"/repo", "/repo/sub"})

    async def test_status_reports_load_bearing_runtime_counters(self) -> None:
        def factory(command: list[str], root: str) -> LspClient:
            return cast(LspClient, FakeLspClient(command, root))

        chain = [ChainServer(command="fake-ls", args=[], name="fake", label="fake")]
        session = BrokerLspSession("/repo", chain, client_factory=factory)

        await session.request("textDocument/definition", {}, uri="file:///repo/a.cs", empty_fallback_methods=set())

        status = session.status()
        self.assertEqual(status["request_count"], 1)
        self.assertEqual(status["last_method"], "textDocument/definition")
        self.assertEqual(status["last_server_label"], "fake")
        client = cast(list[dict[str, object]], status["clients"])[0]
        self.assertEqual(client["state"], "live")
        self.assertEqual(client["open_documents"], 1)
        self.assertEqual(client["request_count"], 1)

    async def test_render_touch_tracks_per_client_frontiers(self) -> None:
        chain = [ChainServer(command="fake-ls", args=[], name="fake", label="fake")]
        session = BrokerLspSession("/repo", chain)
        identity = AliasIdentity(
            kind=AliasKind.SYMBOL,
            name="ctx",
            path="/repo/src/Renderer.cs",
            line=44,
            character=12,
            symbol_kind="arg",
            bucket_key="Renderer",
            bucket_label="Renderer.cs::Renderer",
        )

        first = await session.render_touch("agent-a", [identity])
        second = await session.render_touch("agent-a", [identity])
        third = await session.render_touch("agent-b", [identity])

        self.assertEqual(first.records[0].alias, "A1")
        self.assertFalse(second.decisions[0].introduced)
        self.assertEqual(second.legend, "")
        self.assertEqual(third.records[0].alias, "A1")
        self.assertTrue(third.decisions[0].introduced)


class BrokerLspManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_matching_uses_root_and_config_hash(self) -> None:
        registry = SessionRegistry()
        manager = BrokerLspManager(registry)
        chain = [ChainServer(command="fake-ls", args=[], name="fake", label="fake")]
        sid, _session = manager.get_or_create(
            root="/repo",
            config_hash_value="h1",
            chain=chain,
            server_label="fake",
        )
        manager.get_or_create(
            root="/other",
            config_hash_value="h1",
            chain=chain,
            server_label="fake",
        )

        stopped = await manager.stop_matching(root="/repo", config_hash_value="h1")

        self.assertEqual(stopped, [sid])
        self.assertEqual(len(registry), 1)

    async def test_idle_eviction_stops_sessions_past_ttl(self) -> None:
        registry = SessionRegistry()
        manager = BrokerLspManager(registry)
        chain = [ChainServer(command="fake-ls", args=[], name="fake", label="fake")]
        old_sid, old = manager.get_or_create(
            root="/old",
            config_hash_value="h",
            chain=chain,
            server_label="fake",
        )
        _new_sid, new = manager.get_or_create(
            root="/new",
            config_hash_value="h",
            chain=chain,
            server_label="fake",
        )
        old.last_used_at = 10.0
        new.last_used_at = 95.0

        evicted = await manager.evict_idle(ttl_seconds=50.0, now=100.0)

        self.assertEqual(evicted, [old_sid])
        self.assertEqual(len(registry), 1)


class BrokerDaemonLspForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_lsp_requests_share_one_broker_owned_client(self) -> None:
        clients: list[FakeLspClient] = []

        def factory(command: list[str], root: str) -> LspClient:
            client = FakeLspClient(command, root)
            clients.append(client)
            return cast(LspClient, client)

        daemon = BrokerDaemon()
        daemon.lsp = BrokerLspManager(daemon.registry, client_factory=factory)
        params: dict[str, object] = {
            "root": "/repo",
            "config_hash": "h1",
            "server_label": "fake",
            "chain": chain_to_wire([ChainServer(command="fake-ls", args=[], name="fake", label="fake")]),
            "lsp_method": "textDocument/definition",
            "lsp_params": {},
            "uri": "file:///repo/a.cs",
            "empty_fallback_methods": [],
        }

        first = await daemon.handle_request({"id": "1", "method": "lsp.request", "params": params})
        second = await daemon.handle_request({"id": "2", "method": "lsp.request", "params": params})
        status = await daemon.handle_request({"id": "3", "method": "lsp.status", "params": {}})

        self.assertIn("result", first)
        self.assertIn("result", second)
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].started, 1)
        result = cast(dict[str, object], status["result"])
        self.assertEqual(result["session_count"], 1)
        session = cast(list[dict[str, object]], result["sessions"])[0]
        lsp = cast(dict[str, object], session["lsp"])
        self.assertEqual(lsp["request_count"], 2)

    async def test_router_lsp_request_resolves_chain_inside_broker(self) -> None:
        clients: list[FakeLspClient] = []

        def factory(command: list[str], root: str) -> LspClient:
            client = FakeLspClient(command, root)
            clients.append(client)
            return cast(LspClient, client)

        daemon = BrokerDaemon()
        daemon.lsp = BrokerLspManager(daemon.registry, client_factory=factory)
        params: dict[str, object] = {
            "root": "/repo",
            "router": True,
            "lsp_method": "textDocument/definition",
            "lsp_params": {},
            "uri": "file:///repo/src/lib.rs",
            "empty_fallback_methods": [],
        }

        result = await daemon.handle_request({"id": "1", "method": "lsp.request", "params": params})
        status = await daemon.handle_request({"id": "2", "method": "lsp.status", "params": {}})

        self.assertIn("result", result)
        self.assertEqual(clients[0].command, ["rust-analyzer"])
        status_result = cast(dict[str, object], status["result"])
        session = cast(list[dict[str, object]], status_result["sessions"])[0]
        lsp = cast(dict[str, object], session["lsp"])
        self.assertEqual(lsp["route_id"], "rust")
        self.assertEqual(lsp["language"], "rust")

    async def test_router_lsp_request_uses_nearest_project_root(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tmp" / "test_broker_router"
        shutil.rmtree(root, ignore_errors=True)
        try:
            crate = root / "crates" / "demo"
            src = crate / "src"
            src.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname = 'outer'\n", encoding="utf-8")
            (crate / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
            target = src / "lib.rs"
            target.write_text("", encoding="utf-8")

            clients: list[FakeLspClient] = []

            def factory(command: list[str], root_path: str) -> LspClient:
                client = FakeLspClient(command, root_path)
                clients.append(client)
                return cast(LspClient, client)

            daemon = BrokerDaemon()
            daemon.lsp = BrokerLspManager(daemon.registry, client_factory=factory)
            params: dict[str, object] = {
                "root": str(root),
                "router": True,
                "lsp_method": "textDocument/definition",
                "lsp_params": {},
                "uri": target.resolve().as_uri(),
                "empty_fallback_methods": [],
            }

            await daemon.handle_request({"id": "1", "method": "lsp.request", "params": params})

            self.assertEqual(clients[0].command, ["rust-analyzer"])
            self.assertEqual(clients[0].root, str(crate))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    async def test_render_touch_wire_reuses_alias_but_reintroduces_per_client(self) -> None:
        daemon = BrokerDaemon()
        chain = chain_to_wire([ChainServer(command="fake-ls", args=[], name="fake", label="fake")])
        identity = AliasIdentity(
            kind=AliasKind.SYMBOL,
            name="ctx",
            path="/repo/src/Renderer.cs",
            line=44,
            character=12,
            symbol_kind="arg",
            bucket_key="Renderer",
            bucket_label="Renderer.cs::Renderer",
        )
        params: dict[str, object] = {
            "root": "/repo",
            "config_hash": "h1",
            "server_label": "fake",
            "chain": chain,
            "client_id": "agent-a",
            "identities": [alias_identity_to_wire(identity)],
        }

        first = await daemon.handle_request({"id": "1", "method": "render.touch", "params": params})
        second = await daemon.handle_request({"id": "2", "method": "render.touch", "params": params})
        params_b = dict(params)
        params_b["client_id"] = "agent-b"
        third = await daemon.handle_request({"id": "3", "method": "render.touch", "params": params_b})

        first_result = cast(dict[str, object], first["result"])
        second_result = cast(dict[str, object], second["result"])
        third_result = cast(dict[str, object], third["result"])
        first_decision = cast(list[dict[str, object]], first_result["decisions"])[0]
        second_decision = cast(list[dict[str, object]], second_result["decisions"])[0]
        third_decision = cast(list[dict[str, object]], third_result["decisions"])[0]
        first_record = cast(dict[str, object], first_decision["record"])
        third_record = cast(dict[str, object], third_decision["record"])

        self.assertEqual(first_record["alias"], "A1")
        self.assertEqual(third_record["alias"], "A1")
        self.assertTrue(first_decision["introduced"])
        self.assertFalse(second_decision["introduced"])
        self.assertTrue(third_decision["introduced"])
        self.assertIn("A1=ctx@L44", cast(str, first_result["legend"]))
        self.assertEqual(second_result["legend"], "")


class BrokerWireShapeTests(unittest.TestCase):
    def test_chain_roundtrip_and_hash_are_stable(self) -> None:
        chain = [ChainServer(command="csharp-ls", args=[], name="csharp-ls", label="csharp-ls")]
        wire = chain_to_wire(chain)

        restored = chain_from_wire(wire)

        self.assertEqual(restored, chain)
        self.assertEqual(chain_config_hash("csharp", chain), chain_config_hash("csharp", restored))


class ServerBrokerForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_chain = list(server._chain_configs)
        self.old_clients = list(server._chain_clients)
        self.old_handlers = dict(server._method_handler)

    def tearDown(self) -> None:
        server._chain_configs[:] = self.old_chain
        server._chain_clients[:] = self.old_clients
        server._method_handler.clear()
        server._method_handler.update(self.old_handlers)

    def test_request_uses_broker_and_does_not_spawn_local_client(self) -> None:
        async def fake_broker(_method: str, _params: dict | None, _uri: str | None) -> dict[str, object]:
            return {
                "result": {"answer": 42},
                "server_label": "brokered",
                "started": ["brokered"],
                "workspaces_added": ["/repo"],
            }

        async def explode_get_client(_idx: int) -> LspClient:
            raise AssertionError("direct LSP client should not be spawned")

        server._chain_configs.clear()
        server._chain_clients.clear()
        server._method_handler.clear()

        with patch.dict("os.environ", {"LSP_SERVERS": "fake-ls", "HSP_BROKER": "on"}, clear=False):
            with patch.object(server, "_broker_lsp_request", AsyncMock(side_effect=fake_broker)):
                with patch.object(server, "_get_client", AsyncMock(side_effect=explode_get_client)):
                    result = asyncio.run(server._request("textDocument/definition", {}, uri="file:///repo/a.cs"))

        self.assertEqual(result, {"answer": 42})
        self.assertEqual(server._last_server, "brokered")
        self.assertIn("brokered", server._just_started_this_call)
        self.assertIn("/repo", server._added_workspaces_this_call)

    def test_router_broker_params_do_not_include_frontend_chain(self) -> None:
        with patch.dict("os.environ", {"HSP_ROUTER": "1"}, clear=True):
            params = server._broker_base_params(route_uri="file:///repo/src/lib.rs")

        self.assertTrue(params["router"])
        self.assertEqual(params["uri"], "file:///repo/src/lib.rs")
        self.assertNotIn("chain", params)
        self.assertNotIn("config_hash", params)


if __name__ == "__main__":
    unittest.main()
