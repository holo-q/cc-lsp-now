import unittest

from cc_lsp_now.alias_coordinator import (
    AliasCoordinator,
    alias_identity_from_wire,
    alias_identity_to_wire,
    alias_record_from_wire,
    alias_record_to_wire,
)
from cc_lsp_now.render_memory import AliasIdentity, AliasKind


def _identity(name: str = "ctx") -> AliasIdentity:
    return AliasIdentity(
        kind=AliasKind.SYMBOL,
        name=name,
        path="/repo/src/Renderer.cs",
        line=44,
        character=12,
        symbol_kind="arg",
        bucket_key="Renderer",
        bucket_label="Renderer.cs::Renderer",
    )


class AliasCoordinatorTests(unittest.TestCase):
    def test_same_identity_gets_same_alias_across_clients(self) -> None:
        coordinator = AliasCoordinator()
        first = coordinator.touch("agent-a", [_identity()])
        second = coordinator.touch("agent-b", [_identity()])

        self.assertEqual(first.records[0].alias, "A1")
        self.assertEqual(second.records[0].alias, "A1")
        self.assertTrue(first.decisions[0].introduced)
        self.assertTrue(second.decisions[0].introduced)
        self.assertIn("A1=ctx@L44", first.legend)
        self.assertIn("A1=ctx@L44", second.legend)

    def test_seen_client_gets_no_duplicate_intro_legend(self) -> None:
        coordinator = AliasCoordinator()
        coordinator.touch("agent-a", [_identity()])

        second = coordinator.touch("agent-a", [_identity()])

        self.assertEqual(second.records[0].alias, "A1")
        self.assertFalse(second.decisions[0].introduced)
        self.assertEqual(second.legend, "")

    def test_reset_client_keeps_master_alias_but_reintroduces_to_client(self) -> None:
        coordinator = AliasCoordinator()
        coordinator.touch("agent-a", [_identity()])

        self.assertTrue(coordinator.clear_client("agent-a"))
        second = coordinator.touch("agent-a", [_identity()])

        self.assertEqual(second.records[0].alias, "A1")
        self.assertTrue(second.decisions[0].introduced)
        self.assertIn("A1=ctx@L44", second.legend)

    def test_wire_helpers_round_trip_identity_and_record(self) -> None:
        coordinator = AliasCoordinator()
        record = coordinator.touch("agent-a", [_identity()]).records[0]

        self.assertEqual(alias_identity_from_wire(alias_identity_to_wire(record.identity)), record.identity)
        self.assertEqual(alias_record_from_wire(alias_record_to_wire(record)), record)


if __name__ == "__main__":
    unittest.main()
