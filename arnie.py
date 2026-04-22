import argparse
import sys
import tomllib
import tomli_w
from pathlib import Path

CONFIG_FILE = Path("config.toml")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "config":
        cmd_config(args)


if __name__ == "__main__":
    main()
