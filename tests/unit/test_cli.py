"""Tests for the initial command-line interface."""

from __future__ import annotations

import json
from unittest.mock import patch

from repo_archive.cli import build_parser, main
from repo_archive.results import ComponentResult, ComponentStatus, OperationResult


def test_parser_has_expected_program_name() -> None:
    assert build_parser().prog == "repo-archive"


def test_parser_accepts_phase_two_backup_arguments() -> None:
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
