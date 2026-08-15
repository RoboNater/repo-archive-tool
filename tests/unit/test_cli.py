"""Tests for the initial command-line interface."""

from __future__ import annotations

import json
from unittest.mock import patch

from repo_archive.cli import build_parser, main


def test_parser_has_expected_program_name() -> None:
    assert build_parser().prog == "repo-archive"


def test_json_flag_emits_only_stable_json(capsys: object) -> None:
    with patch("sys.argv", ["repo-archive", "--json"]):
        assert main() == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert json.loads(captured.out)["operation"] == "help"
    assert captured.err == ""
