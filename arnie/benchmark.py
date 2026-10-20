"""Build compilers and record benchmark runs without discarding history."""

import argparse
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from .config import BUILD_CONFIGS, DATA_DIR, SPECS, require_repos, resolve_build_env
from .errors import ArnieError
from .results import load_metadata, load_runs, read_run, read_spec_stats


def git_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _run_compiler(
    command: list[str], repo: Path, output: Path, env: dict[str, str] | None
) -> None:
    """Require a fresh output file so stale compiler output cannot become a run."""
    output.unlink(missing_ok=True)
    result = subprocess.run(
        command,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = "\n".join(filter(None, [result.stdout, result.stderr]))
        raise ArnieError(
            f"Compiler command {command!r} failed (exit {result.returncode}):\n{detail}"
        )
    if not output.is_file():
        raise ArnieError(f"Compiler command {command!r} did not produce {output}.")


def cmd_bench(args: argparse.Namespace) -> None:
    if args.runs < 1 or args.warmup < 0:
        raise ArnieError(
            "Use at least one measured run and a nonnegative warmup count."
        )
    builds = list(dict.fromkeys(args.build or BUILD_CONFIGS))
    if args.reuse_original and "original" not in builds:
        raise ArnieError("--reuse-original requires the original build to be selected.")
    active_builds = [b for b in builds if not (args.reuse_original and b == "original")]
    repos = require_repos({BUILD_CONFIGS[b]["repo_key"] for b in active_builds})

    latest = DATA_DIR / "latest"
    if latest.exists() and not latest.is_symlink():
        raise ArnieError(f"{latest} is a directory or file; expected a symlink.")

    reuse_src: Path | None = None
    previous: dict = {}
    if args.reuse_original:
        reuse_src = latest.resolve() / "original"
        if not reuse_src.is_dir():
            raise ArnieError(
                "--reuse-original requires original results in data/latest."
            )
        previous = load_metadata(reuse_src.parent)
        reused_runs = [
            load_runs(spec, "original") for spec in reuse_src.iterdir() if spec.is_dir()
        ]
        if not any(reused_runs):
            raise ArnieError(f"No measured original results found in {reuse_src}.")

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    run_dir = DATA_DIR / timestamp
    commits = {key: git_commit(path) for key, path in repos.items()}
    repo_paths = {key: str(path) for key, path in repos.items()}
    build_details = {
        build: {
            "family": BUILD_CONFIGS[build]["family"],
            "commit": commits[BUILD_CONFIGS[build]["repo_key"]],
            "runs": args.runs,
            "warmup": args.warmup,
        }
        for build in active_builds
    }
    if reuse_src:
        original = dict(previous.get("build_details", {}).get("original", {}))
        original.setdefault("family", "original")
        original.setdefault(
            "commit", previous.get("commits", {}).get("original_vadl_path", "unknown")
        )
        original.setdefault("runs", previous.get("runs", "unknown"))
        original.setdefault("warmup", previous.get("warmup", "unknown"))
        original["source_run"] = str(reuse_src.parent.resolve())
        original.setdefault("system", previous.get("system", {}))
        build_details["original"] = original
        commits["original_vadl_path"] = original["commit"]
        if original_repo := previous.get("repos", {}).get("original_vadl_path"):
            repo_paths["original_vadl_path"] = original_repo
        print(f"original: reusing data from {reuse_src.parent.name}")
    for key, commit in commits.items():
        print(f"{key}: {commit}")
    print(f"Builds: {', '.join(builds)}")
    print(f"Specs:  {', '.join(SPECS)}")
    print(f"Runs:   {args.warmup} warmup + {args.runs} measured\n")

    environments = {
        b: resolve_build_env(BUILD_CONFIGS[b]["build_env"]) for b in active_builds
    }
    if not args.no_build:
        for build in active_builds:
            cfg = BUILD_CONFIGS[build]
            print(f"==> Building {build}...")
            result = subprocess.run(
                cfg["build_cmd"],
                cwd=repos[cfg["repo_key"]],
                env=environments[build],
                check=False,
            )
            if result.returncode != 0:
                raise ArnieError(f"{build} build failed (exit {result.returncode}).")
        print()

    # Keep failed/partial runs for diagnosis; publish latest only after success.
    run_dir.mkdir(parents=True)
    if reuse_src:
        shutil.copytree(reuse_src, run_dir / "original")

    for build in active_builds:
        cfg = BUILD_CONFIGS[build]
        family = cfg["family"]
        repo = repos[cfg["repo_key"]]
        env = environments[build]
        timings_src = repo / cfg["timings_file"]
        stats_src = repo / cfg["stats_file"] if cfg["stats_file"] else None
        for spec_name, family_paths in SPECS.items():
            if family not in family_paths:
                print(f"  [{build}/{spec_name}] (skipped — no spec for {family})")
                continue
            spec_path = str(family_paths[family])
            out_dir = run_dir / build / spec_name
            out_dir.mkdir(parents=True)
            run_cmd = [spec_path if a == "<SPEC>" else a for a in cfg["run_cmd"]]
            stats_cmd = [
                spec_path if a == "<SPEC>" else a for a in cfg.get("stats_cmd", [])
            ]
            if stats_cmd and stats_src:
                print(f"  [{build}/{spec_name}] spec-stats warmup")
                _run_compiler(stats_cmd, repo, stats_src, env)
                read_spec_stats(stats_src)
                shutil.copyfile(stats_src, out_dir / "spec-stats.csv")

            for i in range(1, args.warmup + args.runs + 1):
                is_warmup = i <= args.warmup
                number = i if is_warmup else i - args.warmup
                prefix = "warmup" if is_warmup else "run"
                count = args.warmup if is_warmup else args.runs
                print(f"  [{build}/{spec_name}] {prefix} {number}/{count}")
                _run_compiler(run_cmd, repo, timings_src, env)
                read_run(timings_src, family)
                shutil.move(timings_src, out_dir / f"{prefix}_{number}.csv")

    metadata = {
        "timestamp": timestamp,
        "commits": commits,
        "repos": repo_paths,
        "runs": args.runs,
        "warmup": args.warmup,
        "builds": builds,
        "build_details": build_details,
        "system": {
            "platform": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
    }
    with (run_dir / "metadata.toml").open("wb") as f:
        tomli_w.dump(metadata, f)

    # Replacing a symlink is atomic: a failed run never changes data/latest.
    pending_latest = DATA_DIR / f".latest-{timestamp}"
    try:
        pending_latest.symlink_to(timestamp, target_is_directory=True)
        pending_latest.replace(latest)
    finally:
        pending_latest.unlink(missing_ok=True)
    print(f"\nDone. Results in {run_dir}")
    print(f"       Symlink: {latest} -> {timestamp}")
