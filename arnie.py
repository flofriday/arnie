#!/usr/bin/env -S uv run
"""Compatibility entry point; implementation lives in the arnie package."""

from arnie.cli import main

if __name__ == "__main__":
    main()
