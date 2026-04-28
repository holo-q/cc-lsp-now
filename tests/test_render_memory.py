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


class RenderMemoryAllocationGuardrailTests(unittest.TestCase):
    """Cover the allocation invariants the doc calls out as guardrails:
    1-based members, F/T reservation, monotonic generation, default bucket."""

    def test_member_indexing_is_one_based_for_all_kinds(self) -> None:
        memory = RenderMemory()

        symbol = memory.touch(_identity("Render"))
        file_record = memory.touch(_identity("Renderer.cs", kind=AliasKind.FILE, bucket_key=""))
        type_record = memory.touch(_identity("IRenderer", kind=AliasKind.TYPE, bucket_key="IRenderer", line=9))

        self.assertEqual(symbol.member_index, 1)
        self.assertEqual(symbol.alias, "A1")
        self.assertEqual(file_record.alias, "F1")
        self.assertEqual(type_record.alias, "T1")

    def test_symbol_buckets_skip_F_and_T_reserved_prefixes(self) -> None:
        memory = RenderMemory()

        prefixes = [
            memory.touch(_identity("m", bucket_key=f"k{i}", bucket_label=f"k{i}")).bucket
            for i in range(7)
        ]

        self.assertEqual(prefixes, ["A", "B", "C", "D", "E", "G", "H"])

    def test_default_bucket_key_falls_back_to_path(self) -> None:
        memory = RenderMemory()

        first = memory.touch(_identity("foo", bucket_key="", bucket_label=""))
        second = memory.touch(_identity("bar", line=99, bucket_key="", bucket_label=""))

        self.assertEqual(first.bucket, second.bucket, "same path → same bucket")
        self.assertEqual(first.member_index, 1)
        self.assertEqual(second.member_index, 2)

    def test_generation_only_bumps_on_new_minting(self) -> None:
        memory = RenderMemory()

        memory.touch(_identity("Render"))
        gen1 = memory.generation
        memory.touch(_identity("Render"))
        gen2 = memory.generation
        memory.touch(_identity("Update", line=88))
        gen3 = memory.generation

        self.assertEqual(gen1, 1)
        self.assertEqual(gen2, 1, "reusing identity must not bump generation")
        self.assertEqual(gen3, 2)


class RenderMemoryLookupGuardrailTests(unittest.TestCase):
    def test_lookup_unknown_message_includes_generation(self) -> None:
        memory = RenderMemory()
        memory.touch(_identity("Render"))

        result = memory.lookup("A99")

        self.assertIn(f"gen={memory.generation}", result.message)

    def test_lookup_lowercase_is_canonicalized_to_upper(self) -> None:
        memory = RenderMemory()
        record = memory.touch(_identity("Render"))

        result = memory.lookup(record.alias.lower())

        self.assertTrue(result.ok)
        self.assertIs(result.record, record)

    def test_lookup_empty_or_whitespace_is_invalid(self) -> None:
        for token in ("", "   ", "[]", "[ ]"):
            with self.subTest(token=token):
                self.assertEqual(RenderMemory().lookup(token).error, AliasError.INVALID)

    def test_lookup_malformed_token_is_invalid(self) -> None:
        for token in ("A", "A-1", "1A", "A1B", "::"):
            with self.subTest(token=token):
                self.assertEqual(RenderMemory().lookup(token).error, AliasError.INVALID)

    def test_lookup_zero_member_is_invalid(self) -> None:
        result = RenderMemory().lookup("A0")

        self.assertEqual(result.error, AliasError.INVALID)
        self.assertIn("must be positive", result.message)

    def test_get_returns_record_or_none_without_parsing(self) -> None:
        memory = RenderMemory()
        record = memory.touch(_identity())

        self.assertIs(memory.get(record.alias), record)
        self.assertIsNone(memory.get("Z999"))


class RenderMemoryStalenessGuardrailTests(unittest.TestCase):
    def test_mark_stale_by_identity(self) -> None:
        memory = RenderMemory()
        identity = _identity("Render")
        record = memory.touch(identity)

        retired = memory.mark_stale(identity, "snapshot drift")

        self.assertEqual(retired, record)
        self.assertEqual(memory.lookup(record.alias).error, AliasError.STALE)

    def test_mark_stale_unknown_alias_returns_none(self) -> None:
        self.assertIsNone(RenderMemory().mark_stale("A99", "nope"))

    def test_clear_epoch_drops_stale_tombstones(self) -> None:
        memory = RenderMemory()
        record = memory.touch(_identity("Render"))
        memory.mark_stale(record.alias, "edited")

        memory.clear_epoch()

        # After epoch reset, the old alias string is unknown — not stale —
        # because the epoch boundary is the doc's contract for cross-epoch
        # recycling, and tombstones from prior epochs no longer apply.
        self.assertEqual(memory.lookup(record.alias).error, AliasError.UNKNOWN)


