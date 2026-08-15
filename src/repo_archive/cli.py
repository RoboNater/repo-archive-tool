"""Command-line interface for repo-archive-tool."""

from __future__ import annotations

import argparse

from repo_archive import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser for the archive command."""
    parser = argparse.ArgumentParser(
        prog="repo-archive",
        description="Create and maintain locally restorable Git repository archives.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main() -> int:
    """Run the command-line interface."""
    build_parser().parse_args()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
