import argparse
import csv
import os
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

MACHINE = "Apple M3 Max (36\\,GB)"

IGNORED_PASSES = [
    "VDT",
    "ViamVerificationPass",
    "VIAM Creation (pseudo pass)",
    "Lowering to VIAM",
    "hidden",
]

_HERE = Path(__file__).parent

# Per-spec paths, keyed by compiler family. Specs without an entry for a given
# family are skipped for builds belonging to that family.
SPECS: dict[str, dict[str, str | Path]] = {
    "rv32i": {
        "openvadl": "sys/risc-v/rv32i.vadl",
    },
    "ppc64": {
        "openvadl": "sys/ppc64/ppc64.vadl",
    },
    "hexagon": {
        "openvadl": "sys/hexagon/hexagon.vadl",
        "original": "sys/hexagon/src/hexagon.vadl",
    },
    "sve": {
        "openvadl": "sys/aarch64/sve.vadl",
    },
    "miniARMv7": {
        "openvadl": _HERE / "secret_specs" / "miniARMv7.vadl",
        "original": "sys/aarch32/src/miniARMv7.vadl",
    },
}

# Each build defines its own family, repo, build/run commands, and where it
# writes its timings/stats CSVs (relative to the repo cwd).
# `<SPEC>` in run_cmd is replaced with the resolved spec path at run time.

BUILD_CONFIGS: dict[str, dict] = {
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
            "--spec-stats-csv",
        ],
        "timings_file": "output/timings.csv",
        "stats_file": "output/spec-stats.csv",
    },
}

