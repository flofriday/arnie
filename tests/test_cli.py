import contextlib
import io
import unittest
from unittest.mock import patch

from arnie import cli


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
