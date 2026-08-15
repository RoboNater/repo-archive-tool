"""Tests for the initial command-line interface."""

from __future__ import annotations

from repo_archive.cli import build_parser


def test_parser_has_expected_program_name() -> None:
    assert build_parser().prog == "repo-archive"
