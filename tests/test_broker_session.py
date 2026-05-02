"""Workspace-session registry contract.

`docs/broker.md` calls out the v1 session model:

- session key = `(language, root, command, args, env/config hash)`;
- the broker reference-counts active clients;
- two clients hitting the same key share one record;
- stop must remove the record cleanly.

These tests pin the registry's behaviour without standing up a socket.
The render-memory contract in `docs/render-memory.md` also depends on
these guarantees: alias books are scoped per session, and aliases must
be stable while a session is live.  If `get_or_create` ever silently
minted new ids for the same key, alias books would split.
"""

from __future__ import annotations

import unittest

from hsp.broker_session import (
    BrokerSession,
    SessionKey,
    SessionRegistry,
    config_hash,
    session_to_dict,
)


class SessionKeyTests(unittest.TestCase):
    def test_key_equality(self) -> None:
        a = SessionKey(root="/repo", config_hash="abc")
        b = SessionKey(root="/repo", config_hash="abc")
        c = SessionKey(root="/repo", config_hash="def")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_key_is_hashable(self) -> None:
        bag = {SessionKey(root="/r", config_hash="h"): 1}
        self.assertEqual(bag[SessionKey(root="/r", config_hash="h")], 1)


class ConfigHashTests(unittest.TestCase):
    def test_same_inputs_same_hash(self) -> None:
        a = config_hash("ty", "ty", ["server"], env={"PYTHONPATH": "/x"})
        b = config_hash("ty", "ty", ["server"], env={"PYTHONPATH": "/x"})
        self.assertEqual(a, b)

    def test_different_command_different_hash(self) -> None:
        a = config_hash("ty", "ty", ["server"])
        b = config_hash("ty", "basedpyright", ["server"])
        self.assertNotEqual(a, b)

    def test_different_env_different_hash(self) -> None:
        a = config_hash("ty", "ty", env={"FOO": "1"})
        b = config_hash("ty", "ty", env={"FOO": "2"})
        self.assertNotEqual(a, b)

    def test_hash_is_short_hex(self) -> None:
        h = config_hash("ty", "ty")
        self.assertEqual(len(h), 12)
        int(h, 16)  # raises if not hex


class RegistryTests(unittest.TestCase):
    def test_get_or_create_mints_new_session(self) -> None:
        r = SessionRegistry()
        s = r.get_or_create(
            SessionKey(root="/repo", config_hash="abc"),
            server_label="ty",
        )
        self.assertEqual(s.key.root, "/repo")
        self.assertEqual(s.server_label, "ty")
        self.assertTrue(s.session_id.startswith("s"))
        self.assertEqual(len(r), 1)

    def test_same_key_returns_same_session(self) -> None:
        r = SessionRegistry()
        k = SessionKey(root="/repo", config_hash="abc")
        a = r.get_or_create(k)
        b = r.get_or_create(k)
        self.assertIs(a, b)
        self.assertEqual(a.session_id, b.session_id)
        self.assertEqual(len(r), 1)

    def test_different_keys_yield_distinct_sessions(self) -> None:
        r = SessionRegistry()
        a = r.get_or_create(SessionKey(root="/repo-a", config_hash="h"))
        b = r.get_or_create(SessionKey(root="/repo-b", config_hash="h"))
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertEqual(len(r), 2)

    def test_same_root_different_hash_distinct(self) -> None:
        r = SessionRegistry()
        a = r.get_or_create(SessionKey(root="/repo", config_hash="h1"))
        b = r.get_or_create(SessionKey(root="/repo", config_hash="h2"))
        self.assertNotEqual(a.session_id, b.session_id)

    def test_get_or_create_touches_last_used(self) -> None:
        r = SessionRegistry()
        k = SessionKey(root="/repo", config_hash="abc")
        a = r.get_or_create(k)
        before = a.last_used_at
        a.last_used_at = before - 100.0  # backdate
        b = r.get_or_create(k)
        self.assertIs(a, b)
        self.assertGreater(b.last_used_at, before - 100.0)

    def test_stop_removes_session(self) -> None:
        r = SessionRegistry()
        s = r.get_or_create(SessionKey(root="/repo", config_hash="abc"))
        self.assertTrue(r.stop(s.session_id))
        self.assertEqual(len(r), 0)
        self.assertFalse(r.stop(s.session_id))

    def test_get_finds_by_id(self) -> None:
        r = SessionRegistry()
        s = r.get_or_create(SessionKey(root="/repo", config_hash="abc"))
        self.assertIs(r.get(s.session_id), s)
        self.assertIsNone(r.get("does-not-exist"))

    def test_all_sessions_returns_snapshot_list(self) -> None:
        r = SessionRegistry()
        r.get_or_create(SessionKey(root="/a", config_hash="x"))
        r.get_or_create(SessionKey(root="/b", config_hash="x"))
        sessions = r.all_sessions()
        self.assertEqual(len(sessions), 2)
        # Returned list is a snapshot; mutating it must not affect registry.
        sessions.clear()
        self.assertEqual(len(r), 2)


class SessionDictShape(unittest.TestCase):
    def test_session_to_dict_keys(self) -> None:
        s = BrokerSession(
            session_id="s1",
            key=SessionKey(root="/repo", config_hash="abc"),
            server_label="ty",
        )
        d = session_to_dict(s)
        self.assertEqual(set(d.keys()), {
            "session_id",
            "root",
            "config_hash",
            "server_label",
            "started_at",
            "last_used_at",
            "client_count",
        })
        self.assertEqual(d["session_id"], "s1")
        self.assertEqual(d["root"], "/repo")


if __name__ == "__main__":
    unittest.main()
