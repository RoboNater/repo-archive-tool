"""Command-line interface for repo-archive-tool."""

from __future__ import annotations

import argparse
import sys

from repo_archive import __version__
from repo_archive.reporting import emit_result
from repo_archive.results import ComponentResult, ComponentStatus, OperationResult


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser for the archive command."""
    parser = argparse.ArgumentParser(
        prog="repo-archive",
        description="Create and maintain locally restorable Git repository archives.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a stable JSON operation result",
    )
    return parser


def main() -> int:
    """Run the command-line interface."""
    arguments = build_parser().parse_args()
    result = OperationResult(
        operation="help",
        archive_path=None,
        components=(
            ComponentResult(
                name="cli",
                status=ComponentStatus.COMPLETE,
                message="Shared archive infrastructure is available.",
            ),
        ),
    )
    emit_result(result, sys.stdout, as_json=arguments.json)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
