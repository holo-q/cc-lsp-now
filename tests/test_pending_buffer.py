import unittest

from cc_lsp_now.candidate import Candidate
from cc_lsp_now.candidate_kind import CandidateKind
from cc_lsp_now.pending_buffer import DEFAULT_STAGE_HANDLE, PendingBook, PendingBuffer


def _candidate(title: str) -> Candidate:
    return Candidate(kind=CandidateKind.CODE_ACTION, title=title, edit={"changes": {}})


class PendingBookTests(unittest.TestCase):
    def test_empty_handle_maps_to_default_stage(self) -> None:
        book = PendingBook()

        staged = book.set(PendingBuffer("fix", [_candidate("noop")], "preview", handle=""))

        self.assertEqual(staged.handle, DEFAULT_STAGE_HANDLE)
        self.assertIs(book.active(), staged)

    def test_named_stages_coexist_and_latest_is_active(self) -> None:
        book = PendingBook()
        first = book.set(PendingBuffer("rename", [_candidate("first")], "first", handle="alpha"))
        second = book.set(PendingBuffer("fix", [_candidate("second")], "second", handle="beta"))

        self.assertIs(book.get("alpha"), first)
        self.assertIs(book.get("beta"), second)
        self.assertIs(book.active(), second)
        self.assertEqual(book.handles(), ["alpha", "beta"])

    def test_replacing_stage_preserves_unrelated_stage(self) -> None:
        book = PendingBook()
        book.set(PendingBuffer("rename", [_candidate("first")], "first", handle="alpha"))
        beta = book.set(PendingBuffer("fix", [_candidate("beta")], "beta", handle="beta"))
        replacement = book.set(PendingBuffer("rename", [_candidate("replacement")], "replacement", handle="alpha"))

        self.assertIs(book.get("beta"), beta)
        self.assertIs(book.get("alpha"), replacement)
        self.assertEqual(book.handles(), ["beta", "alpha"])

    def test_clear_active_removes_only_latest_stage(self) -> None:
        book = PendingBook()
        first = book.set(PendingBuffer("rename", [_candidate("first")], "first", handle="alpha"))
        book.set(PendingBuffer("fix", [_candidate("second")], "second", handle="beta"))

        dropped = book.clear_active()

        self.assertIsNotNone(dropped)
        self.assertIs(book.active(), first)
        self.assertEqual(book.handles(), ["alpha"])

    def test_drop_unknown_stage_is_none(self) -> None:
        self.assertIsNone(PendingBook().drop("missing"))


if __name__ == "__main__":
    unittest.main()
