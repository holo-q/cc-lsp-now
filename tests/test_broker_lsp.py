from __future__ import annotations

import asyncio
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from cc_lsp_now import server
from cc_lsp_now.broker_lsp import BrokerLspSession, chain_config_hash, chain_from_wire, chain_to_wire
from cc_lsp_now.chain_server import ChainServer
from cc_lsp_now.lsp import LspClient


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

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        pass

    async def request(self, method: str, params: dict | None, *, timeout: float = 30.0) -> Any:
        self.requests.append((method, params, timeout))
        return self.result

    async def ensure_document(self, uri: str) -> None:
        self.ensured.append(uri)

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

        with patch.dict("os.environ", {"LSP_SERVERS": "fake-ls", "CC_LSP_BROKER": "on"}, clear=False):
            with patch.object(server, "_broker_lsp_request", AsyncMock(side_effect=fake_broker)):
                with patch.object(server, "_get_client", AsyncMock(side_effect=explode_get_client)):
                    result = asyncio.run(server._request("textDocument/definition", {}, uri="file:///repo/a.cs"))

        self.assertEqual(result, {"answer": 42})
        self.assertEqual(server._last_server, "brokered")
        self.assertIn("brokered", server._just_started_this_call)
        self.assertIn("/repo", server._added_workspaces_this_call)


if __name__ == "__main__":
    unittest.main()
