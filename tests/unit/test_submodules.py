"""Tests for parsing submodule configuration and gitlink entries."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from repo_archive.git import CommandResult, GitRunner
from repo_archive.results import ComponentStatus
from repo_archive.submodules import (
    ParsedGitConfig,
    _parse_git_config,
    inspect_submodules,
)


def command(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(("git",), returncode, stdout, stderr)


def test_parse_git_config_preserves_paths_and_relative_urls() -> None:
    definitions = _parse_git_config(
        "submodule.my library.path\nvendor/my library\0"
        "submodule.my library.url\n../library.git\0"
    )

    assert definitions == ParsedGitConfig(
        (("vendor/my library", "../library.git"),), (), 1
    )


def test_parse_git_config_warns_and_keeps_other_definitions() -> None:
    parsed = _parse_git_config(
        "submodule.missing.path\nvendor/missing\0"
        "submodule.valid.path\nvendor/valid\0"
        "submodule.valid.url\n../valid.git\0"
    )

    assert parsed.definitions == (("vendor/valid", "../valid.git"),)
    assert "Ignored incomplete 'submodule.missing'" in parsed.warnings[0]


def test_inspection_batches_ref_resolution_and_reuses_identical_trees(
    tmp_path: Path,
) -> None:
    tree_oid = "1" * 40
    config_oid = "2" * 40
    commit_oid = "3" * 40
    runner = Mock(spec=GitRunner)
    runner.git.side_effect = [
        command("refs/heads/main\nrefs/tags/same\n"),
        command(f"{tree_oid} tree\n{tree_oid} tree\n"),
        command(
            f"100644 blob {config_oid}\t.gitmodules\0"
            f"160000 commit {commit_oid}\tvendor/library\0"
        ),
        command(
            "submodule.library.path\nvendor/library\0"
            "submodule.library.url\n../library.git\0"
        ),
    ]

    result = inspect_submodules(tmp_path, runner)

    assert result.component.status is ComponentStatus.WARNING
    repositories = result.manifest["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    assert repository["refs"] == ["refs/heads/main", "refs/tags/same"]
    assert repository["commits"] == [commit_oid]
    assert runner.git.call_count == 4