# Maps a build family's raw pass names to canonical (openVADL) names. Passes not
# in the map are kept as-is. Passes mapped to the same canonical name are summed.
PASS_RENAME: dict[str, dict[str, str]] = {
    "openvadl": {},  # canonical, no renaming
    "original": {
        # TODO: populate after first 'arnie bench' captures real names
        # "Original Pass Name": "OpenVADL Phase Name",
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
        "AstSymbolRegistrationPass": "Symbol Resolution",
        "AstModuleLoaderPass": "Parsing",
        "AstModuleMergerPass": "Modul Merging",
        "AstPipelineSplittingPass": "Parsing",
        "AstSymbolResolverPass": "Symbol Resolution",
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


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def require_repos(repo_keys: set[str]) -> dict[str, Path]:
    """Return a {repo_key: Path} map for the given keys; exits if any are missing."""
    config = load_config()
    missing = [k for k in repo_keys if k not in config]
    if missing:
        print(
            f"Error: missing config keys: {', '.join(missing)}. "
            "Run 'arnie config' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return {k: Path(config[k]) for k in repo_keys}


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

    def prompt(label: str, current: str) -> str:
        hint = f" [{current}]" if current else ""
        try:
            value = input(f"{label}{hint}: ").strip()
        except EOFError, KeyboardInterrupt:
            print()
            sys.exit(1)
        return value if value else current

    def resolve_dir(label: str, raw: str) -> Path:
        if not raw:
            print(f"Error: {label} is required.", file=sys.stderr)
            sys.exit(1)
        path = Path(raw).expanduser()
        if not path.is_dir():
            print(f"Error: {label} does not exist: {path}", file=sys.stderr)
            sys.exit(1)
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


def _resolve_build_env(env_spec) -> dict | None:
    if env_spec is None:
        return None
    if env_spec == "java17":
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v17"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                "Error: could not find Java 17 via /usr/libexec/java_home -v17.\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            sys.exit(1)
        return {**os.environ, "JAVA_HOME": result.stdout.strip()}
    if isinstance(env_spec, dict):
        return {**os.environ, **env_spec}
    raise ValueError(f"unknown build_env spec: {env_spec!r}")


def _spec_path_for(spec_raw: str | Path) -> str:
    """Resolve a spec path: absolute Paths kept as-is, strings stay repo-relative."""
    p = Path(spec_raw)
    return str(p.resolve()) if p.is_absolute() else str(spec_raw)


def cmd_bench(args: argparse.Namespace) -> None:
    builds = args.build or list(BUILD_CONFIGS.keys())
    repo_keys = {BUILD_CONFIGS[b]["repo_key"] for b in builds}
    repos = require_repos(repo_keys)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    run_dir = DATA_DIR / timestamp

    commits = {key: git_commit(path) for key, path in repos.items()}
    for key, commit in commits.items():
        print(f"{key}: {commit}")
    print(f"Builds: {', '.join(builds)}")
    print(f"Specs:  {', '.join(SPECS.keys())}")
    print(f"Runs:   {args.warmup} warmup + {args.runs} measured\n")

    for d in (DATA_DIR, Path("plots")):
        if d.exists():
            shutil.rmtree(d)
    run_dir.mkdir(parents=True)

    if not args.no_build:
        for build in builds:
            cfg = BUILD_CONFIGS[build]
            repo = repos[cfg["repo_key"]]
            env = _resolve_build_env(cfg["build_env"])
            print(f"==> Building {build}...")
            result = subprocess.run(cfg["build_cmd"], cwd=repo, env=env)
            if result.returncode != 0:
                print(f"Error: {build} build failed.", file=sys.stderr)
                sys.exit(1)
        print()

    for build in builds:
        cfg = BUILD_CONFIGS[build]
        family = cfg["family"]
        repo = repos[cfg["repo_key"]]
        run_cmd_template = cfg["run_cmd"]
        timings_src = repo / cfg["timings_file"]
        stats_src = repo / cfg["stats_file"] if cfg["stats_file"] else None

        for spec_name, family_paths in SPECS.items():
            if family not in family_paths:
                print(f"  [{build}/{spec_name}] (skipped — no spec for {family})")
                continue

            spec_path = _spec_path_for(family_paths[family])
            out_dir = run_dir / build / spec_name
            out_dir.mkdir(parents=True)

            run_cmd = [spec_path if a == "<SPEC>" else a for a in run_cmd_template]

            total = args.warmup + args.runs
            for i in range(1, total + 1):
                is_warmup = i <= args.warmup
                label = (
                    f"warmup {i}/{args.warmup}"
                    if is_warmup
                    else f"run {i - args.warmup}/{args.runs}"
                )
                print(f"  [{build}/{spec_name}] {label}")

                result = subprocess.run(
                    run_cmd,
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

                dst = out_dir / (
                    f"warmup_{i}.csv" if is_warmup else f"run_{i - args.warmup}.csv"
                )
                shutil.move(timings_src, dst)

                if i == 1 and stats_src and stats_src.exists():
                    shutil.copy(stats_src, out_dir / "spec-stats.csv")

    metadata = {
        "timestamp": timestamp,
        "commits": commits,
        "repos": {k: str(v) for k, v in repos.items()},
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


def load_runs(spec_dir: Path, family: str = "openvadl") -> list[dict[str, float]]:
    """Return one pass->ms dict per measured run (warmup files excluded).

    Pass names are renamed via PASS_RENAME[family]; passes that map to the same
    canonical name are summed.
    """
    rename = PASS_RENAME.get(family, {})
    runs = []
    for f in sorted(spec_dir.glob("run_*.csv")):
        with f.open(newline="") as fh:
            reader = csv.DictReader(fh)
            run: dict[str, float] = {}
            for row in reader:
                name = rename.get(row["pass"], row["pass"])
                run[name] = run.get(name, 0.0) + float(row["duration_ms"])
            runs.append(run)
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
        family = BUILD_CONFIGS.get(build, {}).get("family", "openvadl")
        data[build] = {}
        for spec_dir in sorted(build_dir.iterdir()):
            if spec_dir.is_dir():
                runs = load_runs(spec_dir, family)
                if runs:
                    data[build][spec_dir.name] = runs
    return meta, data


def load_spec_stats(data_dir: Path) -> dict[str, dict[str, int]]:
    """Return spec_name -> {stat: value} from the first spec-stats.csv found per spec."""
    stats: dict[str, dict[str, int]] = {}
    for build_dir in sorted(data_dir.iterdir()):
        if not build_dir.is_dir():
            continue
        for spec_dir in sorted(build_dir.iterdir()):
            if not spec_dir.is_dir() or spec_dir.name in stats:
                continue
            csv_path = spec_dir / "spec-stats.csv"
            if csv_path.exists():
                with csv_path.open(newline="") as fh:
                    reader = csv.DictReader(fh)
                    stats[spec_dir.name] = {
                        row["stat"]: int(row["value"]) for row in reader
                    }
    return stats


def _specs_ordered(available: dict) -> list[str]:
    """Return spec names in SPECS declaration order, filtering to those present."""
    present = set(available.keys())
    return [s for s in SPECS if s in present]


def _builds_ordered(available: dict) -> list[str]:
    """Return build names in BUILD_CONFIGS declaration order, filtering to those present."""
    present = set(available.keys())
    return [b for b in BUILD_CONFIGS if b in present]


def _arnie_header() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"% Automatically generated by arnie.py at {ts}\n"


# pgfplots fill colour + hatch pattern per series index
PGF_STYLES = [
    ("blue!40!white", "north east lines"),
    ("red!40!white", "north west lines"),
    ("green!50!black!40", "crosshatch"),
    ("orange!60!white", "horizontal lines"),
    ("purple!40!white", "vertical lines"),
    ("teal!50!white", "dots"),
    ("brown!40!white", "grid"),
    ("cyan!40!white", "crosshatch dots"),
    ("yellow!60!white", "bricks"),
    ("magenta!40!white", "fivepointed stars"),
    ("olive!50!white", "sixpointed stars"),
]

# Preamble for plots.tex — the single compiled document
_PLOTS_TEX = r"""\documentclass[a4paper,12pt]{article}
\usepackage[a4paper,margin=2.5cm]{geometry}
\usepackage{float}
\usepackage{booktabs}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}
\usetikzlibrary{patterns}
\usepgfplotslibrary{groupplots}
% Required packages when including individual snippets in a paper:
%   \usepackage{pgfplots}, \usetikzlibrary{patterns},
%   \usepgfplotslibrary{groupplots}
"""

_AXIS_BASE = (
    "    ymajorgrids=true,\n"
    "    grid style={dashed,gray!30},\n"
    "    ymin=0,\n"
    "    tick align=inside,\n"
    "    xtick align=inside,\n"
)


def _pgf_style(i: int) -> str:
    fill, pattern = PGF_STYLES[i % len(PGF_STYLES)]
    return f"fill={fill}, postaction={{pattern={pattern}}}"


def _allowed_total(run: dict[str, float]) -> float:
    return sum(
        ms
        for phase, ms in run.items()
        if phase != "Total" and not any(ig in phase for ig in IGNORED_PASSES)
    )


def allowed_phases(data: dict) -> list[str]:
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


def gen_preamble_tex(meta: dict, plots_dir: Path) -> Path:
    commits = meta.get("commits", {})
    open_commit = commits.get("open_vadl_path", "unknown")[:12]
    orig_commit = commits.get("original_vadl_path", "unknown")[:12]
    runs_n = meta.get("runs", "?")
    warmup_n = meta.get("warmup", "?")

    tex = (
        _arnie_header()
        + f"All benchmarks were recorded on an {MACHINE}.\n"
        + f"OpenVADL was checked out at git commit \\texttt{{{open_commit}}} "
        + f"and Original VADL at \\texttt{{{orig_commit}}}.\n"
        + f"Each specification was measured over ${runs_n}$~timed runs "
        + f"after ${warmup_n}$~warmup runs.\n"
    )
    out = plots_dir / "preamble.tex"
    out.write_text(tex)
    return out


def compile_tex(tex_path: Path) -> Path:
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", tex_path.name],
        cwd=tex_path.parent,
        capture_output=True,
        text=True,
    )
    pdf_path = tex_path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        log = tex_path.with_suffix(".log")
        detail = (
            log.read_text(errors="replace")[-3000:] if log.exists() else result.stdout
        )
        print(f"pdflatex error for {tex_path.name}:\n{detail}", file=sys.stderr)
        sys.exit(1)
    return pdf_path


def gen_total_time_tex(data: dict, meta: dict, plots_dir: Path) -> Path:
    builds = _builds_ordered(data)
    specs = _specs_ordered(next(iter(data.values())))

    means: dict[str, list[float]] = {}
    errs: dict[str, list[float]] = {}
    for build in builds:
        totals_per_spec = [
            [_allowed_total(r) for r in data[build].get(spec, [])] for spec in specs
        ]
        means[build] = [statistics.mean(t) if t else 0 for t in totals_per_spec]
        errs[build] = [
            statistics.stdev(t) if len(t) > 1 else 0 for t in totals_per_spec
        ]

    addplots = []
    for i, build in enumerate(builds):
        coords = " ".join(
            f"({spec},{means[build][j]:.2f}) +- (0,{errs[build][j]:.2f})"
            for j, spec in enumerate(specs)
            if data[build].get(spec)
        )
        addplots.append(
            f"\\addplot[{_pgf_style(i)}]\n"
            f"    plot[error bars/.cd, y dir=both, y explicit]\n"
            f"    coordinates {{{coords}}};\n"
            f"\\addlegendentry{{{build}}}"
        )

    sym = ",".join(specs)
    body = "\n".join(addplots)
    tex = (
        _arnie_header()
        + "\\begin{figure}[ht]\n"
        + "\\centering\n"
        + "\\resizebox{\\linewidth}{!}{%\n"
        + "\\begin{tikzpicture}\n"
        + "\\begin{axis}[\n"
        + "    ybar,\n"
        + "    bar width=14pt,\n"
        + "    width=14cm, height=7cm,\n"
        + f"    symbolic x coords={{{sym}}},\n"
        + f"    xtick={{{sym}}},\n"
        + "    enlarge x limits=0.2,\n"
        + "    xlabel={Specification},\n"
        + "    ylabel={Compile time},\n"
        + "    legend style={at={(1.02,1)},anchor=north west,nodes={anchor=west},font=\\small},\n"
        + _AXIS_BASE
        + "    ymode=log, log basis y=10,\n"
        + "    ymin=1, ymax=6000,\n"
        + "    ytick={10,50,100,500,1000,5000},\n"
        + "    yticklabels={10ms,50ms,100ms,500ms,1s,5s},\n"
        + "    log origin=infty,\n"
        + "]\n"
        + body
        + "\n"
        + "\\end{axis}\n"
        + "\\end{tikzpicture}%\n"
        + "}\n"
        + "\\caption{Total compile time per specification (lower is better).}\n"
        + "\\label{fig:total_time}\n"
        + "\\end{figure}\n"
    )
    out = plots_dir / "total_time.tex"
    out.write_text(tex)
    return out


def gen_total_time_table_tex(data: dict, plots_dir: Path) -> Path:
    builds = _builds_ordered(data)
    specs = _specs_ordered(next(iter(data.values())))

    col_spec = "l" + "r" * len(builds)
    header = " & ".join(
        ["\\textbf{Specification}"] + [f"\\textbf{{{b}}} (ms)" for b in builds]
    )

    rows = []
    for spec in specs:
        cells = []
        for build in builds:
            totals = [_allowed_total(r) for r in data[build].get(spec, [])]
            if totals:
                mean = statistics.mean(totals)
                std = statistics.stdev(totals) if len(totals) > 1 else 0.0
                cells.append(f"{mean:.1f} $\\pm$ {std:.1f}")
            else:
                cells.append("---")
        rows.append(f"    {spec} & {' & '.join(cells)} \\\\")

    tex = (
        _arnie_header()
        + "\\begin{table}[ht]\n"
        + "\\centering\n"
        + "\\begin{tabular}{"
        + col_spec
        + "}\n"
        "\\toprule\n"
        f"    {header} \\\\\n"
        "\\midrule\n" + "\n".join(rows) + "\n"
        "\\bottomrule\n"
        "\\end{tabular}\n"
        "\\caption{Total compile time per specification and build (mean $\\pm$ stddev, in milliseconds).}\n"
        "\\label{tab:total_time}\n"
        "\\end{table}\n"
    )
    out = plots_dir / "total_time_table.tex"
    out.write_text(tex)
    return out


def _phase_means(
    phases: list[str], build_data: dict, specs: list[str]
) -> dict[str, dict[str, float]]:
    """Return means[spec][phase] normalized so each spec's phases sum to 1."""
    raw: dict[str, dict[str, float]] = {
        spec: {
            phase: statistics.mean(
                [r[phase] for r in build_data.get(spec, []) if phase in r] or [0]
            )
            for phase in phases
        }
        for spec in specs
    }
    for spec in specs:
        total = sum(raw[spec].values()) or 1.0
        for phase in phases:
            raw[spec][phase] /= total
    return raw


def _stacked_addplots(
    phases: list[str], build_data: dict, specs: list[str], first: bool
) -> str:
    means = _phase_means(phases, build_data, specs)
    lines = []
    for i, phase in enumerate(phases):
        coords = " ".join(f"({spec},{means[spec][phase]:.4f})" for spec in specs)
        entry = f"\\addlegendentry{{{phase}}}" if first else ""
        lines.append(f"\\addplot[{_pgf_style(i)}] coordinates {{{coords}}};\n{entry}")
    return "\n".join(lines)


def gen_phase_breakdown_tex(data: dict, meta: dict, plots_dir: Path) -> list[Path]:
    specs = _specs_ordered(next(iter(data.values())))
    phases = allowed_phases(data)
    sym = ",".join(specs)
    outputs = []

    for build, build_data in data.items():
        body = _stacked_addplots(phases, build_data, specs, first=True)
        tex = (
            _arnie_header()
            + "\\begin{figure}[ht]\n"
            + "\\centering\n"
            + "\\resizebox{\\linewidth}{!}{%\n"
            + "\\begin{tikzpicture}\n"
            + "\\begin{axis}[\n"
            + "    ybar stacked,\n"
            + "    bar width=16pt,\n"
            + "    width=12cm, height=7cm,\n"
            + f"    symbolic x coords={{{sym}}},\n"
            + "    xtick=data,\n"
            + "    enlarge x limits=0.2,\n"
            + "    ymin=0, ymax=1,\n"
            + "    xlabel={Specification},\n"
            + "    ylabel={Fraction of compile time},\n"
            + "    legend style={at={(1.02,1)},anchor=north west,font=\\small},\n"
            + _AXIS_BASE
            + "]\n"
            + body
            + "\n"
            + "\\end{axis}\n"
            + "\\end{tikzpicture}%\n"
            + "}\n"
            + f"\\caption{{Phase breakdown for the \\texttt{{{build}}} build.}}\n"
            + f"\\label{{fig:phase_breakdown_{build}}}\n"
            + "\\end{figure}\n"
        )
        out = plots_dir / f"phase_breakdown_{build}.tex"
        out.write_text(tex)
        outputs.append(out)

    return outputs


def gen_phase_breakdown_combined_tex(data: dict, meta: dict, plots_dir: Path) -> Path:
    builds = _builds_ordered(data)
    specs = _specs_ordered(next(iter(data.values())))
    phases = allowed_phases(data)

    n_builds = len(builds)
    gap = n_builds + 1  # bars per group + 1 empty unit between groups

    def bar_pos(si: int, bi: int) -> float:
        return si * gap + bi + 1

    centers = [
        (bar_pos(si, 0) + bar_pos(si, n_builds - 1)) / 2 for si in range(len(specs))
    ]
    xmax = bar_pos(len(specs) - 1, n_builds - 1) + gap / 2

    # Normalized means: means[build][spec][phase] sums to 1 per (build, spec)
    norm: dict[str, dict[str, dict[str, float]]] = {
        build: _phase_means(phases, data[build], specs) for build in builds
    }

    addplots = []
    for pi, phase in enumerate(phases):
        coords = " ".join(
            f"({bar_pos(si, bi):.1f},{norm[build][spec][phase]:.4f})"
            for si, spec in enumerate(specs)
            for bi, build in enumerate(builds)
        )
        addplots.append(
            f"\\addplot[{_pgf_style(pi)}] coordinates {{{coords}}};\n"
            f"\\addlegendentry{{{phase}}}"
        )

    xtick = ",".join(f"{c:.1f}" for c in centers)
    xticklabels = ",".join(specs)
    extra_ticks = ",".join(
        f"{bar_pos(si, bi):.1f}" for si in range(len(specs)) for bi in range(n_builds)
    )
    extra_labels = ",".join(build for _ in specs for build in builds)

    body = "\n".join(addplots)
    tex = (
        _arnie_header()
        + "\\begin{figure}[ht]\n"
        + "\\centering\n"
        + "\\resizebox{\\linewidth}{!}{%\n"
        + "\\begin{tikzpicture}\n"
        + "\\begin{axis}[\n"
        + "    ybar stacked,\n"
        + "    bar width=10pt,\n"
        + "    width=15cm, height=8cm,\n"
        + f"    xmin=0, xmax={xmax:.1f},\n"
        + f"    xtick={{{xtick}}},\n"
        + f"    xticklabels={{{xticklabels}}},\n"
        + "    xticklabel style={yshift=-24pt},\n"
        + f"    extra x ticks={{{extra_ticks}}},\n"
        + f"    extra x tick labels={{{extra_labels}}},\n"
        + "    extra x tick style={tick label style={font=\\tiny,rotate=30,anchor=north east,yshift=24pt}},\n"
        + "    ymin=0, ymax=1,\n"
        + "    xlabel={Specification},\n"
        + "    ylabel={Fraction of compile time},\n"
        + "    legend style={at={(1.02,1)},anchor=north west,font=\\small},\n"
        + _AXIS_BASE
        + "]\n"
        + body
        + "\n"
        + "\\end{axis}\n"
        + "\\end{tikzpicture}%\n"
        + "}\n"
        + "\\caption{Phase breakdown across all specifications and builds.}\n"
        + "\\label{fig:phase_breakdown}\n"
        + "\\end{figure}\n"
    )
    out = plots_dir / "phase_breakdown.tex"
    out.write_text(tex)
    return out


_STAT_LABELS: dict[str, str] = {
    "files": "Files",
    "lines_of_code": "Lines of Code",
    "function_definitions": "Function Definitions",
    "format_definitions": "Format Definitions",
    "instruction_definitions": "Instruction Definitions",
    "total_definitions": "Total Definitions",
    "total_statements": "Statements",
    "total_expressions": "Expressions",
}


def gen_spec_stats_table_tex(
    spec_stats: dict[str, dict[str, int]], plots_dir: Path
) -> Path:
    specs = _specs_ordered(spec_stats)
    all_stats = list(_STAT_LABELS.keys())

    col_spec = "|l" + "|r" * len(specs) + "|"
    header = " & ".join(["\\textbf{Metric}"] + [f"\\textbf{{{s}}}" for s in specs])

    rows = []
    for stat in all_stats:
        label = _STAT_LABELS[stat]
        vals = [str(spec_stats[s].get(stat, "---")) for s in specs]
        rows.append(f"    {label} & {' & '.join(vals)} \\\\")

    tex = (
        _arnie_header()
        + "\\begin{table}[ht]\n"
        + "\\centering\n"
        + "\\begin{tabular}{"
        + col_spec
        + "}\n"
        "\\hline\n"
        f"    {header} \\\\\n"
        "\\hline\n" + "\n".join(rows) + "\n"
        "\\hline\n"
        "\\end{tabular}\n"
        "\\caption{Metrics for each benchmarked specification.}\n"
        "\\label{tab:spec_stats}\n"
        "\\end{table}\n"
    )
    out = plots_dir / "spec_stats.tex"
    out.write_text(tex)
    return out


def gen_plots_tex(
    preamble: Path,
    snippet_paths: list[Path],
    plots_dir: Path,
    table: Path | None = None,
    appendix_tables: list[Path] | None = None,
) -> Path:
    figures = "\n".join(f"\\input{{{p.name}}}" for p in snippet_paths)
    table_input = f"\\input{{{table.name}}}\n" if table else ""
    appendix_block = ""
    if appendix_tables:
        appendix_body = "\n".join(f"\\input{{{t.name}}}" for t in appendix_tables)
        appendix_block = "\n\\appendix\n\\section{Raw Data}\n" + appendix_body
    tex = (
        _arnie_header()
        + _PLOTS_TEX
        + "\\begin{document}\n"
        + f"\\input{{{preamble.name}}}\n"
        + table_input
        + "\n"
        + figures
        + appendix_block
        + "\n"
        + "\\end{document}\n"
    )
    out = plots_dir / "plots.tex"
    out.write_text(tex)
    return out


def pdf_to_png(pdf_path: Path) -> list[Path]:
    stem = pdf_path.stem
    out_pattern = pdf_path.parent / f"{stem}-%d.png"
    for gs in ("gs", "gswin64c"):
        result = subprocess.run(
            [
                gs,
                "-dNOPAUSE",
                "-dBATCH",
                "-sDEVICE=pngalpha",
                "-r150",
                f"-sOutputFile={out_pattern}",
                str(pdf_path),
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            pngs = sorted(pdf_path.parent.glob(f"{stem}-*.png"))
            if pngs:
                return pngs
    # Fallback: pdftoppm (poppler)
    result = subprocess.run(
        ["pdftoppm", "-r", "150", "-png", str(pdf_path), str(pdf_path.parent / stem)],
        capture_output=True,
    )
    pngs = sorted(pdf_path.parent.glob(f"{stem}-*.png"))
    if result.returncode == 0 and pngs:
        return pngs
    print(
        f"Warning: could not convert {pdf_path.name} to PNG "
        "(install Ghostscript or poppler).",
        file=sys.stderr,
    )
    return []


def cmd_plot(args: argparse.Namespace) -> None:
    data_dir = Path(args.data).resolve()
    if not data_dir.exists():
        print(f"Error: data directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)
    if data_dir.is_symlink():
        data_dir = data_dir.parent / data_dir.readlink()

    print(f"Loading data from {data_dir}")
    meta, data = load_benchmark(data_dir)
    if not data:
        print("Error: no benchmark data found.", file=sys.stderr)
        sys.exit(1)

    plots_dir = Path("plots")
    plots_dir.mkdir(exist_ok=True)

    preamble = gen_preamble_tex(meta, plots_dir)
    print(f"  Generated: {preamble.name}")

    spec_stats = load_spec_stats(data_dir)
    table: Path | None = None
    if spec_stats:
        table = gen_spec_stats_table_tex(spec_stats, plots_dir)
        print(f"  Generated: {table.name}")

    snippets = (
        [gen_total_time_tex(data, meta, plots_dir)]
        + gen_phase_breakdown_tex(data, meta, plots_dir)
        + [gen_phase_breakdown_combined_tex(data, meta, plots_dir)]
    )
    for s in snippets:
        print(f"  Generated: {s.name}")

    total_time_table = gen_total_time_table_tex(data, plots_dir)
    print(f"  Generated: {total_time_table.name}")

    plots_tex = gen_plots_tex(preamble, snippets, plots_dir, table, [total_time_table])
    print(f"\n  Compiling {plots_tex.name} ...")
    pdf = compile_tex(plots_tex)
    print(f"  Written:   {pdf}")

    if args.png:
        pngs = pdf_to_png(pdf)
        for png in pngs:
            print(f"  Written:   {png}")

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
        help=(
            "build to include: "
            + ", ".join(BUILD_CONFIGS.keys())
            + " (repeatable; default: all)"
        ),
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
    plot.add_argument(
        "--png",
        action="store_true",
        help="also rasterize each PDF to PNG via Ghostscript (for editor preview)",
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
