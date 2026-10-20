"""Read compiler CSVs and normalize them to a common benchmark model."""

import csv
import math
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from .config import BUILD_CONFIGS, IGNORED_PASSES, PASS_RENAME, SPECS
from .errors import ArnieError

type TimingRun = dict[str, float]
type BenchmarkData = dict[str, dict[str, list[TimingRun]]]
type SpecStats = dict[str, dict[str, int]]


def load_metadata(data_dir: Path) -> dict:
    with (data_dir / "metadata.toml").open("rb") as f:
        return tomllib.load(f)


def read_run(path: Path, family: str = "openvadl") -> TimingRun:
    """Read either compiler's timings, summing passes after name normalization."""
    rename = PASS_RENAME.get(family, {})
    run: TimingRun = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not {"pass", "duration_ms"}.issubset(reader.fieldnames or []):
            raise ArnieError(f"{path}: expected CSV columns 'pass' and 'duration_ms'.")
        for row in reader:
            try:
                name = row["pass"].strip()
                ms = float(row["duration_ms"])
                if not name or not math.isfinite(ms) or ms < 0:
                    raise ValueError(
                        "expected a pass name and a finite, nonnegative duration"
                    )
            except (AttributeError, TypeError, ValueError) as e:
                raise ArnieError(
                    f"{path}:{reader.line_num}: invalid timing: {e}"
                ) from e
            name = rename.get(name, name)
            run[name] = run.get(name, 0.0) + ms
    if not run:
        raise ArnieError(f"{path}: no timings found.")
    return run


def run_files(spec_dir: Path, prefix: str) -> list[Path]:
    """Find numbered runs in execution order, including run_10 after run_9."""
    pattern = re.compile(rf"{re.escape(prefix)}_(\d+)\.csv")
    numbered = [
        (int(match[1]), path)
        for path in spec_dir.glob(f"{prefix}_*.csv")
        if (match := pattern.fullmatch(path.name))
    ]
    return [path for _, path in sorted(numbered)]


def load_runs(spec_dir: Path, family: str = "openvadl") -> list[TimingRun]:
    """Return measured runs only; warmups are excluded."""
    return [read_run(path, family) for path in run_files(spec_dir, "run")]


def load_all_runs_total(spec_dir: Path, family: str = "openvadl") -> list[float]:
    """Return allowed totals in execution order: warmups, then measurements."""
    paths = run_files(spec_dir, "warmup") + run_files(spec_dir, "run")
    return [allowed_total(read_run(path, family)) for path in paths]


def load_benchmark(data_dir: Path) -> tuple[dict, BenchmarkData]:
    """Return (metadata, data), retaining only builds with measured results."""
    meta = load_metadata(data_dir)
    data: BenchmarkData = {}
    for build_dir in sorted(data_dir.iterdir()):
        if not build_dir.is_dir() or build_dir.name == "latest":
            continue
        build = build_dir.name
        family = BUILD_CONFIGS.get(build, {}).get("family", "openvadl")
        specs = {
            spec_dir.name: runs
            for spec_dir in sorted(build_dir.iterdir())
            if spec_dir.is_dir() and (runs := load_runs(spec_dir, family))
        }
        if specs:
            data[build] = specs
    return meta, data


def read_spec_stats(path: Path) -> dict[str, int]:
    stats = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not {"stat", "value"}.issubset(reader.fieldnames or []):
            raise ArnieError(f"{path}: expected CSV columns 'stat' and 'value'.")
        for row in reader:
            try:
                name = row["stat"].strip()
                value = int(row["value"])
                if not name or value < 0 or name in stats:
                    raise ValueError(
                        "expected a unique stat name and a nonnegative integer"
                    )
            except (AttributeError, TypeError, ValueError) as e:
                raise ArnieError(
                    f"{path}:{reader.line_num}: invalid statistic: {e}"
                ) from e
            stats[name] = value
    if not stats:
        raise ArnieError(f"{path}: no statistics found.")
    return stats


def load_spec_stats(data_dir: Path) -> SpecStats:
    """Read the first spec-stats.csv available per specification."""
    stats: SpecStats = {}
    for build_dir in sorted(data_dir.iterdir()):
        if not build_dir.is_dir() or build_dir.name == "latest":
            continue
        for spec_dir in sorted(build_dir.iterdir()):
            csv_path = spec_dir / "spec-stats.csv"
            if spec_dir.is_dir() and spec_dir.name not in stats and csv_path.exists():
                stats[spec_dir.name] = read_spec_stats(csv_path)
    return stats


def _ordered(available: Mapping, preferred: Mapping) -> list[str]:
    return [name for name in preferred if name in available] + sorted(
        set(available) - set(preferred)
    )


def ordered_specs(available: Mapping) -> list[str]:
    return _ordered(available, SPECS)


def ordered_builds(available: Mapping) -> list[str]:
    return _ordered(available, BUILD_CONFIGS)


def benchmark_specs(data: BenchmarkData) -> list[str]:
    """Include specs from every build, even if the first build lacks them."""
    return ordered_specs({spec: None for specs in data.values() for spec in specs})


def _allowed_phase(phase: str) -> bool:
    return phase != "Total" and not any(ignored in phase for ignored in IGNORED_PASSES)


def allowed_total(run: TimingRun) -> float:
    return sum(ms for phase, ms in run.items() if _allowed_phase(phase))


def allowed_phases(data: BenchmarkData) -> list[str]:
    return list(
        dict.fromkeys(
            phase
            for build_data in data.values()
            for runs in build_data.values()
            for run in runs
            for phase in run
            if _allowed_phase(phase)
        )
    )
