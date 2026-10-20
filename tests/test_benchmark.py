import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomli_w

from arnie import benchmark
from arnie.errors import ArnieError
from arnie.results import load_metadata, load_runs


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "compiler"
        self.repo.mkdir()
        self.data_dir = self.root / "data"
        self.old = self.data_dir / "old-run"
        self.old.mkdir(parents=True)
        (self.old / "sentinel").write_text("keep old results")
        (self.data_dir / "latest").symlink_to("old-run", target_is_directory=True)
        self.compiler = self.repo / "compiler.py"
        self.compiler.write_text(
            "from pathlib import Path\nPath('timings.csv').write_text('pass,duration_ms\\nSymbol Resolution,2\\nParsing,3\\n')\n"
        )
        self.config = {
            "native": {
                "family": "openvadl",
                "repo_key": "open_vadl_path",
                "build_cmd": [sys.executable, "-c", "pass"],
                "build_env": None,
                "run_cmd": [sys.executable, str(self.compiler), "<SPEC>"],
                "timings_file": "timings.csv",
                "stats_file": None,
            }
        }
        for target, value in [
            ("DATA_DIR", self.data_dir),
            ("BUILD_CONFIGS", self.config),
            ("SPECS", {"rv32i": {"openvadl": "input.vadl"}}),
        ]:
            self.enterContext(patch.object(benchmark, target, value))
        self.enterContext(
            patch.object(
                benchmark, "require_repos", return_value={"open_vadl_path": self.repo}
            )
        )
        self.enterContext(
            patch.object(benchmark, "git_commit", return_value="new-commit")
        )
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.args = argparse.Namespace(
            build=["native"], runs=2, warmup=1, no_build=True, reuse_original=False
        )

    def test_success_preserves_history_and_records_distinct_warmups(self):
        benchmark.cmd_bench(self.args)
        current = (self.data_dir / "latest").resolve()
        self.assertNotEqual(current, self.old)
        self.assertEqual((self.old / "sentinel").read_text(), "keep old results")
        self.assertEqual(len(load_runs(current / "native" / "rv32i")), 2)
        self.assertTrue((current / "native" / "rv32i" / "warmup_1.csv").exists())
        self.assertEqual(
            load_metadata(current)["build_details"]["native"]["commit"], "new-commit"
        )
        benchmark.cmd_bench(self.args)
        self.assertTrue(current.exists())
        self.assertNotEqual(current, (self.data_dir / "latest").resolve())

    def test_missing_output_is_not_replaced_by_a_stale_csv(self):
        (self.repo / "timings.csv").write_text("pass,duration_ms\nParsing,999\n")
        self.compiler.write_text("pass\n")
        with self.assertRaisesRegex(ArnieError, "did not produce"):
            benchmark.cmd_bench(self.args)
        self.assertEqual((self.data_dir / "latest").resolve(), self.old)
        self.assertFalse(list(self.data_dir.glob("*/native/rv32i/run_*.csv")))

    def test_failed_command_retains_output_and_previous_latest(self):
        self.compiler.write_text(
            "import sys\nprint('compiler diagnostic', file=sys.stderr)\nsys.exit(7)\n"
        )
        with self.assertRaisesRegex(ArnieError, "compiler diagnostic"):
            benchmark.cmd_bench(self.args)
        self.assertEqual((self.data_dir / "latest").resolve(), self.old)

    def test_invalid_csv_does_not_publish_the_run(self):
        self.compiler.write_text(
            "from pathlib import Path\nPath('timings.csv').write_text('pass,duration_ms\\nParsing,NaN\\n')\n"
        )
        with self.assertRaisesRegex(ArnieError, "invalid timing"):
            benchmark.cmd_bench(self.args)
        self.assertEqual((self.data_dir / "latest").resolve(), self.old)

    def test_spec_stats_are_recorded_separately_from_measured_runs(self):
        stats_script = self.repo / "stats.py"
        stats_script.write_text(
            "from pathlib import Path\nPath('stats.csv').write_text('stat,value\\nTotal Definitions,123\\n')\n"
        )
        self.config["native"]["stats_cmd"] = [
            sys.executable,
            str(stats_script),
            "<SPEC>",
        ]
        self.config["native"]["stats_file"] = "stats.csv"
        self.args.warmup = 0
        benchmark.cmd_bench(self.args)
        output = (self.data_dir / "latest").resolve() / "native" / "rv32i"
        self.assertEqual(len(load_runs(output)), 2)
        self.assertIn("123", (output / "spec-stats.csv").read_text())
        self.assertFalse(list(output.glob("warmup_*.csv")))

    def test_build_failure_preserves_existing_results(self):
        self.args.no_build = False
        self.config["native"]["build_cmd"] = [
            sys.executable,
            "-c",
            "raise SystemExit(3)",
        ]
        with self.assertRaisesRegex(ArnieError, "build failed"):
            benchmark.cmd_bench(self.args)
        self.assertEqual((self.data_dir / "latest").resolve(), self.old)
        self.assertTrue((self.old / "sentinel").exists())

    def test_duplicate_builds_are_run_once(self):
        self.args.build = ["native", "native"]
        benchmark.cmd_bench(self.args)
        self.assertEqual(
            load_metadata((self.data_dir / "latest").resolve())["builds"], ["native"]
        )

    def test_reuse_preserves_original_commit_counts_and_source(self):
        original = self.old / "original" / "rv32i"
        original.mkdir(parents=True)
        (original / "run_1.csv").write_text(
            "pass,duration_ms\nAstSymbolResolverPass,8\n"
        )
        with (self.old / "metadata.toml").open("wb") as f:
            tomli_w.dump(
                {
                    "runs": 1,
                    "warmup": 0,
                    "commits": {"original_vadl_path": "old-original-commit"},
                    "repos": {"original_vadl_path": "/old/repo"},
                },
                f,
            )
        self.config["original"] = {
            **self.config["native"],
            "family": "original",
            "repo_key": "original_vadl_path",
        }
        self.args.build = ["original", "native"]
        self.args.reuse_original = True
        benchmark.cmd_bench(self.args)
        current = (self.data_dir / "latest").resolve()
        meta = load_metadata(current)
        detail = meta["build_details"]["original"]
        self.assertEqual(detail["commit"], "old-original-commit")
        self.assertEqual(detail["runs"], 1)
        self.assertEqual(detail["warmup"], 0)
        self.assertEqual(detail["source_run"], str(self.old.resolve()))
        self.assertEqual(meta["commits"]["original_vadl_path"], "old-original-commit")
        self.assertEqual(meta["repos"]["original_vadl_path"], "/old/repo")
        self.assertEqual(
            load_runs(current / "original" / "rv32i", "original"),
            [{"Name Resolution": 8.0}],
        )
        self.assertTrue(original.exists())
        # Reusing reused results must retain the original counts again.
        benchmark.cmd_bench(self.args)
        detail = load_metadata((self.data_dir / "latest").resolve())["build_details"][
            "original"
        ]
        self.assertEqual((detail["runs"], detail["warmup"]), (1, 0))

    def test_invalid_counts_fail_before_creating_results(self):
        for runs, warmup in [(0, 1), (-1, 1), (1, -1)]:
            with self.subTest(runs=runs, warmup=warmup):
                self.args.runs, self.args.warmup = runs, warmup
                with self.assertRaises(ArnieError):
                    benchmark.cmd_bench(self.args)
        self.assertEqual(
            list(self.data_dir.iterdir()), [self.old, self.data_dir / "latest"]
        )
