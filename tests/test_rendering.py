import unittest

from cc_lsp_now.rendering import (
    LegendBucket,
    LegendBinding,
    LegendIdentity,
    LegendMember,
    format_compact_row,
    format_empty_state,
    format_legend_block,
    format_path_dense,
    format_path_dense_header,
    format_sample_lines,
    format_sample_locs,
    format_truncation_footer,
    legend_buckets_from_records,
)


class RenderingHelperTests(unittest.TestCase):
    def test_non_exhaustive_samples_use_trailing_ellipsis(self) -> None:
        self.assertEqual(format_sample_lines([78, 159, 218, 400]), "L78,L159,L218,...")

    def test_exhaustive_samples_omit_ellipsis(self) -> None:
        self.assertEqual(format_sample_lines([78, 159, 218]), "L78,L159,L218")

    def test_sample_locs_include_foreign_basenames(self) -> None:
        samples = format_sample_locs(
            [("/repo/A.cs", 10), ("/repo/B.cs", 20), ("/repo/A.cs", 30), ("/repo/C.cs", 40)],
            primary_path="/repo/A.cs",
        )

        self.assertEqual(samples, "L10,B.cs:L20,L30,...")

    def test_truncation_footer_names_unfold_knob(self) -> None:
        self.assertEqual(
            format_truncation_footer(7, "edges", "max_edges"),
            "... +7 more edges; raise max_edges to unfold.",
        )

    def test_empty_state_family(self) -> None:
        self.assertEqual(format_empty_state("references", "A0"), "No references for A0.")

    def test_compact_row_drops_empty_parts_and_newlines(self) -> None:
        self.assertEqual(format_compact_row(["[0] arg ctx", "", "refs 4\nsamples L1"]), "[0] arg ctx — refs 4 samples L1")

    def test_dense_path_plain_and_labeled(self) -> None:
        self.assertEqual(format_path_dense(["A0", "A1", "B0"]), "A0 -> A1 -> B0")
        self.assertEqual(format_path_dense(["A0", "A1", "B0"], ["calls", "refs"]), "A0 -calls-> A1 -refs-> B0")

    def test_dense_path_rejects_missing_edge_label(self) -> None:
        with self.assertRaises(ValueError):
            format_path_dense(["A0", "A1", "B0"], ["calls"])

    def test_dense_path_header_shape(self) -> None:
        self.assertEqual(
            format_path_dense_header("[P0]", 3, 3, "verified", "A0 -> A1 -> B0"),
            "[P0] cost 3 hops 3 verified  A0 -> A1 -> B0",
        )

    def test_legend_block_shape(self) -> None:
        block = format_legend_block(
            [
                LegendBucket(
                    "A",
                    "Renderer.cs::Renderer",
                    (LegendMember("A0", "Render", 44), LegendMember("A1", "Update", 88)),
                )
            ],
            gen=2,
        )

        self.assertIn("legend gen=2:", block)
        self.assertIn("A=Renderer.cs::Renderer", block)
        self.assertIn("A0=Render@L44", block)

    def test_records_group_into_legend_buckets(self) -> None:
        ident = LegendIdentity(
            workspace_root="/repo",
            server_label="csharp-ls",
            kind="method",
            name="Render",
            def_path="/repo/Renderer.cs",
            def_line=44,
            def_char=8,
        )
        buckets = legend_buckets_from_records([
            LegendBinding(alias="A0", identity=ident, bucket_alias="A", bucket_label="Renderer.cs::Renderer")
        ])

        self.assertEqual(buckets[0].bucket_alias, "A")
        self.assertEqual(buckets[0].members[0].alias, "A0")


if __name__ == "__main__":
    unittest.main()
