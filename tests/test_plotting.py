import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arnie import plotting


class PlottingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = {
            "original": {"rv32i": [{"Parsing": 10}]},
            "native": {"rv32i": [{"Parsing": 2}], "huge": [{"Name Resolution": 8}]},
        }

    def test_tables_and_combined_plots_include_specs_missing_from_first_build(self):
        for generate in [
            lambda: plotting.gen_total_time_tex(self.data, self.root),
            lambda: plotting.gen_total_time_table_tex(self.data, self.root),
            lambda: plotting.gen_phase_breakdown_combined_tex(self.data, self.root),
        ]:
            with self.subTest(generate=generate):
                text = generate().read_text()
                self.assertIn("rv32i", text)
                self.assertIn("huge", text)

    def test_individual_breakdown_only_labels_specs_present_in_that_build(self):
        plotting.gen_phase_breakdown_tex(self.data, self.root)
        original = (self.root / "phase_breakdown_original.tex").read_text()
        native = (self.root / "phase_breakdown_native.tex").read_text()
        self.assertNotIn("huge", original)
        self.assertIn("huge", native)

    def test_phase_means_treat_a_missing_phase_as_zero(self):
        result = plotting._phase_means(
            ["Parsing", "Optional"],
            {"rv32i": [{"Parsing": 10, "Optional": 10}, {"Parsing": 10}]},
            ["rv32i"],
        )
        self.assertAlmostEqual(result["rv32i"]["Parsing"], 2 / 3)
        self.assertAlmostEqual(result["rv32i"]["Optional"], 1 / 3)

    def test_warmup_plot_reads_current_and_legacy_metadata_keys(self):
        path = self.root / "native" / "miniARMv7"
        path.mkdir(parents=True)
        (path / "warmup_1.csv").write_text("pass,duration_ms\nParsing,10\n")
        (path / "run_1.csv").write_text("pass,duration_ms\nName Resolution,2\n")
        for key in ["builds", "configurations"]:
            with self.subTest(key=key):
                out = plotting.gen_warmup_plot_tex(
                    self.root, {key: ["native"], "warmup": 1}, self.root
                )
                self.assertIsNotNone(out)
                text = out.read_text()
                self.assertIn("(1,10.00) (2,2.00)", text)
                self.assertIn("axis cs:1.5,0", text)

    def test_warmup_boundaries_follow_each_builds_actual_runs(self):
        for build, count in [("original", 1), ("native", 2)]:
            path = self.root / build / "miniARMv7"
            path.mkdir(parents=True)
            for i in range(1, count + 1):
                (path / f"warmup_{i}.csv").write_text("pass,duration_ms\nParsing,10\n")
            (path / "run_1.csv").write_text("pass,duration_ms\nParsing,2\n")
        out = plotting.gen_warmup_plot_tex(
            self.root, {"builds": ["original", "native"], "warmup": 99}, self.root
        )
        text = out.read_text()
        self.assertIn("axis cs:1.5,0", text)
        self.assertIn("axis cs:2.5,0", text)
        self.assertNotIn("axis cs:99.5,0", text)

    def test_speedups_skip_zero_denominators_without_crashing(self):
        self.data["native"]["rv32i"] = [{"Parsing": 0}]
        self.assertIsNone(plotting.gen_speedup_table_tex(self.data, self.root))
        self.data["native"]["rv32i"] = [{"Parsing": 2}]
        text = plotting.gen_speedup_table_tex(self.data, self.root).read_text()
        self.assertIn(r"5.00$\times$", text)

    def test_speedup_table_compares_variants_of_same_base_build(self):
        data = {
            "graalvm@main": {"rv32i": [{"Parsing": 10}]},
            "graalvm@feature": {"rv32i": [{"Parsing": 5}]},
        }
        text = plotting.gen_speedup_table_tex(data, self.root).read_text()
        self.assertIn("graalvm@feature", text)
        self.assertIn("graalvm@main", text)
        # ordered_builds sorts alphabetically: feature < main, so the pair
        # is (feature, main) and the value is feature/main = 5/10 = 0.50x.
        self.assertIn(r"0.50$\times$", text)

    def test_speedup_table_ignores_variants_of_different_base_builds(self):
        data = {
            "graalvm@main": {"rv32i": [{"Parsing": 10}]},
            "native@main": {"rv32i": [{"Parsing": 5}]},
        }
        self.assertIsNone(plotting.gen_speedup_table_tex(data, self.root))

    def test_relative_comparison_plot_shows_ratios_and_percentages(self):
        data = {
            "native@master": {"rv32i": [{"Parsing": 100}], "huge": [{"Parsing": 200}]},
            "native@chore~feature": {
                "rv32i": [{"Parsing": 90}],
                "huge": [{"Parsing": 210}],
            },
        }
        outputs = plotting.gen_relative_comparison_tex(
            data, self.root, build_order=["native@master", "native@chore~feature"]
        )
        self.assertEqual(len(outputs), 1)
        text = outputs[0].read_text()
        # master is the baseline; chore~feature is 10% faster on rv32i (0.90)
        # and 5% slower on huge (1.05).
        self.assertIn("(rv32i,0.9000) [-10.0", text)
        self.assertIn("(huge,1.0500) [+5.0", text)
        # Reference line at y=1 and baseline in caption.
        self.assertIn("(axis cs:rv32i,1)", text)
        self.assertIn("native@master", text)
        # Display name restores the slash for the variant label.
        self.assertIn("native@chore/feature", text)
        self.assertIn(r"\label{fig:relative_comparison_native}", text)

    def test_relative_comparison_plot_skips_non_variant_builds(self):
        data = {
            "original": {"rv32i": [{"Parsing": 10}]},
            "graalvm": {"rv32i": [{"Parsing": 5}]},
        }
        self.assertEqual(plotting.gen_relative_comparison_tex(data, self.root), [])

    def test_relative_comparison_plot_uses_build_order_for_baseline(self):
        data = {
            "native@a": {"rv32i": [{"Parsing": 100}]},
            "native@b": {"rv32i": [{"Parsing": 50}]},
        }
        # Without build_order, ordered_builds sorts alphabetically: a is baseline.
        outputs = plotting.gen_relative_comparison_tex(data, self.root)
        text = outputs[0].read_text()
        self.assertIn("(rv32i,0.5000) [-50.0", text)
        self.assertIn("native@a", text)
        # With build_order putting b first, b is the baseline.
        outputs = plotting.gen_relative_comparison_tex(
            data, self.root, build_order=["native@b", "native@a"]
        )
        text = outputs[0].read_text()
        self.assertIn("(rv32i,2.0000) [+100.0", text)
        self.assertIn("native@b", text)

    def test_preamble_includes_variant_refs_and_commits(self):
        meta = {
            "builds": ["graalvm@main", "graalvm@feature-x"],
            "runs": 10,
            "warmup": 3,
            "build_details": {
                "graalvm@main": {
                    "family": "openvadl",
                    "commit": "aaa111222333",
                    "ref": "main",
                    "runs": 10,
                    "warmup": 3,
                },
                "graalvm@feature-x": {
                    "family": "openvadl",
                    "commit": "bbb444555666",
                    "ref": "feature-x",
                    "runs": 10,
                    "warmup": 3,
                },
            },
        }
        text = plotting.gen_preamble_tex(meta, self.root).read_text()
        self.assertIn("graalvm@main", text)
        self.assertIn("graalvm@feature-x", text)
        self.assertIn("aaa111", text)
        self.assertIn("bbb444", text)
        self.assertIn("used 10~timed runs after 3~warmup runs", text)

    def test_stats_table_contains_metrics_from_later_specs(self):
        out = plotting.gen_spec_stats_table_tex(
            {"rv32i": {"Files": 1}, "huge": {"Files": 2, "Total Definitions": 99}},
            self.root,
        )
        self.assertIn("Total Definitions", out.read_text())
        self.assertIn(r"\num{99}", out.read_text())

    def test_report_labels_escape_tex_special_characters(self):
        data = {"native": {"future_spec": [{"Future_Phase & Checks": 2}]}}
        total = plotting.gen_total_time_table_tex(data, self.root).read_text()
        self.assertIn(r"future\_spec", total)
        phases = plotting.gen_phase_breakdown_tex(data, self.root)[0].read_text()
        self.assertIn(r"future\_spec", phases)
        self.assertIn(r"Future\_Phase \& Checks", phases)

    def test_preamble_uses_recorded_system_and_reused_build_counts(self):
        meta = {
            "builds": ["original", "native"],
            "runs": 10,
            "warmup": 3,
            "commits": {"open_vadl_path": "new-commit"},
            "system": {"platform": "Test_OS", "machine": "Test CPU"},
            "build_details": {
                "original": {
                    "commit": "old-commit",
                    "runs": 1,
                    "warmup": 0,
                    "source_run": "old_run",
                }
            },
        }
        text = plotting.gen_preamble_tex(meta, self.root).read_text()
        self.assertIn(r"Test\_OS, Test CPU", text)
        self.assertIn("old-commit", text)
        self.assertIn("used 1~timed runs after 0~warmup runs", text)
        self.assertIn("used 10~timed runs after 3~warmup runs", text)
        self.assertIn(r"old\_run", text)
        self.assertNotIn("M3 Max", text)

    def test_png_falls_back_to_poppler_when_ghostscript_is_absent(self):
        pdf = self.root / "plots.pdf"
        pdf.touch()
        (self.root / "plots-99.png").touch()

        def convert(command, **kwargs):
            self.assertEqual(command[0], "pdftoppm")
            prefix = command[-1]
            for number in [10, 2, 1]:
                Path(f"{prefix}-{number}.png").write_bytes(b"PNG placeholder")
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(
                plotting.shutil,
                "which",
                side_effect=lambda name: (
                    "/bin/pdftoppm" if name == "pdftoppm" else None
                ),
            ),
            patch.object(plotting.subprocess, "run", side_effect=convert),
        ):
            pages = plotting.pdf_to_png(pdf)
        self.assertEqual(
            [p.name for p in pages], ["plots-1.png", "plots-2.png", "plots-10.png"]
        )
        self.assertFalse((self.root / "plots-99.png").exists())

    def test_png_no_converter_is_a_warning_and_preserves_previous_pages(self):
        pdf = self.root / "plots.pdf"
        pdf.touch()
        old = self.root / "plots-1.png"
        old.touch()
        with (
            patch.object(plotting.shutil, "which", return_value=None),
            patch.object(plotting.subprocess, "run") as run,
            patch.object(plotting, "print") as warning,
        ):
            self.assertEqual(plotting.pdf_to_png(pdf), [])
        run.assert_not_called()
        warning.assert_called_once()
        self.assertTrue(old.exists())
