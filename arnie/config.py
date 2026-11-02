"""Compiler definitions, specification paths, and local configuration."""

import argparse
import os
import subprocess
import tomllib
from pathlib import Path
from typing import NotRequired, TypedDict

import tomli_w

from .errors import ArnieError

CONFIG_FILE = Path("config.toml")
DATA_DIR = Path("data")


class BuildConfig(TypedDict):
    family: str
    repo_key: str
    build_cmd: list[str]
    build_env: str | dict[str, str] | None
    run_cmd: list[str]
    stats_cmd: NotRequired[list[str]]
    timings_file: str
    stats_file: str | None


IGNORED_PASSES = [
    "VDT",
    "ViamVerificationPass",
    "VIAM Creation (pseudo pass)",
    "Lowering to VIAM",
    "hidden",
]

_HERE = Path(__file__).resolve().parent.parent

# Per-spec paths, keyed by compiler family. Specs without an entry for a given
# family are skipped for builds belonging to that family.
SPECS: dict[str, dict[str, str | Path]] = {
    "rv32i": {
        "openvadl": "sys/risc-v/rv32i.vadl",
        "original": "sys/risc-v/newsrc/rv32i.vadl",
    },
    "ppc64": {
        "openvadl": "sys/ppc64/ppc64.vadl",
    },
    "hexagon": {
        "openvadl": "sys/hexagon/hexagon.vadl",
        "original": "sys/hexagon/src/hexagon.vadl",
    },
    "aarch64": {
        "original": "sys/aarch64/src/aarch64.vadl",
        "openvadl": "sys/aarch64/aarch64.vadl",
    },
    "miniARMv7": {
        "openvadl": _HERE / "secret_specs" / "miniARMv7.vadl",
        "original": "sys/aarch32/src/miniARMv7.vadl",
    },
    "aarch32": {
        "openvadl": "sys/aarch32/aarch32.vadl",
    },
    "aarch32-expanded": {
        "openvadl": _HERE / "secret_specs" / "aarch32-expanded.vadl",
    },
    "huge": {
        "openvadl": "sys/huge/huge.vadl",
    },
    "huge-expanded": {
        "openvadl": _HERE / "secret_specs" / "huge-expanded.vadl",
    },
}

# Each build defines its own family, repo, build/run commands, and where it
# writes its timings/stats CSVs (relative to the repo cwd).
# `<SPEC>` in run_cmd/stats_cmd is replaced with the resolved spec path at run time.

BUILD_CONFIGS: dict[str, BuildConfig] = {
    "original": {
        "family": "original",
        "repo_key": "original_vadl_path",
        "build_cmd": ["make", "build"],
        "build_env": "java17",  # resolved lazily via /usr/libexec/java_home -v17
        "run_cmd": ["./obj/bin/vadl", "--pass-stats-csv=timings.csv", "<SPEC>"],
        "timings_file": "timings.csv",
        "stats_file": None,
    },
    "graalvm": {
        "family": "openvadl",
        "repo_key": "open_vadl_path",
        "build_cmd": ["./gradlew", "installDist"],
        "build_env": None,
        "run_cmd": [
            "./vadl-cli/build/install/openvadl/bin/openvadl",
            "check",
            "--decoder",
            "skip=all",
            "<SPEC>",
            "--timings-csv",
        ],
        "stats_cmd": [
            "./vadl-cli/build/install/openvadl/bin/openvadl",
            "check",
            "--decoder",
            "skip=all",
            "<SPEC>",
            "--spec-stats-csv",
        ],
        "timings_file": "output/timings.csv",
        "stats_file": "output/spec-stats.csv",
    },
    "native": {
        "family": "openvadl",
        "repo_key": "open_vadl_path",
        "build_cmd": ["./gradlew", "nativeCompile"],
        "build_env": None,
        "run_cmd": [
            "./vadl-cli/build/native/nativeCompile/openvadl",
            "check",
            "--decoder",
            "skip=all",
            "<SPEC>",
            "--timings-csv",
        ],
        "stats_cmd": [
            "./vadl-cli/build/native/nativeCompile/openvadl",
            "check",
            "--decoder",
            "skip=all",
            "<SPEC>",
            "--spec-stats-csv",
        ],
        "timings_file": "output/timings.csv",
        "stats_file": "output/spec-stats.csv",
    },
}

