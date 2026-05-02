"""JSONL framing + request dispatch contract for the broker daemon.

`docs/broker.md` describes the broker as a JSONL Unix-socket daemon.
This file pins the wire shape without a socket: the encode/decode pair,
plus `BrokerDaemon.handle_request` for `ping`, `status`,
`session.get_or_create`, and the malformed-request path.

Driving `handle_request` directly (no socket) keeps the tests fast and
makes the failure modes obvious — every error frame exercised here is
exactly what `test_broker_status.py` will see over the wire.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import cast

from hsp.broker import (
    BrokerDaemon,
    BrokerError,
    decode_message,
    encode_message,
)


def _handle(d: BrokerDaemon, req: dict[str, object]) -> dict[str, object]:
    return asyncio.run(d.handle_request(req))


def _as_dict(x: object) -> dict[str, object]:
    assert isinstance(x, dict)
    return cast(dict[str, object], x)


class FramingTests(unittest.TestCase):
    def test_roundtrip_preserves_payload(self) -> None:
        msg: dict[str, object] = {
            "id": "c1",
            "method": "ping",
            "params": {"a": 1, "b": [1, 2]},
        }
        wire = encode_message(msg)
        self.assertTrue(wire.endswith(b"\n"))
        self.assertEqual(decode_message(wire), msg)

    def test_decode_accepts_string_input(self) -> None:
        msg: dict[str, object] = {"id": 1, "method": "status"}
        wire = encode_message(msg).decode("utf-8")
        self.assertEqual(decode_message(wire), msg)

    def test_decode_rejects_non_json(self) -> None:
        with self.assertRaises(BrokerError) as cm:
            decode_message(b"not json\n")
        self.assertEqual(cm.exception.code, "invalid_request")

    def test_decode_rejects_empty_frame(self) -> None:
        with self.assertRaises(BrokerError) as cm:
            decode_message(b"\n")
        self.assertEqual(cm.exception.code, "invalid_request")

    def test_decode_rejects_non_object_root(self) -> None:
        with self.assertRaises(BrokerError) as cm:
            decode_message(b"[1, 2, 3]\n")
        self.assertEqual(cm.exception.code, "invalid_request")

    def test_encode_is_deterministic(self) -> None:
        a = encode_message({"b": 2, "a": 1})
        b = encode_message({"a": 1, "b": 2})
        self.assertEqual(a, b)


class HandlerTests(unittest.TestCase):
    def test_ping_returns_pong(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c1", "method": "ping"})
        self.assertEqual(resp, {"id": "c1", "result": {"pong": True}})

    def test_status_response_shape(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c2", "method": "status"})
        self.assertIn("result", resp)
        result = _as_dict(resp["result"])
        self.assertEqual(set(result.keys()), {
            "pid", "started_at", "uptime", "session_count", "sessions", "bus", "devtools",
        })
        self.assertEqual(result["session_count"], 0)
        self.assertEqual(result["sessions"], [])
        self.assertEqual(_as_dict(result["bus"])["event_count"], 0)
        self.assertFalse(_as_dict(result["devtools"])["enabled"])

    def test_unknown_method_returns_error_frame(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c3", "method": "does_not_exist"})
        self.assertIn("error", resp)
        err = _as_dict(resp["error"])
        self.assertEqual(err["code"], "unknown_method")

    def test_missing_method_returns_invalid_request(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c4"})
        err = _as_dict(resp["error"])
        self.assertEqual(err["code"], "invalid_request")

    def test_params_must_be_object(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c5", "method": "ping", "params": [1, 2]})
        err = _as_dict(resp["error"])
        self.assertEqual(err["code"], "invalid_request")

    def test_session_get_or_create_requires_root_and_hash(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {
            "id": "c6",
            "method": "session.get_or_create",
            "params": {},
        })
        err = _as_dict(resp["error"])
        self.assertEqual(err["code"], "invalid_params")

    def test_session_get_or_create_returns_record(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {
            "id": "c7",
            "method": "session.get_or_create",
            "params": {"root": "/repo", "config_hash": "abc", "server_label": "ty"},
        })
        result = _as_dict(resp["result"])
        self.assertEqual(result["root"], "/repo")
        self.assertEqual(result["config_hash"], "abc")
        self.assertEqual(result["server_label"], "ty")
        self.assertTrue(result["session_id"])

    def test_shutdown_sets_event(self) -> None:
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c8", "method": "shutdown"})
        self.assertEqual(resp["result"], {"shutting_down": True})
        self.assertTrue(d.shutdown_event.is_set())

    def test_response_id_is_echoed(self) -> None:
        d = BrokerDaemon()
        for rid in ("abc", 17, None):
            resp = _handle(d, {"id": rid, "method": "ping"})
            self.assertEqual(resp.get("id"), rid)

    def test_response_is_json_encodable(self) -> None:
        # Every result/error frame produced by the daemon must round-trip
        # through json.dumps so the socket layer can ship it as JSONL.
        d = BrokerDaemon()
        resp = _handle(d, {"id": "c9", "method": "status"})
        json.dumps(resp)


if __name__ == "__main__":
    unittest.main()
