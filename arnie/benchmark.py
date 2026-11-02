"""Build compilers and record benchmark runs without discarding history."""

import argparse
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from .config import (
    BUILD_CONFIGS,
    DATA_DIR,
    SPECS,
    build_config,
    openvadl_builds,
    require_repos,
    resolve_build_env,
    split_variant,
    variant_name,
)
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


def git_checkout(repo: Path, ref: str) -> str:
    """Check out a ref and return the resolved commit."""
    result = subprocess.run(
        ["git", "checkout", ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = "\n".join(filter(None, [result.stdout, result.stderr]))
        raise ArnieError(f"Could not checkout {ref!r} in {repo}:\n{detail}")
    return git_commit(repo)


def git_current_ref(repo: Path) -> str:
    """Return the current branch name, or the commit hash when detached."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        branch = result.stdout.strip()
        if branch and branch != "HEAD":
            return branch
    return git_commit(repo)


def git_has_tracked_changes(repo: Path) -> bool:
    """Return True when the working tree has staged or unstaged changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode != 0 or bool(result.stdout.strip())


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

    refs = list(dict.fromkeys(getattr(args, "ref", None) or []))
    if refs:
        if args.reuse_original:
            raise ArnieError("--ref cannot be combined with --reuse-original.")
        if args.no_build:
            raise ArnieError("--ref requires building; remove --no-build.")
        selected = list(dict.fromkeys(args.build or openvadl_builds()))
        non_openvadl = [b for b in selected if build_config(b)["family"] != "openvadl"]
        if non_openvadl:
            raise ArnieError(
                "--ref only applies to OpenVADL builds, not: " + ", ".join(non_openvadl)
            )
        builds = [variant_name(b, ref) for b in selected for ref in refs]
    else:
        builds = list(dict.fromkeys(args.build or BUILD_CONFIGS))

    if args.reuse_original and "original" not in builds:
        raise ArnieError("--reuse-original requires the original build to be selected.")
    active_builds = [b for b in builds if not (args.reuse_original and b == "original")]
    repos = require_repos({build_config(b)["repo_key"] for b in active_builds})

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
    repo_paths = {key: str(path) for key, path in repos.items()}
    commits: dict[str, str] = {}
    for key, path in repos.items():
        if not (refs and key == "open_vadl_path"):
            commits[key] = git_commit(path)

    build_details: dict[str, dict] = {}
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

    # Save the OpenVADL git state so refs can be checked out and restored.
    openvadl_repo = repos.get("open_vadl_path")
    saved_ref: str | None = None
    if refs and openvadl_repo:
        if git_has_tracked_changes(openvadl_repo):
            raise ArnieError(
                f"{openvadl_repo} has uncommitted changes. "
                "Commit or stash them before using --ref."
            )
        saved_ref = git_current_ref(openvadl_repo)
        print(f"OpenVADL repo: {openvadl_repo} (on: {saved_ref})")
        print(f"Refs:   {', '.join(refs)}")
    else:
        for key, commit in commits.items():
            print(f"{key}: {commit}")
    print(f"Builds: {', '.join(builds)}")
    print(f"Specs:  {', '.join(SPECS)}")
    print(f"Runs:   {args.warmup} warmup + {args.runs} measured\n")

    # Resolve environments per base build so a failure surfaces before any work.
    environments: dict[str, dict[str, str] | None] = {}
    for build in active_builds:
        base, _ = split_variant(build)
        if base not in environments:
            environments[base] = resolve_build_env(build_config(build)["build_env"])

    # Keep failed/partial runs for diagnosis; publish latest only after success.
    run_dir.mkdir(parents=True)
    if reuse_src:
        shutil.copytree(reuse_src, run_dir / "original")

    restore_error: ArnieError | None = None
    try:
        for build in active_builds:
            cfg = build_config(build)
            base, ref = split_variant(build)
            family = cfg["family"]
            repo = repos[cfg["repo_key"]]
            env = environments[base]

            if ref:
                # Undo the '/' -> '~' sanitization so git gets the real ref.
                git_ref = ref.replace("~", "/")
                print(f"==> Checking out {git_ref}...")
                commit = git_checkout(repo, git_ref)
            else:
                commit = commits.setdefault(cfg["repo_key"], git_commit(repo))

            if not args.no_build:
                print(f"==> Building {build}...")
                build_cmd = cfg["build_cmd"]
                if ref and build_cmd[0].endswith("gradlew"):
                    build_cmd = [build_cmd[0], "clean", *build_cmd[1:]]
                result = subprocess.run(
                    build_cmd,
                    cwd=repo,
                    env=env,
                    check=False,
                )
                if result.returncode != 0:
                    raise ArnieError(
                        f"{build} build failed (exit {result.returncode})."
                    )
                print()

            build_details[build] = {
                "family": family,
                "commit": commit,
                "runs": args.runs,
                "warmup": args.warmup,
            }
            if ref:
                build_details[build]["ref"] = ref.replace("~", "/")

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
    finally:
        if saved_ref and openvadl_repo:
            print(f"==> Restoring {saved_ref}...")
            try:
                git_checkout(openvadl_repo, saved_ref)
                commits["open_vadl_path"] = git_commit(openvadl_repo)
            except ArnieError as e:
                restore_error = e
                print(f"Warning: could not restore {saved_ref}: {e}", file=sys.stderr)

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

    if restore_error is not None:
        raise ArnieError(
            f"Results were saved, but the OpenVADL repository could not be "
            f"restored to {saved_ref}. Restore it manually."
        )