# Maps a build family's raw pass names to canonical (openVADL) names. Passes not
# in the map are kept as-is. Passes mapped to the same canonical name are summed.
PASS_RENAME: dict[str, dict[str, str]] = {
    "openvadl": {"Symbol Resolution": "Name Resolution"},
    "original": {
        "ConfigurationPass": "hidden",
        "SourceToCstPass": "Parsing",
        "CstResourcePass": "Parsing",
        "CstTemplateRegistrationPass": "Parsing",
        "CstModelReplacementPass": "Model Removing",
        "CstModelInstancePass": "Macro Expanding",
        "CstValidationPass": "Parsing",
        "CstToAstPass": "Parsing",
        "AstSourceLocationPass": "Parsing",
        "AstAnnotationPass": "Parsing",
        "AstSymbolRegistrationPass": "Name Resolution",
        "AstModuleLoaderPass": "Parsing",
        "AstModuleMergerPass": "Modul Merging",
        "AstPipelineSplittingPass": "Parsing",
        "AstSymbolResolverPass": "Name Resolution",
        "AstRAIIPass": "RAII",
        "AstRecursiveCallDetectionPass": "Type Checking",
        "DefaultGrammarInjectionPass": "Parsing",
        "AstOperationAnnotationPass": "Parsing",
        "AstTypeInferencePass": "Type Checking",
        "AstLoweringPass": "hidden",
        "AstValidationPass": "hidden",
        "Total": "hidden",
    },
}


def split_variant(name: str) -> tuple[str, str | None]:
    """Split a variant name like 'graalvm@main' into ('graalvm', 'main').

    Plain build names return (name, None). Longer build names are matched
    first so prefixes cannot shadow each other.
    """
    for build in sorted(BUILD_CONFIGS, key=len, reverse=True):
        prefix = f"{build}@"
        if name.startswith(prefix):
            return build, name[len(prefix) :]
    return name, None


def variant_name(build: str, ref: str) -> str:
    """Create a variant directory name from a base build and a git ref.

    '/' in refs is replaced with '~' (which git forbids in ref names, so no
    collision is possible) to keep the variant a single directory level.
    """
    return f"{build}@{ref.replace('/', '~')}"


def display_name(name: str) -> str:
    """Human-readable form of a build or variant name (undoes variant_name)."""
    base, ref = split_variant(name)
    if ref is not None:
        return f"{base}@{ref.replace('~', '/')}"
    return name


def build_config(name: str) -> BuildConfig:
    """Return the BuildConfig for a build name or variant name."""
    base, _ = split_variant(name)
    return BUILD_CONFIGS[base]


def build_family(name: str) -> str:
    """Return the compiler family for a build name or variant name."""
    return build_config(name)["family"]


def openvadl_builds() -> list[str]:
    """Return all build names whose family is OpenVADL."""
    return [b for b in BUILD_CONFIGS if BUILD_CONFIGS[b]["family"] == "openvadl"]


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def require_repos(repo_keys: set[str]) -> dict[str, Path]:
    """Resolve and validate only the repositories needed for this invocation."""
    if not repo_keys:
        return {}
    config = load_config()
    missing = sorted(k for k in repo_keys if not config.get(k))
    if missing:
        raise ArnieError(
            f"Missing config keys: {', '.join(missing)}. Run 'arnie config' first."
        )
    repos = {}
    for key in sorted(repo_keys):
        if not isinstance(config[key], str):
            raise ArnieError(f"{key} must be a repository path string.")
        path = Path(config[key]).expanduser().resolve()
        if not path.is_dir():
            raise ArnieError(f"Repository directory does not exist: {path}")
        repos[key] = path
    return repos


def cmd_config(args: argparse.Namespace) -> None:
    config = load_config()

    def prompt(label: str, current: str) -> str:
        hint = f" [{current}]" if current else ""
        try:
            value = input(f"{label}{hint}: ").strip()
        except EOFError as e:
            raise ArnieError(
                "Configuration input ended before paths were supplied."
            ) from e
        return value if value else current

    def resolve_dir(label: str, raw: str) -> Path:
        if not raw:
            raise ArnieError(f"{label} is required.")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise ArnieError(f"{label} does not exist: {path}")
        return path.resolve()

    print("Configure compiler repository paths (absolute paths).\n")
    open_vadl = prompt("openVADL repository path", config.get("open_vadl_path", ""))
    original_vadl = prompt(
        "Original VADL repository path", config.get("original_vadl_path", "")
    )

    config.pop("vadl_path", None)
    config["open_vadl_path"] = str(resolve_dir("openVADL path", open_vadl))
    config["original_vadl_path"] = str(resolve_dir("Original VADL path", original_vadl))

    with CONFIG_FILE.open("wb") as f:
        tomli_w.dump(config, f)

    print(f"\nSaved config to {CONFIG_FILE.resolve()}")


def resolve_build_env(env_spec: str | dict[str, str] | None) -> dict[str, str] | None:
    if env_spec is None:
        return None
    if env_spec == "java17":
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v17"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ArnieError(
                "Could not find Java 17 via /usr/libexec/java_home -v17.\n"
                f"{result.stderr}"
            )
        return {**os.environ, "JAVA_HOME": result.stdout.strip()}
    if isinstance(env_spec, dict):
        return {**os.environ, **env_spec}
    raise ArnieError(f"Unknown build_env spec: {env_spec!r}")
