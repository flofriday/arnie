"""Command-line argument parsing and error reporting."""

import argparse
import tomllib

from .benchmark import cmd_bench
from .config import BUILD_CONFIGS, cmd_config, openvadl_builds
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

    compare = subparsers.add_parser(
        "compare",
        help="benchmark OpenVADL at multiple git refs and plot the comparison",
        color=True,
    )
    compare.add_argument(
        "refs",
        nargs="*",
        metavar="REF",
        help="OpenVADL git refs (branches, tags, or commits) to compare",
    )
    compare.add_argument(
        "--build",
        action="append",
        choices=openvadl_builds(),
        metavar="TYPE",
        help=(
            "build to include: "
            + ", ".join(openvadl_builds())
            + " (repeatable; default: all OpenVADL builds)"
        ),
    )
    compare.add_argument(
        "--runs",
        type=_positive_int,
        default=10,
        metavar="N",
        help="number of measured runs per spec (default: 10)",
    )
    compare.add_argument(
        "--warmup",
        type=_nonnegative_int,
        default=3,
        metavar="N",
        help="number of warmup runs to discard (default: 3)",
    )
    compare.add_argument(
        "--png",
        action="store_true",
        help="also rasterize PDF pages to PNG via Ghostscript or Poppler",
    )
    compare.add_argument(
        "--plot-only",
        action="store_true",
        help="skip benchmarking and only re-plot data/latest",
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


def cmd_compare(args: argparse.Namespace) -> None:
    """Benchmark OpenVADL at multiple git refs, then plot the comparison."""
    if args.plot_only:
        cmd_plot(argparse.Namespace(data="data/latest", png=args.png))
        return
    if len(args.refs) < 2:
        raise ArnieError("compare requires at least two refs (or use --plot-only).")
    print(f"Comparing OpenVADL refs: {', '.join(args.refs)}\n")
    cmd_bench(
        argparse.Namespace(
            build=args.build,
            runs=args.runs,
            warmup=args.warmup,
            no_build=False,
            reuse_original=False,
            ref=args.refs,
        )
    )
    cmd_plot(argparse.Namespace(data="data/latest", png=args.png))


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        match args.command:
            case "config":
                cmd_config(args)
            case "bench":
                cmd_bench(args)
            case "compare":
                cmd_compare(args)
            case "plot":
                cmd_plot(args)
    except (ArnieError, OSError, tomllib.TOMLDecodeError) as e:
        parser.exit(1, f"Error: {e}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nInterrupted.\n")
