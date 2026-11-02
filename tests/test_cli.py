import argparse
import contextlib
import io
import unittest
from unittest.mock import patch

from arnie import cli
from arnie.errors import ArnieError


class CliTests(unittest.TestCase):
    def test_invalid_counts_are_rejected_by_argument_parser(self):
        for args in [
            ["--runs", "0"],
            ["--runs", "-1"],
            ["--warmup", "-1"],
            ["--runs", "oops"],
        ]:
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                cli.build_parser().parse_args(["bench", *args])
            self.assertEqual(error.exception.code, 2)
        args = cli.build_parser().parse_args(["bench", "--warmup", "0", "--runs", "1"])
        self.assertEqual((args.runs, args.warmup), (1, 0))

    def test_compare_parses_refs_and_options(self):
        args = cli.build_parser().parse_args(
            [
                "compare",
                "main",
                "feature-x",
                "--build",
                "graalvm",
                "--runs",
                "5",
                "--warmup",
                "1",
                "--png",
            ]
        )
        self.assertEqual(args.command, "compare")
        self.assertEqual(args.refs, ["main", "feature-x"])
        self.assertEqual(args.build, ["graalvm"])
        self.assertEqual((args.runs, args.warmup), (5, 1))
        self.assertTrue(args.png)

    def test_compare_defaults(self):
        args = cli.build_parser().parse_args(["compare", "main", "v0.1.0"])
        self.assertEqual(args.refs, ["main", "v0.1.0"])
        self.assertIsNone(args.build)
        self.assertEqual((args.runs, args.warmup), (10, 3))
        self.assertFalse(args.png)

    def test_compare_rejects_non_openvadl_builds(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            cli.build_parser().parse_args(
                ["compare", "main", "feature-x", "--build", "original"]
            )
        self.assertEqual(error.exception.code, 2)

    def test_compare_refs_are_optional_at_parse_level(self):
        args = cli.build_parser().parse_args(["compare"])
        self.assertEqual(args.refs, [])
        self.assertFalse(args.plot_only)

    def test_compare_plot_only_flag(self):
        args = cli.build_parser().parse_args(["compare", "--plot-only", "--png"])
        self.assertTrue(args.plot_only)
        self.assertTrue(args.png)
        self.assertEqual(args.refs, [])

    def test_cmd_compare_calls_bench_then_plot(self):
        with (
            patch.object(cli, "cmd_bench") as bench_mock,
            patch.object(cli, "cmd_plot") as plot_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.cmd_compare(
                argparse.Namespace(
                    refs=["main", "feature-x"],
                    build=["graalvm"],
                    runs=5,
                    warmup=2,
                    png=True,
                    plot_only=False,
                )
            )
        bench_mock.assert_called_once()
        bench_args = bench_mock.call_args.args[0]
        self.assertEqual(bench_args.ref, ["main", "feature-x"])
        self.assertEqual(bench_args.build, ["graalvm"])
        self.assertEqual((bench_args.runs, bench_args.warmup), (5, 2))
        self.assertFalse(bench_args.no_build)
        self.assertFalse(bench_args.reuse_original)
        plot_mock.assert_called_once()
        plot_args = plot_mock.call_args.args[0]
        self.assertEqual(plot_args.data, "data/latest")
        self.assertTrue(plot_args.png)

    def test_cmd_compare_requires_at_least_two_refs(self):
        with self.assertRaisesRegex(ArnieError, "at least two"):
            cli.cmd_compare(
                argparse.Namespace(
                    refs=["main"],
                    build=None,
                    runs=10,
                    warmup=3,
                    png=False,
                    plot_only=False,
                )
            )

    def test_cmd_compare_does_not_plot_when_bench_fails(self):
        with (
            patch.object(cli, "cmd_bench", side_effect=ArnieError("build failed")),
            patch.object(cli, "cmd_plot") as plot_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(ArnieError, "build failed"):
                cli.cmd_compare(
                    argparse.Namespace(
                        refs=["main", "feature-x"],
                        build=None,
                        runs=10,
                        warmup=3,
                        png=False,
                        plot_only=False,
                    )
                )
        plot_mock.assert_not_called()

    def test_cmd_compare_plot_only_skips_bench(self):
        with (
            patch.object(cli, "cmd_bench") as bench_mock,
            patch.object(cli, "cmd_plot") as plot_mock,
        ):
            cli.cmd_compare(
                argparse.Namespace(
                    refs=[],
                    build=None,
                    runs=10,
                    warmup=3,
                    png=True,
                    plot_only=True,
                )
            )
        bench_mock.assert_not_called()
        plot_mock.assert_called_once()
        plot_args = plot_mock.call_args.args[0]
        self.assertEqual(plot_args.data, "data/latest")
        self.assertTrue(plot_args.png)

    def test_expected_failure_is_reported_without_traceback(self):
        stderr = io.StringIO()
        with (
            patch.object(
                cli, "cmd_plot", side_effect=FileNotFoundError("pdflatex missing")
            ),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as error,
        ):
            cli.main(["plot"])
        self.assertEqual(error.exception.code, 1)
        self.assertIn("pdflatex missing", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
