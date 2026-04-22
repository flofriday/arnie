import argparse
import csv
import platform
import shutil
import statistics
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import tomli_w

CONFIG_FILE = Path("config.toml")
DATA_DIR = Path("data")

IGNORED_PASSES = [
    "VDT",
    "ViamVerificationPass",
    "VIAM Creation (pseudo pass)",
    "Lowering to VIAM",
]

SPECS = {
    "miniARMv7": "sys/aarch32/miniARMv7.vadl",
    "sve": "sys/aarch64/sve.vadl",
    "hexagon": "sys/hexagon/hexagon.vadl",
    "rv32i": "sys/risc-v/rv32i.vadl",
}

BUILD_CONFIGS = {
    "default": {
        "build_cmd": ["./gradlew", "installDist"],
        "run_cmd": ["./vadl-cli/build/install/openvadl/bin/openvadl", "check"],
    },
    "native": {
        "build_cmd": ["./gradlew", "nativeCompile"],
        "run_cmd": ["./vadl-cli/build/native/nativeCompile/openvadl", "check"],
    },
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def require_repo() -> Path:
    config = load_config()
    if "open_vadl_path" not in config:
        print("Error: run 'arnie config' first.", file=sys.stderr)
        sys.exit(1)
    return Path(config["open_vadl_path"])


def git_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def cmd_config(args: argparse.Namespace) -> None:
    config = load_config()
    current_open_vadl = config.get("open_vadl_path", "")

    def prompt(label: str, current: str) -> str:
        hint = f" [{current}]" if current else ""
        try:
            value = input(f"{label}{hint}: ").strip()
        except EOFError, KeyboardInterrupt:
            print()
            sys.exit(1)
        return value if value else current

    print("Configure compiler repository path (absolute path).\n")
    open_vadl_path = prompt("openVADL repository path", current_open_vadl)

    if not open_vadl_path:
        print("Error: path is required.", file=sys.stderr)
        sys.exit(1)

    open_vadl_resolved = Path(open_vadl_path).expanduser()
    if not open_vadl_resolved.is_dir():
        print(
            f"Error: openVADL path does not exist: {open_vadl_resolved}",
            file=sys.stderr,
        )
        sys.exit(1)

    config.pop("vadl_path", None)
    config["open_vadl_path"] = str(open_vadl_resolved.resolve())

    with CONFIG_FILE.open("wb") as f:
        tomli_w.dump(config, f)

    print(f"\nSaved config to {CONFIG_FILE.resolve()}")


def cmd_bench(args: argparse.Namespace) -> None:
    repo = require_repo()
    builds = args.build or list(BUILD_CONFIGS.keys())

    commit = git_commit(repo)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    run_dir = DATA_DIR / timestamp

    print(f"openVADL commit: {commit}")
    print(f"Builds: {', '.join(builds)}")
    print(f"Specs:  {', '.join(SPECS.keys())}")
    print(f"Runs:   {args.warmup} warmup + {args.runs} measured\n")

    for d in (DATA_DIR, Path("plots")):
        if d.exists():
            shutil.rmtree(d)
    run_dir.mkdir(parents=True)

    if not args.no_build:
        for build in builds:
            print(f"==> Building {build}...")
            result = subprocess.run(
                BUILD_CONFIGS[build]["build_cmd"],
                cwd=repo,
            )
            if result.returncode != 0:
                print(f"Error: {build} build failed.", file=sys.stderr)
                sys.exit(1)
        print()

    for build in builds:
        run_cmd = BUILD_CONFIGS[build]["run_cmd"]
        for spec_name, spec_path in SPECS.items():
            out_dir = run_dir / build / spec_name
            out_dir.mkdir(parents=True)

            total = args.warmup + args.runs
            for i in range(1, total + 1):
                is_warmup = i <= args.warmup
                if is_warmup:
                    label = f"warmup {i}/{args.warmup}"
                else:
                    label = f"run {i - args.warmup}/{args.runs}"
                print(f"  [{build}/{spec_name}] {label}")

                result = subprocess.run(
                    run_cmd + [spec_path, "--timings-csv"],
                    cwd=repo,
                    capture_output=True,
                )
                if result.returncode != 0:
                    stdout = result.stdout.decode(errors="replace")
                    stderr = result.stderr.decode(errors="replace")
                    output = "\n".join(filter(None, [stdout, stderr]))
                    print(
                        f"Error: compiler failed on {spec_path}:\n{output}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

                src = repo / "output" / "timings.csv"
                dst = out_dir / (
                    f"warmup_{i}.csv" if is_warmup else f"run_{i - args.warmup}.csv"
                )
                shutil.move(src, dst)

    metadata = {
        "timestamp": timestamp,
        "open_vadl_commit": commit,
        "open_vadl_path": str(repo),
        "runs": args.runs,
        "warmup": args.warmup,
        "builds": builds,
        "system": {
            "platform": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }
    with (run_dir / "metadata.toml").open("wb") as f:
        tomli_w.dump(metadata, f)

    latest = DATA_DIR / "latest"
    if latest.is_symlink():
        latest.unlink()
    latest.symlink_to(timestamp, target_is_directory=True)

    print(f"\nDone. Results in {run_dir}")
    print(f"       Symlink: {latest} -> {timestamp}")


def load_runs(spec_dir: Path) -> list[dict[str, float]]:
    """Return one pass->ms dict per measured run (warmup files excluded)."""
    runs = []
    for f in sorted(spec_dir.glob("run_*.csv")):
        with f.open(newline="") as fh:
            reader = csv.DictReader(fh)
            runs.append({row["pass"]: float(row["duration_ms"]) for row in reader})
    return runs


def load_benchmark(data_dir: Path) -> tuple[dict, dict]:
    """Return (metadata, data) where data[build][spec] = list of run dicts."""
    meta_path = data_dir / "metadata.toml"
    if not meta_path.exists():
        print(f"Error: no metadata.toml found in {data_dir}", file=sys.stderr)
        sys.exit(1)
    with meta_path.open("rb") as f:
        meta = tomllib.load(f)

    data: dict[str, dict[str, list[dict[str, float]]]] = {}
    for build_dir in sorted(data_dir.iterdir()):
        if not build_dir.is_dir() or build_dir.name == "latest":
            continue
        build = build_dir.name
        data[build] = {}
        for spec_dir in sorted(build_dir.iterdir()):
            if spec_dir.is_dir():
                runs = load_runs(spec_dir)
                if runs:
                    data[build][spec_dir.name] = runs
    return meta, data


def plot_total_time(data: dict, meta: dict, plots_dir: Path) -> Path:
    import matplotlib.pyplot as plt
    import numpy as np

    builds = list(data.keys())
    specs = list(next(iter(data.values())).keys())
    n_specs = len(specs)
    n_builds = len(builds)

    means = {b: [] for b in builds}
    errs = {b: [] for b in builds}

    def allowed_total(run: dict[str, float]) -> float:
        return sum(
            ms
            for phase, ms in run.items()
            if phase != "Total" and not any(ig in phase for ig in IGNORED_PASSES)
        )

    for build in builds:
        for spec in specs:
            totals = [allowed_total(run) for run in data[build].get(spec, [])]
            means[build].append(statistics.mean(totals) if totals else 0)
            errs[build].append(statistics.stdev(totals) if len(totals) > 1 else 0)

    x = np.arange(n_specs)
    width = 0.35
    offsets = np.linspace(-(n_builds - 1) / 2, (n_builds - 1) / 2, n_builds) * width

    fig, ax = plt.subplots(figsize=(max(6, n_specs * 2), 5))
    for i, build in enumerate(builds):
        ax.bar(
            x + offsets[i],
            means[build],
            width,
            yerr=errs[build],
            capsize=4,
            label=build,
        )

    ax.set_xlabel("Spec")
    ax.set_ylabel("Compile time (ms)")
    ax.set_title("Total compile time — openVADL")
    ax.set_xticks(x)
    ax.set_xticklabels(specs)
    ax.legend()
    ax.set_ylim(bottom=0)
    ax.yaxis.grid(True, color="gray", linewidth=0.4, alpha=0.5)
    ax.set_axisbelow(True)

    commit = meta.get("open_vadl_commit", "")[:12]
    runs = meta.get("runs", "?")
    fig.text(
        0.5,
        -0.02,
        f"commit {commit} · {runs} runs · error bars = stddev",
        ha="center",
        fontsize=8,
        color="gray",
    )
    fig.tight_layout()

    out = plots_dir / "total_time.pdf"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def allowed_phases(data: dict) -> list[str]:
    """Return allowed phases in order of first appearance across all builds/specs."""
    import matplotlib.pyplot as plt  # noqa: ensure matplotlib is available

    phase_order: list[str] = []
    for build_data in data.values():
        for runs in build_data.values():
            for run in runs:
                for phase in run:
                    if (
                        phase != "Total"
                        and phase not in phase_order
                        and not any(ig in phase for ig in IGNORED_PASSES)
                    ):
                        phase_order.append(phase)
    return phase_order


def phase_colors(phase_order: list[str]) -> dict[str, tuple]:
    import matplotlib.pyplot as plt

    cmap = plt.colormaps["tab20"]
    return {
        phase: cmap(i / max(len(phase_order), 1)) for i, phase in enumerate(phase_order)
    }


def plot_phase_breakdown(data: dict, meta: dict, plots_dir: Path) -> list[Path]:
    import matplotlib.pyplot as plt
    import numpy as np

    specs = list(next(iter(data.values())).keys())
    phases = allowed_phases(data)
    colors = phase_colors(phases)
    outputs = []

    for build, build_data in data.items():
        # Phases present in this build
        phase_order: list[str] = []
        for spec in specs:
            for run in build_data.get(spec, []):
                for phase in run:
                    if phase in phases and phase not in phase_order:
                        phase_order.append(phase)

        # Mean duration per phase per spec
        phase_means: dict[str, list[float]] = {p: [] for p in phase_order}
        for spec in specs:
            runs = build_data.get(spec, [])
            for phase in phase_order:
                vals = [r[phase] for r in runs if phase in r]
                phase_means[phase].append(statistics.mean(vals) if vals else 0)

        x = np.arange(len(specs))
        width = 0.6
        fig, ax = plt.subplots(figsize=(max(6, len(specs) * 2), 5))

        bottoms = np.zeros(len(specs))
        for phase in phase_order:
            heights = np.array(phase_means[phase])
            ax.bar(x, heights, width, bottom=bottoms, label=phase, color=colors[phase])
            bottoms += heights

        ax.set_xlabel("Spec")
        ax.set_ylabel("Compile time (ms)")
        ax.set_title(f"Phase breakdown — {build} build")
        ax.set_xticks(x)
        ax.set_xticklabels(specs)
        ax.legend(loc="upper left", bbox_to_anchor=(1, 1), fontsize=7)
        ax.set_ylim(bottom=0)
        ax.yaxis.grid(True, color="gray", linewidth=0.4, alpha=0.5)
        ax.set_axisbelow(True)

        commit = meta.get("open_vadl_commit", "")[:12]
        runs = meta.get("runs", "?")
        fig.text(
            0.5,
            -0.02,
            f"commit {commit} · {runs} runs · averaged across runs",
            ha="center",
            fontsize=8,
            color="gray",
        )
        fig.tight_layout()

        out = plots_dir / f"phase_breakdown_{build}.pdf"
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        outputs.append(out)

    return outputs


def plot_phase_breakdown_combined(data: dict, meta: dict, plots_dir: Path) -> Path:
    import matplotlib.pyplot as plt
    import numpy as np

    builds = list(data.keys())
    specs = list(next(iter(data.values())).keys())
    phase_order = allowed_phases(data)
    colors = phase_colors(phase_order)

    # X positions: one group per spec, bars interleaved by build
    n_specs = len(specs)
    n_builds = len(builds)
    width = 0.35
    offsets = np.linspace(-(n_builds - 1) / 2, (n_builds - 1) / 2, n_builds) * width
    x = np.arange(n_specs)

    fig, ax = plt.subplots(figsize=(max(8, n_specs * 3), 5))

    for bi, build in enumerate(builds):
        build_data = data[build]
        bottoms = np.zeros(n_specs)
        for phase in phase_order:
            heights = np.array(
                [
                    statistics.mean(
                        [r[phase] for r in build_data.get(spec, []) if phase in r]
                        or [0]
                    )
                    for spec in specs
                ]
            )
            ax.bar(
                x + offsets[bi],
                heights,
                width,
                bottom=bottoms,
                color=colors[phase],
                label=phase if bi == 0 else "_nolegend_",
            )
            bottoms += heights

        # Build label below each bar group
        for xi in x:
            ax.text(
                xi + offsets[bi],
                -ax.get_ylim()[1] * 0.03,
                build,
                ha="center",
                va="top",
                fontsize=7,
                color="gray",
            )

    ax.set_xlabel("Spec")
    ax.set_ylabel("Compile time (ms)")
    ax.set_title("Phase breakdown — all builds")
    ax.set_xticks(x)
    ax.set_xticklabels(specs)
    ax.legend(loc="upper left", bbox_to_anchor=(1, 1), fontsize=7)
    ax.set_ylim(bottom=0)
    ax.yaxis.grid(True, color="gray", linewidth=0.4, alpha=0.5)
    ax.set_axisbelow(True)

    commit = meta.get("open_vadl_commit", "")[:12]
    runs = meta.get("runs", "?")
    fig.text(
        0.5,
        -0.02,
        f"commit {commit} · {runs} runs · averaged across runs",
        ha="center",
        fontsize=8,
        color="gray",
    )
    fig.tight_layout()

    out = plots_dir / "phase_breakdown.pdf"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def cmd_plot(args: argparse.Namespace) -> None:
    data_dir = Path(args.data).resolve()
    if not data_dir.exists():
        print(f"Error: data directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    # Resolve symlink (e.g. data/latest -> data/2026-...)
    if data_dir.is_symlink():
        data_dir = data_dir.parent / data_dir.readlink()

    print(f"Loading data from {data_dir}")
    meta, data = load_benchmark(data_dir)

    if not data:
        print("Error: no benchmark data found.", file=sys.stderr)
        sys.exit(1)

    plots_dir = Path("plots")
    plots_dir.mkdir(exist_ok=True)

    out1 = plot_total_time(data, meta, plots_dir)
    print(f"  Written: {out1}")

    for out in plot_phase_breakdown(data, meta, plots_dir):
        print(f"  Written: {out}")

    out = plot_phase_breakdown_combined(data, meta, plots_dir)
    print(f"  Written: {out}")

    print(f"\nDone. Plots in {plots_dir.resolve()}")


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
        help="create or update config.toml with the openVADL repository path",
        color=True,
    )

    bench = subparsers.add_parser(
        "bench",
        help="run benchmarks and store results under data/",
        color=True,
    )
    bench.add_argument(
        "--runs",
        type=int,
        default=10,
        metavar="N",
        help="number of measured runs per spec (default: 10)",
    )
    bench.add_argument(
        "--warmup",
        type=int,
        default=3,
        metavar="N",
        help="number of warmup runs to discard (default: 3)",
    )
    bench.add_argument(
        "--build",
        action="append",
        choices=list(BUILD_CONFIGS.keys()),
        metavar="TYPE",
        help="build type to include: default, native (repeatable; default: both)",
    )
    bench.add_argument(
        "--no-build",
        action="store_true",
        help="skip the gradle build step",
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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    match args.command:
        case "config":
            cmd_config(args)
        case "bench":
            cmd_bench(args)
        case "plot":
            cmd_plot(args)


if __name__ == "__main__":
    main()
