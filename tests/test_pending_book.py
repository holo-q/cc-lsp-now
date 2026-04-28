"""Tests for the multi-slot ``PendingBook`` introduced for parallel-agent
staging.

Pinned contracts (see ``docs/agent-tool-roadmap.md`` "Pending Edits" and
``docs/broker.md`` "Staged Edits And Prediction"):

- The most recently set stage is *active*; ``lsp_confirm(0)`` (no ``stage``)
  resolves against it. This is the legacy single-slot behavior preserved.
- Named handles coexist with the default slot. Replacing one stage never
  rewrites another stage's metadata or candidate list.
- Confirming a stage drops only that stage from the book; older stages keep
  working and confirming.
- Clearing the active stage promotes the previous stage to active.
- Unknown stage handles error readably and never silently match a different
  stage.
"""
from __future__ import annotations

import asyncio
import unittest

from cc_lsp_now import server as _server
from cc_lsp_now.candidate import Candidate
from cc_lsp_now.candidate_kind import CandidateKind
from cc_lsp_now.pending_buffer import (
    DEFAULT_STAGE_HANDLE,
    PendingBook,
    PendingBuffer,
)


def _candidate(title: str) -> Candidate:
    """No-op edit candidate. Empty ``changes`` means ``_apply_candidate``
    is a successful no-op and we don't write to disk.
    """
    return Candidate(
        kind=CandidateKind.CODE_ACTION,
        title=title,
        edit={"changes": {}},
    )


class PendingBookPureTests(unittest.TestCase):
    """Pure ``PendingBook`` data-structure tests, no server integration."""

    def test_empty_book_has_no_active_stage(self) -> None:
        book = PendingBook()
        self.assertIsNone(book.active())
        self.assertIsNone(book.active_handle())
        self.assertEqual(book.handles(), [])
        self.assertEqual(len(book), 0)
        self.assertFalse(bool(book))

    def test_set_makes_stage_active(self) -> None:
        book = PendingBook()
        buf = PendingBuffer(
            kind="symbol_rename",
            candidates=[_candidate("rename")],
            description="rename preview",
            handle="rename-history-ui",
        )
        book.set(buf)
        self.assertIs(book.active(), buf)
        self.assertEqual(book.active_handle(), "rename-history-ui")
        self.assertIn("rename-history-ui", book)

    def test_default_handle_is_used_when_buffer_handle_is_empty(self) -> None:
        # An empty handle must not vanish into a separate "" slot — it routes
        # to the named DEFAULT_STAGE_HANDLE so legacy unnamed callers stay
        # consistent with named-default callers.
        book = PendingBook()
        buf = PendingBuffer(
            kind="fix",
            candidates=[_candidate("noop")],
            description="legacy",
            handle="",
        )
        book.set(buf)
        self.assertEqual(book.active_handle(), DEFAULT_STAGE_HANDLE)
        self.assertIs(book.get(DEFAULT_STAGE_HANDLE), buf)
        self.assertIs(book.get(""), buf)
        self.assertEqual(buf.handle, DEFAULT_STAGE_HANDLE)

    def test_latest_stage_is_active_with_multiple_stages(self) -> None:
        book = PendingBook()
        first = PendingBuffer("fix", [_candidate("first")], "first", handle="alpha")
        second = PendingBuffer("fix", [_candidate("second")], "second", handle="beta")
        book.set(first)
        book.set(second)
        self.assertIs(book.active(), second)
        self.assertEqual(book.active_handle(), "beta")
        # The earlier stage is still resolvable by name.
        self.assertIs(book.get("alpha"), first)

    def test_replacing_a_stage_does_not_corrupt_other_stages(self) -> None:
        # Brief explicit requirement: replacement must not corrupt older stage
        # metadata. Setting alpha → beta → alpha should leave beta untouched
        # while bumping alpha back to active.
        book = PendingBook()
        alpha_v1 = PendingBuffer("fix", [_candidate("a1")], "a1", handle="alpha")
        beta = PendingBuffer("fix", [_candidate("b1")], "b1", handle="beta")
        alpha_v2 = PendingBuffer(
            "symbol_rename", [_candidate("a2")], "a2 rename", handle="alpha"
        )
        book.set(alpha_v1)
        book.set(beta)
        book.set(alpha_v2)

        # Beta keeps every byte of its own metadata.
        survivor = book.get("beta")
        self.assertIs(survivor, beta)
        assert survivor is not None  # ty
        self.assertEqual(survivor.kind, "fix")
        self.assertEqual(survivor.description, "b1")
        self.assertEqual(survivor.candidates[0].title, "b1")

        # Alpha's slot now holds v2 and is the active stage; v1 is gone.
        self.assertIs(book.get("alpha"), alpha_v2)
        self.assertEqual(book.active_handle(), "alpha")
        self.assertEqual(book.handles(), ["beta", "alpha"])

    def test_clear_active_promotes_previous_stage(self) -> None:
        book = PendingBook()
        alpha = PendingBuffer("fix", [_candidate("a")], "a", handle="alpha")
        beta = PendingBuffer("fix", [_candidate("b")], "b", handle="beta")
        book.set(alpha)
        book.set(beta)

        cleared = book.clear_active()
        self.assertIs(cleared, beta)
        self.assertIs(book.active(), alpha)
        self.assertEqual(book.active_handle(), "alpha")

    def test_drop_specific_handle_leaves_active_alone_if_not_active(self) -> None:
        book = PendingBook()
        alpha = PendingBuffer("fix", [_candidate("a")], "a", handle="alpha")
        beta = PendingBuffer("fix", [_candidate("b")], "b", handle="beta")
        book.set(alpha)
        book.set(beta)

        dropped = book.drop("alpha")
        self.assertIs(dropped, alpha)
        # Beta stays active because it was active before; only alpha left.
        self.assertEqual(book.active_handle(), "beta")
        self.assertNotIn("alpha", book)

    def test_drop_unknown_handle_returns_none(self) -> None:
        book = PendingBook()
        self.assertIsNone(book.drop("ghost"))


