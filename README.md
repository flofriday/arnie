# Arnie

A small tool for benchmarking the original VADL compiler against OpenVADL's JVM
(`graalvm`) and native (`native`) builds. Requires Python 3.14+ and
[uv](https://docs.astral.sh/uv/).

## Usage

Run commands from this checkout; `config.toml`, `data/`, and `plots/` are relative
to the current directory.

```bash
uv run arnie config                     # Set compiler repository paths once
uv run arnie bench                      # Build and benchmark all configurations
uv run arnie bench --build native --runs 10 --warmup 3
uv run arnie bench --build graalvm --build native --no-build
uv run arnie bench --reuse-original      # Reuse original results from data/latest
uv run arnie plot                       # Generate plots/plots.pdf and TeX snippets
uv run arnie plot --data data/<run> --png
```

The existing `uv run arnie.py ...` entry point and `uv run python -m arnie ...`
also work. `--runs` must be positive; `--warmup` may be zero. Repeating `--build`
selects multiple configurations. `--reuse-original` requires `original` to be
selected (all configurations are selected by default).

Compiler builds need the tools required by their respective repositories. The
original compiler's Java 17 environment currently uses macOS's
`/usr/libexec/java_home -v17`. Plotting requires `pdflatex` and the TeX packages
listed in `arnie/plotting.py`'s report preamble. PNG export additionally needs
Ghostscript or Poppler (`pdftoppm`); it warns if neither converter is available.

## Results

Each benchmark writes a new UTC timestamped directory under `data/` containing
`metadata.toml` and `<build>/<spec>/run_N.csv`, `warmup_N.csv`, and, where
supported, `spec-stats.csv`. Warmups are excluded from reported means and standard
deviations. Totals sum the included phases, excluding compiler totals and the
passes listed in `IGNORED_PASSES`.

Previous runs and plots are preserved. `data/latest` is updated atomically after
a successful benchmark. Failed runs can leave partial directories for diagnosis;
they do not replace `latest`. Running `plot` regenerates the report in `plots/`.
Old timestamp formats and existing CSVs remain readable.

Both compilers use **Name Resolution** in reports. OpenVADL's older
**Symbol Resolution** timings and the original compiler's symbol registration
and resolver passes are mapped to that name when loading data. Raw CSVs are
preserved, and passes mapping to the same name are summed.

`--reuse-original` copies the previous original-compiler measurements and retains
their commit, run counts, warmup counts, source directory, and available machine
metadata. New configurations use the requested counts; reports show per-build
counts for reused results.

## Development

- `arnie/config.py`: compiler commands, specification paths, pass mappings, and
  local configuration. Add builds and specs here; specs absent for a compiler
  family are skipped. Private specs are resolved under this checkout's
  `secret_specs/` directory.
- `arnie/benchmark.py`: build execution, measurement capture, and result metadata.
- `arnie/results.py`: CSV validation, name normalization, ordering, and totals.
- `arnie/plotting.py`: LaTeX figures, tables, compilation, and PNG export.
- `arnie/cli.py`: command-line arguments and user-facing errors.

Run the regression tests without invoking real compiler builds:

```bash
uv run python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

Tests use temporary directories and a small fake compiler process to cover
successful runs, failed builds, stale output, reuse metadata, old/new pass names,
and report calculations. Ruff is an optional developer tool, installed separately.