class RenderMemoryLegendShapeTests(unittest.TestCase):
    def test_empty_records_emit_no_legend(self) -> None:
        self.assertEqual(RenderMemory().aliases_for_response([]), "")

    def test_legend_groups_same_bucket_members_on_one_line(self) -> None:
        memory = RenderMemory()
        a1 = memory.touch(_identity("Render"))
        a2 = memory.touch(_identity("Update", line=88))
        b1 = memory.touch(
            _identity("Get", path="/repo/Store.cs", line=21, bucket_key="Store", bucket_label="Store.cs::Store")
        )

        legend = memory.aliases_for_response([a1, a2, b1])

        bucket_a_line = next(line for line in legend.splitlines() if "A=Renderer.cs::Renderer" in line)
        self.assertIn("A1=Render@L44", bucket_a_line)
        self.assertIn("A2=Update@L88", bucket_a_line)
        bucket_b_line = next(line for line in legend.splitlines() if "B=Store.cs::Store" in line)
        self.assertIn("B1=Get@L21", bucket_b_line)

    def test_delta_legend_uses_plus_marker(self) -> None:
        memory = RenderMemory()
        record = memory.touch(_identity("Render"))

        legend = memory.aliases_for_response([record], delta=True)

        self.assertTrue(legend.startswith(f"legend+ gen={memory.generation}:"))

    def test_file_legend_omits_bucket_header_one_chip_per_line(self) -> None:
        memory = RenderMemory()
        f1 = memory.touch(_identity("a.cs", kind=AliasKind.FILE, path="/repo/a.cs", bucket_key=""))
        f2 = memory.touch(_identity("b.cs", kind=AliasKind.FILE, path="/repo/b.cs", bucket_key=""))

        legend = memory.aliases_for_response([f1, f2])

        self.assertIn("F1=/repo/a.cs", legend)
        self.assertIn("F2=/repo/b.cs", legend)
        self.assertNotIn("F=", legend, "files have no bucket header per docs/render-memory.md")
        f1_line = next(line for line in legend.splitlines() if "F1=" in line)
        self.assertNotIn("F2=", f1_line, "each file alias on its own line")

    def test_type_legend_emits_chip_with_at_line(self) -> None:
        memory = RenderMemory()
        t1 = memory.touch(_identity("IRenderer", kind=AliasKind.TYPE, line=9, bucket_key="IRenderer"))

        legend = memory.aliases_for_response([t1])

        self.assertIn("T1=IRenderer@L9", legend)
        self.assertNotIn("T=", legend)

    def test_legend_dedupes_repeated_records(self) -> None:
        memory = RenderMemory()
        record = memory.touch(_identity("Render"))

        legend = memory.aliases_for_response([record, record, record])

        self.assertEqual(legend.count("A1=Render@L44"), 1)


class RenderMemorySnapshotGuardrailTests(unittest.TestCase):
    def test_snapshot_preserves_member_counters_so_no_recycling(self) -> None:
        memory = RenderMemory()
        first = memory.touch(_identity("Render"))
        memory.touch(_identity("Update", line=88))
        memory.touch(_identity("Renderer.cs", kind=AliasKind.FILE, bucket_key=""))
        memory.mark_stale(first.alias, "edited")

        restored = RenderMemory()
        restored.restore(memory.snapshot())

        next_symbol = restored.touch(_identity("Flush", line=120))
        self.assertEqual(next_symbol.alias, "A3", "counter must survive restore — no A1 recycling")
        next_file = restored.touch(_identity("Store.cs", kind=AliasKind.FILE, path="/repo/Store.cs", bucket_key=""))
        self.assertEqual(next_file.alias, "F2")

    def test_snapshot_preserves_epoch_id_and_generation(self) -> None:
        memory = RenderMemory()
        memory.clear_epoch()
        memory.clear_epoch()
        memory.touch(_identity("Render"))

        restored = RenderMemory()
        restored.restore(memory.snapshot())

        self.assertEqual(restored.epoch_id, memory.epoch_id)
        self.assertEqual(restored.generation, memory.generation)


if __name__ == "__main__":
    unittest.main()
