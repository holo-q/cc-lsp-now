import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from hsp.broker import BrokerDaemon, BrokerError, decode_message, encode_message, socket_path
from hsp.broker_session import SessionKey, SessionRegistry, config_hash


class BrokerProtocolTests(unittest.TestCase):
    def test_socket_path_honors_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            expected = Path(d) / "broker.sock"
            with patch.dict(os.environ, {"HSP_BROKER_SOCKET": str(expected)}):
                self.assertEqual(socket_path(), expected)

    def test_jsonl_round_trip_is_deterministic(self) -> None:
        frame = encode_message({"method": "ping", "id": "1", "params": {}})

        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(decode_message(frame), {"id": "1", "method": "ping", "params": {}})

    def test_decode_rejects_non_object_frame(self) -> None:
        with self.assertRaises(BrokerError):
            decode_message("[]\n")


class BrokerDaemonTests(unittest.TestCase):
    def test_status_response_includes_sessions(self) -> None:
        daemon = BrokerDaemon()

        result = asyncio.run(daemon.handle_request({"id": "s", "method": "status", "params": {}}))

        self.assertEqual(result["id"], "s")
        payload = cast(dict[str, object], result["result"])
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["session_count"], 0)

    def test_malformed_request_returns_structured_error(self) -> None:
        daemon = BrokerDaemon()

        result = asyncio.run(daemon.handle_request({"id": "bad", "params": {}}))

        self.assertEqual(result["id"], "bad")
        error = cast(dict[str, object], result["error"])
        self.assertEqual(error["code"], "invalid_request")

    def test_session_get_or_create_reuses_session_key(self) -> None:
        daemon = BrokerDaemon()
        params = {"root": "/repo", "config_hash": "abc", "server_label": "csharp-ls"}

        first = asyncio.run(daemon.handle_request({"id": "1", "method": "session.get_or_create", "params": params}))
        second = asyncio.run(daemon.handle_request({"id": "2", "method": "session.get_or_create", "params": params}))

        first_result = cast(dict[str, object], first["result"])
        second_result = cast(dict[str, object], second["result"])
        self.assertEqual(first_result["session_id"], second_result["session_id"])
        self.assertEqual(first_result["server_label"], "csharp-ls")


class SessionRegistryTests(unittest.TestCase):
    def test_config_hash_is_stable_and_sensitive_to_args(self) -> None:
        first = config_hash("csharp", "csharp-ls", ["--stdio"])
        second = config_hash("csharp", "csharp-ls", ["--stdio"])
        third = config_hash("csharp", "csharp-ls", ["--verbose"])

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_registry_reuses_matching_key(self) -> None:
        registry = SessionRegistry()
        key = SessionKey(root="/repo", config_hash="abc")

        first = registry.get_or_create(key)
        second = registry.get_or_create(key)

        self.assertIs(first, second)
        self.assertEqual(len(registry), 1)


if __name__ == "__main__":
    unittest.main()
