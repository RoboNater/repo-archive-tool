"""Command-line interface for repo-archive-tool."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repo_archive import __version__
from repo_archive.archive import ArchiveLayout, backup_archive, update_archive
from repo_archive.inspection import info_archive, verify_archive
from repo_archive.remote import derive_archive_path, normalize_remote
from repo_archive.reporting import emit_result, write_latest_reports
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser for the archive command."""
    parser = argparse.ArgumentParser(
        prog="repo-archive",
        description="Create and maintain locally restorable Git repository archives.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--json", action="store_true", help="emit a stable JSON result")
    parser.add_argument(
        "--verbose", action="store_true", help="request expanded diagnostics"
    )
    subcommands = parser.add_subparsers(dest="command")

    backup = subcommands.add_parser("backup", help="create or update an archive")
    backup.add_argument("remote_url")
    backup.add_argument(
        "--root", type=Path, required=True, help="archive root directory"
    )
    backup.add_argument("--name", help="archive-set name override")
    backup.add_argument(
        "--no-lfs",
        action="store_true",
        help="intentionally skip LFS object fetching (partial when LFS is detected)",
    )
    backup.add_argument("--bundle", action="store_true", help="reserved for snapshots")
    backup.add_argument(
        "--metadata", choices=("github",), help="reserved for metadata export"
    )
    backup.add_argument("--json", action="store_true", dest="command_json")
    backup.add_argument("--verbose", action="store_true", dest="command_verbose")

    update = subcommands.add_parser("update", help="safely refresh an archive")
    update.add_argument("archive_path", type=Path)
    update.add_argument("--json", action="store_true", dest="command_json")
    update.add_argument("--verbose", action="store_true", dest="command_verbose")

    info = subcommands.add_parser("info", help="show archive contents and status")
    info.add_argument("archive_path", type=Path)
    info.add_argument("--json", action="store_true", dest="command_json")
    info.add_argument("--verbose", action="store_true", dest="command_verbose")

    verify = subcommands.add_parser("verify", help="verify an archive")
    verify.add_argument("archive_path", type=Path)
    verification_mode = verify.add_mutually_exclusive_group()
    verification_mode.add_argument(
        "--quick", action="store_true", help="skip object and bundle checks"
    )
    verification_mode.add_argument(
        "--full", action="store_true", help="verify objects and bundle snapshots"
    )
    verify.add_argument("--json", action="store_true", dest="command_json")
    verify.add_argument("--verbose", action="store_true", dest="command_verbose")
    return parser


def main() -> int:
    """Run the command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args()
    as_json = arguments.json or getattr(arguments, "command_json", False)
    if arguments.command == "backup":
        try:
            remote = normalize_remote(arguments.remote_url)
            layout = ArchiveLayout(
                derive_archive_path(arguments.root, remote, arguments.name)
            )
        except ValueError as error:
            result = _configuration_failure("backup", str(error))
        else:
            result = backup_archive(
                arguments.remote_url, layout, lfs_enabled=not arguments.no_lfs
            )
            result = _add_deferred_option_warnings(result, arguments)
    elif arguments.command == "update":
        result = update_archive(ArchiveLayout(arguments.archive_path))
    elif arguments.command == "info":
        result = info_archive(ArchiveLayout(arguments.archive_path))
    elif arguments.command == "verify":
        result = verify_archive(
            ArchiveLayout(arguments.archive_path), full=not arguments.quick
        )
    else:
        result = OperationResult(
            operation="help",
            archive_path=None,
            components=(
                ComponentResult(
                    name="cli",
                    status=ComponentStatus.COMPLETE,
                    message="Use a subcommand such as backup or update.",
                ),
            ),
        )
    _write_final_report(result)
    emit_result(result, sys.stdout, as_json=as_json)
    return result.exit_code


def _configuration_failure(operation: str, message: str) -> OperationResult:
    return OperationResult(
        operation=operation,
        archive_path=None,
        components=(
            ComponentResult(
                "configuration",
                ComponentStatus.FAILED,
                message,
                ErrorKind.CONFIGURATION,
            ),
        ),
        errors=(message,),
    )


def _add_deferred_option_warnings(
    result: OperationResult, arguments: argparse.Namespace
) -> OperationResult:
    warnings = list(result.warnings)
    if arguments.bundle:
        warnings.append("--bundle has no effect until snapshot support is implemented.")
    if arguments.metadata:
        warnings.append("Metadata export is not implemented yet.")
    if getattr(arguments, "verbose", False) or getattr(
        arguments, "command_verbose", False
    ):
        warnings.append("Expanded command diagnostics are not implemented yet.")
    if not warnings:
        return result
    return OperationResult(
        operation=result.operation,
        archive_path=result.archive_path,
        components=result.components,
        warnings=tuple(warnings),
        errors=result.errors,
    )


def _write_final_report(result: OperationResult) -> None:
    """Persist the exact result emitted by the CLI when it targets an archive."""
    if result.archive_path is not None:
        write_latest_reports(ArchiveLayout(result.archive_path).reports_path, result)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
