"""Socket-path derivation for the hsp-broker daemon.

`docs/broker.md` calls for "auto-started user-level Unix-domain socket"
and the broker is meant to be reachable without environment plumbing.
That only works if every client and the daemon agree on the same path
from the same env state.  These tests pin the resolution rules so the
contract is explicit:

1. `HSP_BROKER_SOCKET` is honoured verbatim — used by tests and by
   anyone who wants an isolated per-project broker.
2. `XDG_RUNTIME_DIR` is preferred when available; the resulting path
   lives under that runtime dir.
3. Without `XDG_RUNTIME_DIR`, the broker falls back to a per-user
   `/tmp/hsp-broker-<user>/` so concurrent users don't share a
   socket.
4. Calling `socket_path()` repeatedly with the same env returns the
   same `Path` — required for both the daemon's bind and the client's
   connect to land on the same file.
"""

from __future__ import annotations

import os
import unittest

from hsp.broker import (
    DEFAULT_SOCKET_NAME,
    SOCKET_ENV_OVERRIDE,
    socket_path,
)


class _EnvScope:
    """Tiny env-var snapshot/restore helper local to these tests."""

    def __init__(self, **overrides: str | None) -> None:
        self._overrides = overrides
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_EnvScope":
        for k, v in self._overrides.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_: object) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SocketPathTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with _EnvScope(
            **{
                SOCKET_ENV_OVERRIDE: "/tmp/hsp-broker-test.sock",
                "XDG_RUNTIME_DIR": "/run/user/0",
            }
        ):
            self.assertEqual(
                str(socket_path()),
                "/tmp/hsp-broker-test.sock",
            )

    def test_xdg_runtime_dir_preferred(self) -> None:
        with _EnvScope(
            **{
                SOCKET_ENV_OVERRIDE: None,
                "XDG_RUNTIME_DIR": "/run/user/1234",
            }
        ):
            p = socket_path()
            self.assertEqual(p.name, DEFAULT_SOCKET_NAME)
            self.assertEqual(str(p.parent), "/run/user/1234")

    def test_fallback_per_user_under_tmp(self) -> None:
        with _EnvScope(
            **{
                SOCKET_ENV_OVERRIDE: None,
                "XDG_RUNTIME_DIR": None,
                "USER": "hsptester",
            }
        ):
            p = socket_path()
            self.assertEqual(p.name, DEFAULT_SOCKET_NAME)
            self.assertEqual(str(p.parent), "/tmp/hsp-broker-hsptester")

    def test_path_is_stable_across_calls(self) -> None:
        with _EnvScope(
            **{
                SOCKET_ENV_OVERRIDE: "/tmp/hsp-broker-stable.sock",
            }
        ):
            self.assertEqual(socket_path(), socket_path())


if __name__ == "__main__":
    unittest.main()
