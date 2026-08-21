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
    update = build_parser().parse_args(["update", "archive", "--no-lfs"])
    quick = build_parser().parse_args(["verify", "archive", "--quick"])
    full = build_parser().parse_args(["verify", "archive", "--full"])
    deep = build_parser().parse_args(["verify", "archive", "--deep"])
    snapshot = build_parser().parse_args(["snapshot", "archive", "--json"])
    restore = build_parser().parse_args(
        ["restore", "archive", "destination", "--snapshot", "stamp", "--mirror"]
    )

    assert info.command == "info"
    assert update.no_lfs is True
    assert quick.quick is True
    assert full.full is True
    assert deep.deep is True
    assert snapshot.command == "snapshot"
    assert restore.snapshot == "stamp"
    assert restore.mirror is True


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


def test_backup_bundle_creates_snapshot_and_persists_combined_result(
    tmp_path: Path, capsys: object
) -> None:
    archive_path = tmp_path / "archives" / "project"
    archive_result = OperationResult(
        "backup",
        archive_path,
        components=(ComponentResult("git mirror", ComponentStatus.COMPLETE),),
    )
    snapshot_result = OperationResult(
        "snapshot",
        archive_path,
        components=(ComponentResult("bundle", ComponentStatus.COMPLETE),),
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
                "--bundle",
                "--json",
            ],
        ),
        patch("repo_archive.cli.backup_archive", return_value=archive_result),
        patch(
            "repo_archive.cli.create_snapshot", return_value=snapshot_result
        ) as snapshot,
    ):
        assert main() == 0

    snapshot.assert_called_once_with(ArchiveLayout(archive_path), record_result=False)
    emitted = json.loads(capsys.readouterr().out)
    persisted = json.loads(
        (archive_path / "reports" / "latest.json").read_text(encoding="utf-8")
    )
    assert emitted == persisted
    assert persisted["warnings"] == []
    assert [item["name"] for item in persisted["components"]] == [
        "git mirror",
        "bundle",
    ]
    assert (archive_path / "reports" / "latest.txt").read_text(
        encoding="utf-8"
    ) == render_text(
        OperationResult(
            "backup",
            archive_path,
            components=archive_result.components + snapshot_result.components,
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


def test_update_forwards_no_lfs(tmp_path: Path, capsys: object) -> None:
    archive_path = tmp_path / "archive"
    result = OperationResult(
        "update",
        archive_path,
        components=(ComponentResult("lfs", ComponentStatus.PARTIAL),),
    )
    with (
        patch(
            "sys.argv",
            ["repo-archive", "update", str(archive_path), "--no-lfs", "--json"],
        ),
        patch("repo_archive.cli.update_archive", return_value=result) as update,
    ):
        assert main() == 3

    update.assert_called_once_with(ArchiveLayout(archive_path), lfs_enabled=False)
    assert json.loads(capsys.readouterr().out)["outcome"] == "partial"


def test_restore_forwards_source_and_mode(tmp_path: Path, capsys: object) -> None:
    archive_path = tmp_path / "archive"
    destination = tmp_path / "restored.git"
    result = OperationResult(
        "restore",
        archive_path,
        components=(ComponentResult("git restore", ComponentStatus.COMPLETE),),
    )
    with (
        patch(
            "sys.argv",
            [
                "repo-archive",
                "restore",
                str(archive_path),
                str(destination),
                "--snapshot",
                "2026-08-21T150000.000000Z",
                "--mirror",
                "--json",
            ],
        ),
        patch("repo_archive.cli.restore_archive", return_value=result) as restore,
    ):
        assert main() == 0

    restore.assert_called_once_with(
        ArchiveLayout(archive_path),
        destination,
        snapshot="2026-08-21T150000.000000Z",
        recovered_mirror=True,
    )
    assert json.loads(capsys.readouterr().out)["operation"] == "restore"
