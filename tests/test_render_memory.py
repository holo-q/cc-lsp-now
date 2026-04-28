import unittest

from cc_lsp_now.render_memory import AliasError, AliasIdentity, AliasKind, RenderMemory


def _identity(
    name: str = "Render",
    *,
    kind: AliasKind = AliasKind.SYMBOL,
    path: str = "/repo/src/Renderer.cs",
    line: int = 44,
    character: int = 8,
    bucket_key: str = "Renderer",
    bucket_label: str = "Renderer.cs::Renderer",
) -> AliasIdentity:
    return AliasIdentity(
        kind=kind,
        name=name,
        path=path,
        line=line,
        character=character,
        symbol_kind="method" if kind is AliasKind.SYMBOL else kind.value,
        bucket_key=bucket_key,
        bucket_label=bucket_label,
    )


class RenderMemoryTests(unittest.TestCase):
    def test_symbol_aliases_are_deterministic_inside_bucket(self) -> None:
        memory = RenderMemory()

        first = memory.touch(_identity("Render"))
        second = memory.touch(_identity("Update", line=88))

        self.assertEqual(first.alias, "A1")
        self.assertEqual(second.alias, "A2")

    def test_same_identity_reuses_alias(self) -> None:
        memory = RenderMemory()
        identity = _identity("Render")

        first = memory.touch(identity)
        second = memory.touch(identity)

        self.assertEqual(first.alias, second.alias)
        self.assertEqual(memory.generation, 1)

    def test_different_bucket_gets_different_letter(self) -> None:
        memory = RenderMemory()

        first = memory.touch(_identity("Render", bucket_key="Renderer"))
        second = memory.touch(
            _identity(
                "Flush",
                path="/repo/src/Pipeline.cs",
                line=21,
                bucket_key="Pipeline",
                bucket_label="Pipeline.cs::Pipeline",
            )
        )

        self.assertEqual(first.alias, "A1")
        self.assertEqual(second.alias, "B1")

    def test_file_and_type_alias_families_are_reserved(self) -> None:
        memory = RenderMemory()

        file_record = memory.touch(_identity("Renderer.cs", kind=AliasKind.FILE, bucket_key="", line=1))
        type_record = memory.touch(_identity("Renderer", kind=AliasKind.TYPE, bucket_key="Renderer", line=3))

        self.assertEqual(file_record.alias, "F1")
        self.assertEqual(type_record.alias, "T1")

    def test_lookup_accepts_bracketed_and_unbracketed_tokens(self) -> None:
        memory = RenderMemory()
        record = memory.touch(_identity())

        self.assertIs(memory.lookup(record.alias).record, record)
        self.assertIs(memory.lookup(f"[{record.alias}]").record, record)

    def test_unknown_alias_returns_lookup_error(self) -> None:
        result = RenderMemory().lookup("A99")

        self.assertEqual(result.error, AliasError.UNKNOWN)
        self.assertIn("not active", result.message)

    def test_unicode_alias_is_invalid_not_fuzzy_matched(self) -> None:
        result = RenderMemory().lookup("Α1")

        self.assertEqual(result.error, AliasError.INVALID)
        self.assertIn("non-ASCII", result.message)

    def test_numeric_token_is_not_an_alias(self) -> None:
        result = RenderMemory().lookup("L42")

        self.assertEqual(result.error, AliasError.UNKNOWN)

        numeric = RenderMemory().lookup("[3]")
        self.assertEqual(numeric.error, AliasError.INVALID)

    def test_stale_alias_refuses_without_recycling(self) -> None:
        memory = RenderMemory()
        first = memory.touch(_identity("Render"))

        retired = memory.mark_stale(first.alias, "file changed")
        self.assertIsNotNone(retired)
        stale = memory.lookup(first.alias)

        self.assertEqual(stale.error, AliasError.STALE)
        self.assertIn("file changed", stale.message)

        second = memory.touch(_identity("Update", line=88))
        self.assertEqual(second.alias, "A2")

    def test_clear_epoch_restarts_alias_book_and_bumps_epoch(self) -> None:
        memory = RenderMemory()
        first = memory.touch(_identity())

        memory.clear_epoch()
        second = memory.touch(_identity())

        self.assertEqual(first.alias, "A1")
        self.assertEqual(second.alias, "A1")
        self.assertEqual(second.epoch_id, first.epoch_id + 1)

    def test_legend_contains_generation_and_bucket_rows(self) -> None:
        memory = RenderMemory()
        first = memory.touch(_identity("Render"))

        legend = memory.aliases_for_response([first])

        self.assertIn("legend gen=1:", legend)
        self.assertIn("A=Renderer.cs::Renderer", legend)
        self.assertIn("A1=Render@L44", legend)

    def test_snapshot_round_trips_records(self) -> None:
        memory = RenderMemory()
        first = memory.touch(_identity("Render"))
        memory.mark_stale(first.alias, "retired")

        restored = RenderMemory()
        restored.restore(memory.snapshot())
        result = restored.lookup("A1")

        self.assertEqual(result.error, AliasError.STALE)
        self.assertIn("retired", result.message)


if __name__ == "__main__":
    unittest.main()
