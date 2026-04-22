import argparse
import platform
import shutil
import subprocess
import sys
import tomllib
import tomli_w
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = Path("config.toml")
DATA_DIR = Path("data")

SPECS = {
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
        except (EOFError, KeyboardInterrupt):
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
        print(f"Error: openVADL path does not exist: {open_vadl_resolved}", file=sys.stderr)
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

    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
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
                    stderr = result.stderr.decode(errors="replace")
                    print(f"Error: compiler failed on {spec_path}:\n{stderr}", file=sys.stderr)
                    sys.exit(1)

                src = repo / "output" / "timings.csv"
                dst = out_dir / (f"warmup_{i}.csv" if is_warmup else f"run_{i - args.warmup}.csv")
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


def cmd_plot(args: argparse.Namespace) -> None:
    print("plot: not yet implemented")


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

    subparsers.add_parser(
        "plot",
        help="plot benchmark results from data/",
        color=True,
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
