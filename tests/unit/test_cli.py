"""Tests for the initial command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from repo_archive.archive import ArchiveLayout
from repo_archive.cli import build_parser, main
from repo_archive.reporting import render_text
from repo_archive.results import ComponentResult, ComponentStatus, OperationResult


def test_parser_has_expected_program_name() -> None:
    assert build_parser().prog == "repo-archive"


def test_parser_accepts_backup_arguments() -> None:
    arguments = build_parser().parse_args(
        [
            "backup",
            "https://example.test/team/repo.git",
            "--root",
            "archives",
            "--name",
            "daily",
            "--no-lfs",
            "--bundle",
            "--metadata",
            "github",
            "--json",
            "--verbose",
        ]
    )

    assert arguments.command == "backup"
    assert arguments.root.name == "archives"
    assert arguments.name == "daily"
    assert arguments.no_lfs is True
    assert arguments.bundle is True
    assert arguments.metadata == "github"
    assert arguments.command_json is True


def test_parser_accepts_info_and_verification_modes() -> None:
    info = build_parser().parse_args(["info", "archive", "--json"])
    quick = build_parser().parse_args(["verify", "archive", "--quick"])
    full = build_parser().parse_args(["verify", "archive", "--full"])

    assert info.command == "info"
    assert quick.quick is True
    assert full.full is True


def test_json_flag_emits_only_stable_json(capsys: object) -> None:
    with patch("sys.argv", ["repo-archive", "--json"]):
        assert main() == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["operation"] == "help"
    assert captured.err == ""


def test_verify_json_emits_only_the_operation_result(capsys: object) -> None:
    result = OperationResult(
        "verify",
        None,
        components=(ComponentResult("git integrity", ComponentStatus.COMPLETE),),
    )
    with (
        patch("sys.argv", ["repo-archive", "verify", "archive", "--json"]),
        patch("repo_archive.cli.verify_archive", return_value=result),
    ):
        assert main() == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out) == result.to_dict()
    assert captured.err == ""


def test_backup_persists_the_decorated_cli_result(
    tmp_path: Path, capsys: object
) -> None:
    archive_path = tmp_path / "archives" / "project"
    archive_result = OperationResult(
        "backup",
        archive_path,
        components=(ComponentResult("git mirror", ComponentStatus.COMPLETE),),
    )
    with (
        patch(
            "sys.argv",
            [
                "repo-archive",
                "backup",
                "https://example.test/team/repo.git",
                "--root",
                str(tmp_path / "archives"),
                "--bundle",
                "--json",
            ],
        ),
        patch("repo_archive.cli.backup_archive", return_value=archive_result),
    ):
        assert main() == 0

    emitted = json.loads(capsys.readouterr().out)
    persisted = json.loads(
        (archive_path / "reports" / "latest.json").read_text(encoding="utf-8")
    )
    assert emitted == persisted
    assert any("--bundle has no effect" in warning for warning in persisted["warnings"])
    assert (archive_path / "reports" / "latest.txt").read_text(
        encoding="utf-8"
    ) == render_text(
        OperationResult(
            "backup",
            archive_path,
            components=archive_result.components,
            warnings=("--bundle has no effect until snapshot support is implemented.",),
        )
    )


def test_no_lfs_is_forwarded_without_a_deferred_warning(
    tmp_path: Path, capsys: object
) -> None:
    archive_path = tmp_path / "archives" / "project"
    archive_result = OperationResult(
        "backup",
        archive_path,
        components=(ComponentResult("lfs", ComponentStatus.PARTIAL),),
    )
    with (
        patch(
            "sys.argv",
            [
                "repo-archive",
                "backup",
                "https://example.test/team/repo.git",
                "--root",
                str(tmp_path / "archives"),
                "--name",
                "project",
                "--no-lfs",
                "--json",
            ],
        ),
        patch("repo_archive.cli.backup_archive", return_value=archive_result) as backup,
    ):
        assert main() == 3

    backup.assert_called_once_with(
        "https://example.test/team/repo.git",
        ArchiveLayout(archive_path),
        lfs_enabled=False,
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["warnings"] == []
