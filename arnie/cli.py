"""Command-line argument parsing and error reporting."""

import argparse
import tomllib

from .benchmark import cmd_bench
from .config import BUILD_CONFIGS, cmd_config
from .errors import ArnieError
from .plotting import cmd_plot


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arnie",
        description="Benchmark tool for openVADL compiler performance.",
        color=True,
    )
    subparsers = parser.add_subparsers(title="commands", dest="command")
    subparsers.required = True

    subparsers.add_parser(
        "config",
        help="create or update config.toml with the compiler repository paths",
        color=True,
    )

    bench = subparsers.add_parser(
        "bench",
        help="run benchmarks and store results under data/",
        color=True,
    )
    bench.add_argument(
        "--runs",
        type=_positive_int,
        default=10,
        metavar="N",
        help="number of measured runs per spec (default: 10)",
    )
    bench.add_argument(
        "--warmup",
        type=_nonnegative_int,
        default=3,
        metavar="N",
        help="number of warmup runs to discard (default: 3)",
    )
    bench.add_argument(
        "--build",
        action="append",
        choices=list(BUILD_CONFIGS.keys()),
        metavar="TYPE",
        help=(
            "build to include: "
            + ", ".join(BUILD_CONFIGS.keys())
            + " (repeatable; default: all)"
        ),
    )
    bench.add_argument(
        "--no-build",
        action="store_true",
        help="skip compiler build steps",
    )
    bench.add_argument(
        "--reuse-original",
        action="store_true",
        help="copy original build data from data/latest instead of re-running it",
    )

    plot = subparsers.add_parser(
        "plot",
        help="plot benchmark results from data/",
        color=True,
    )
    plot.add_argument(
        "--data",
        default="data/latest",
        metavar="PATH",
        help="benchmark run directory to plot (default: data/latest)",
    )
    plot.add_argument(
        "--png",
        action="store_true",
        help="also rasterize PDF pages to PNG via Ghostscript or Poppler",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        match args.command:
            case "config":
                cmd_config(args)
            case "bench":
                cmd_bench(args)
            case "plot":
                cmd_plot(args)
    except (ArnieError, OSError, tomllib.TOMLDecodeError) as e:
        parser.exit(1, f"Error: {e}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nInterrupted.\n")
