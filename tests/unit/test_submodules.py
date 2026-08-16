"""Tests for parsing submodule configuration and gitlink entries."""

from __future__ import annotations

import pytest

from repo_archive.submodules import _gitlink_commit, _parse_git_config


def test_parse_git_config_preserves_paths_and_relative_urls() -> None:
    definitions = _parse_git_config(
        "submodule.my library.path\nvendor/my library\0"
        "submodule.my library.url\n../library.git\0"
    )

    assert definitions == [("vendor/my library", "../library.git")]


def test_parse_git_config_requires_path_and_url() -> None:
    with pytest.raises(ValueError, match="both path and url"):
        _parse_git_config("submodule.missing.path\nvendor/missing\0")


def test_gitlink_commit_reads_mode_160000_only() -> None:
    oid = "1" * 40

    assert _gitlink_commit(f"160000 commit {oid}\tvendor/library\n") == oid
    assert _gitlink_commit(f"100644 blob {oid}\tvendor/library\n") is None
