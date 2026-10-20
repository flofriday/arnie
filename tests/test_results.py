import tempfile
import unittest
from pathlib import Path

from arnie.errors import ArnieError
from arnie.results import (
    allowed_total,
    benchmark_specs,
    load_all_runs_total,
    load_benchmark,
    load_runs,
    read_run,
    read_spec_stats,
)


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def csv(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_resolution_aliases_and_repeated_passes_are_summed(self):
        path = self.csv(
            "run_1.csv",
            "pass,duration_ms\nSymbol Resolution,2\nName Resolution,3\nParsing,4\nParsing,5\n",
        )
        self.assertEqual(read_run(path), {"Name Resolution": 5.0, "Parsing": 9.0})

    def test_original_resolution_passes_use_the_same_canonical_name(self):
        path = self.csv(
            "run_1.csv",
            "run,pass,duration_ms,percentage\n1,AstSymbolRegistrationPass,2,20\n2,AstSymbolResolverPass,3,30\n3,Total,10,100\n",
        )
        run = read_run(path, "original")
        self.assertEqual(run["Name Resolution"], 5.0)
        self.assertEqual(allowed_total(run), 5.0)

    def test_new_resolution_name_is_unchanged(self):
        path = self.csv("run_1.csv", "pass,duration_ms\nName Resolution,2\n")
        self.assertEqual(read_run(path), {"Name Resolution": 2.0})

    def test_warmups_excluded_from_measurements_and_ordered_numerically(self):
        for name, duration in [
            ("warmup_10", 110),
            ("warmup_2", 102),
            ("warmup_1", 101),
            ("run_10", 10),
            ("run_2", 2),
            ("run_1", 1),
        ]:
            self.csv(
                f"{name}.csv",
                f"pass,duration_ms\nParsing,{duration}\nTotal,999\nLowering to VIAM,80\n",
            )
        self.csv("run_notes.csv", "not a run")
        self.assertEqual([r["Parsing"] for r in load_runs(self.root)], [1, 2, 10])
        self.assertEqual(load_all_runs_total(self.root), [101, 102, 110, 1, 2, 10])

    def test_malformed_timings_have_file_and_row_context(self):
        for content in [
            "pass,other\nParsing,1\n",
            "pass,duration_ms\n",
            "",
            "pass,duration_ms\n,1\n",
            "pass,duration_ms\nParsing\n",
        ]:
            with self.subTest(content=content):
                path = self.csv("bad.csv", content)
                with self.assertRaisesRegex(ArnieError, "bad.csv"):
                    read_run(path)
        for duration in ["NaN", "inf", "-inf", "-1", "nope"]:
            with self.subTest(duration=duration):
                path = self.csv("bad.csv", f"pass,duration_ms\nParsing,{duration}\n")
                with self.assertRaisesRegex(ArnieError, "bad.csv:2"):
                    read_run(path)

    def test_unknown_passes_remain_visible(self):
        path = self.csv("run_1.csv", "pass,duration_ms\nFuture Phase,4\n")
        self.assertEqual(read_run(path), {"Future Phase": 4.0})

    def test_stats_validate_schema_values_and_duplicates(self):
        path = self.csv("stats.csv", "stat,value\nTotal Definitions,3\n")
        self.assertEqual(read_spec_stats(path), {"Total Definitions": 3})
        for content in [
            "stat,value\n",
            "other,value\na,1\n",
            "stat,value\na,-1\n",
            "stat,value\na,1\na,2\n",
        ]:
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ArnieError):
                    read_spec_stats(path)

    def test_specs_are_the_union_of_all_builds_including_unregistered_specs(self):
        data = {"original": {"rv32i": []}, "native": {"huge": [], "future-spec": []}}
        self.assertEqual(benchmark_specs(data), ["rv32i", "huge", "future-spec"])

    def test_load_benchmark_excludes_empty_builds_and_warmup_only_specs(self):
        self.csv("metadata.toml", 'builds = ["native"]\n')
        (self.root / "original").mkdir()
        self.csv("native/rv32i/warmup_1.csv", "pass,duration_ms\nParsing,9\n")
        self.csv("native/huge/run_1.csv", "pass,duration_ms\nSymbol Resolution,2\n")
        _, data = load_benchmark(self.root)
        self.assertEqual(data, {"native": {"huge": [{"Name Resolution": 2.0}]}})