class ServerPendingIntegrationTests(unittest.TestCase):
    """``_set_pending`` / ``_clear_pending`` / ``lsp_confirm`` against the
    module-level book. These pin the ``lsp_confirm(0)`` legacy contract and
    the new named-stage confirmation flow.
    """

    def setUp(self) -> None:
        # Snapshot and reset both the book and the legacy mirror so leakage
        # from other tests doesn't poison assertions. Anything previously
        # staged is fully restored in tearDown.
        self._prior_pending = _server._pending
        self._prior_book = _server._pending_book
        _server._pending_book = PendingBook()
        _server._pending = None

    def tearDown(self) -> None:
        _server._pending_book = self._prior_book
        _server._pending = self._prior_pending

    # --- legacy single-slot contract ----------------------------------------

    def test_set_pending_without_handle_lands_on_default_stage(self) -> None:
        _server._set_pending("fix", [_candidate("noop")], "1 action")
        self.assertEqual(_server._pending_book.active_handle(), DEFAULT_STAGE_HANDLE)
        self.assertIsNotNone(_server._pending)
        assert _server._pending is not None  # ty
        self.assertEqual(_server._pending.kind, "fix")

    def test_lsp_confirm_zero_targets_active_stage(self) -> None:
        # Two stages: the default slot (set first) and a named one (set
        # second). lsp_confirm(0) without `stage=` must hit the named one
        # because it is the active/latest.
        _server._set_pending("fix", [_candidate("default cand")], "default")
        _server._set_pending(
            "symbol_rename",
            [_candidate("named cand")],
            "named preview",
            handle="rename-history-ui",
        )

        result = asyncio.run(_server.lsp_confirm(0))

        self.assertIn("Applied", result)
        self.assertIn("symbol_rename", result)
        self.assertIn("named cand", result)
        # Only the named stage was committed; the default slot is intact.
        self.assertNotIn("rename-history-ui", _server._pending_book)
        self.assertIn(DEFAULT_STAGE_HANDLE, _server._pending_book)
        # `_pending` mirror has been promoted back to the surviving default slot.
        self.assertIsNotNone(_server._pending)
        assert _server._pending is not None  # ty
        self.assertEqual(_server._pending.handle, DEFAULT_STAGE_HANDLE)

    # --- named-stage confirmation -------------------------------------------

    def test_lsp_confirm_with_stage_targets_named_buffer(self) -> None:
        _server._set_pending("fix", [_candidate("default cand")], "default")
        _server._set_pending(
            "symbol_rename",
            [_candidate("named cand")],
            "named",
            handle="rename-history-ui",
        )

        # Explicitly target the *default* slot even though it is not active.
        result = asyncio.run(
            _server.lsp_confirm(0, stage=DEFAULT_STAGE_HANDLE)
        )

        self.assertIn("Applied", result)
        self.assertIn("default cand", result)
        # The named stage is untouched.
        self.assertIn("rename-history-ui", _server._pending_book)
        named = _server._pending_book.get("rename-history-ui")
        self.assertIsNotNone(named)
        assert named is not None  # ty
        self.assertEqual(named.candidates[0].title, "named cand")

    def test_lsp_confirm_unknown_stage_errors_readably(self) -> None:
        _server._set_pending(
            "fix",
            [_candidate("noop")],
            "stage A",
            handle="stage-a",
        )

        result = asyncio.run(_server.lsp_confirm(0, stage="not-a-real-stage"))

        self.assertIn("not-a-real-stage", result)
        # The error names the stages that *are* active so the agent can recover.
        self.assertIn("stage-a", result)
        # Nothing was confirmed — the stage we did stage is still around.
        self.assertIn("stage-a", _server._pending_book)

    def test_lsp_confirm_with_no_stages_returns_nothing_to_confirm(self) -> None:
        result = asyncio.run(_server.lsp_confirm(0))
        self.assertEqual(result, "Nothing to confirm.")

    def test_invalid_index_does_not_drop_stage(self) -> None:
        # Out-of-range index is a user error; the stage must still be
        # available so the agent can retry with a valid index.
        _server._set_pending(
            "fix",
            [_candidate("only one")],
            "single",
            handle="solo",
        )
        result = asyncio.run(_server.lsp_confirm(99, stage="solo"))
        self.assertIn("Invalid index 99", result)
        self.assertIn("solo", _server._pending_book)

    # --- clear semantics ----------------------------------------------------

    def test_clear_pending_default_drops_active_only(self) -> None:
        _server._set_pending("fix", [_candidate("d")], "d")
        _server._set_pending(
            "symbol_rename", [_candidate("n")], "n", handle="named"
        )

        _server._clear_pending()

        # Active was "named"; it's gone; default is now active.
        self.assertNotIn("named", _server._pending_book)
        self.assertIn(DEFAULT_STAGE_HANDLE, _server._pending_book)
        self.assertEqual(
            _server._pending_book.active_handle(), DEFAULT_STAGE_HANDLE
        )
        # Mirror reflects the surviving default.
        self.assertIsNotNone(_server._pending)
        assert _server._pending is not None  # ty
        self.assertEqual(_server._pending.handle, DEFAULT_STAGE_HANDLE)

    def test_clear_pending_with_handle_drops_only_that_stage(self) -> None:
        _server._set_pending(
            "fix", [_candidate("a")], "a", handle="alpha"
        )
        _server._set_pending(
            "fix", [_candidate("b")], "b", handle="beta"
        )

        _server._clear_pending("alpha")

        self.assertNotIn("alpha", _server._pending_book)
        self.assertIn("beta", _server._pending_book)
        # Beta was active before the drop and stays active.
        self.assertEqual(_server._pending_book.active_handle(), "beta")


if __name__ == "__main__":
    unittest.main()
