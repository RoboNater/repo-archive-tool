"""Integration coverage for the single authoritative LFS requirement set.

These tests build Git history with plumbing commands so no clean filter can run,
which keeps the three divergence cases from issue #15 reproducible whether or
not `git-lfs` is installed on the machine running the suite.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from repo_archive.git import CommandResult, GitRunner
from repo_archive.lfs import archive_lfs, enumerate_lfs_oids, verify_lfs_archive
from repo_archive.results import (
    ComponentStatus,
    Outcome,
    aggregate_outcome,
    exit_code_for,
)

_LFS_ATTRIBUTES = "filter=lfs diff=lfs merge=lfs -text"


class NoLfsToolingRunner(GitRunner):
    """Use real Git while simulating a machine with no `git-lfs` executable."""

    def lfs(self, *arguments: str, **kwargs: object) -> CommandResult:
        return CommandResult(
            ("git", "lfs", *arguments), 127, "", "git: 'lfs' is not a git command"
        )


def git(*arguments: str, cwd: Path | None = None, stdin: str = "") -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        input=stdin,
    )
    return completed.stdout.strip()


def pointer_for(content: bytes) -> tuple[str, str]:
    """Build a valid LFS pointer blob for *content* and return (oid, pointer)."""
    oid = hashlib.sha256(content).hexdigest()
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {len(content)}\n"
    )
    return oid, pointer


def make_repository(tmp_path: Path, files: dict[str, str]) -> Path:
    """Commit *files* verbatim into a bare repository, bypassing any filters."""
    repository = tmp_path / "mirror.git"
    git("init", "--bare", "-b", "main", str(repository))
    git("config", "user.name", "Archive Test", cwd=repository)
    git("config", "user.email", "archive@example.test", cwd=repository)

    entries = []
    for path, content in sorted(files.items()):
        blob = git("hash-object", "-w", "--stdin", cwd=repository, stdin=content)
        entries.append(f"100644 blob {blob}\t{path}")
    tree = git("mktree", cwd=repository, stdin="\n".join(entries) + "\n")
    commit = git("commit-tree", tree, "-m", "test history", cwd=repository)
    git("update-ref", "refs/heads/main", commit, cwd=repository)
    return repository


def store_object(mirror_path: Path, content: bytes) -> None:
    oid = hashlib.sha256(content).hexdigest()
    object_path = mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(content)


def test_pointer_outside_filter_lfs_attributes_is_still_required(
    tmp_path: Path,
) -> None:
    """Divergence 2: `ls-files` is attribute-driven; a real pointer still counts."""
    payload = b"content stored outside the tracked glob\n"
    oid, pointer = pointer_for(payload)
    repository = make_repository(
        tmp_path,
        {
            ".gitattributes": f"*.media {_LFS_ATTRIBUTES}\n",
            "payload.bin": pointer,
        },
    )

    inventory = enumerate_lfs_oids(repository, GitRunner(), include_attributes=True)

    assert not isinstance(inventory, CommandResult)
    assert inventory.required_oids == (oid,)
    assert inventory.tracking_declared is True


def test_tracked_path_committed_as_raw_content_requires_nothing(
    tmp_path: Path,
) -> None:
    """Divergence 3: a tracked glob with no pointer has no object to fetch."""
    repository = make_repository(
        tmp_path,
        {
            ".gitattributes": f"*.bin {_LFS_ATTRIBUTES}\n",
            "data.bin": "raw bytes committed with the filter disabled\n",
        },
    )

    inventory = enumerate_lfs_oids(repository, GitRunner(), include_attributes=True)

    assert not isinstance(inventory, CommandResult)
    assert inventory.required_oids == ()
    assert inventory.tracking_declared is True

    component = verify_lfs_archive(repository, GitRunner())

    assert component.status is ComponentStatus.COMPLETE
    assert "no reachable" in (component.message or "")
    assert exit_code_for(aggregate_outcome((component,)), (component,)) == 0


def test_missing_git_lfs_still_names_the_exact_gap(tmp_path: Path) -> None:
    """Divergence 1: the requirement set is known without the `git-lfs` binary."""
    payload = b"payload that was never fetched\n"
    oid, pointer = pointer_for(payload)
    repository = make_repository(
        tmp_path,
        {
            ".gitattributes": f"*.bin {_LFS_ATTRIBUTES}\n",
            "payload.bin": pointer,
        },
    )

    result = archive_lfs(repository, NoLfsToolingRunner(), enabled=True)

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["tooling_available"] is False
    assert result.manifest["expected_object_count"] == 1
    assert result.manifest["missing_objects"] == [oid]
    outcome = aggregate_outcome((result.component,))
    assert outcome is Outcome.PARTIAL
    assert exit_code_for(outcome, (result.component,)) == 3


def test_missing_git_lfs_with_a_complete_store_is_complete(tmp_path: Path) -> None:
    """A known requirement set that is already archived needs no tooling."""
    payload = b"payload carried by a previous archive\n"
    _, pointer = pointer_for(payload)
    repository = make_repository(
        tmp_path,
        {
            ".gitattributes": f"*.bin {_LFS_ATTRIBUTES}\n",
            "payload.bin": pointer,
        },
    )
    store_object(repository, payload)

    result = archive_lfs(repository, NoLfsToolingRunner(), enabled=True)

    assert result.component.status is ComponentStatus.COMPLETE
    assert result.manifest["status"] == "complete"
    assert result.manifest["tooling_available"] is False
    outcome = aggregate_outcome((result.component,))
    assert outcome is Outcome.COMPLETE
    assert exit_code_for(outcome, (result.component,)) == 0


def test_corrupt_archived_object_is_reported_without_tooling(tmp_path: Path) -> None:
    payload = b"payload that was corrupted on disk\n"
    oid, pointer = pointer_for(payload)
    repository = make_repository(
        tmp_path,
        {
            ".gitattributes": f"*.bin {_LFS_ATTRIBUTES}\n",
            "payload.bin": pointer,
        },
    )
    object_path = repository / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"truncated")

    component = verify_lfs_archive(repository, NoLfsToolingRunner())

    assert component.status is ComponentStatus.PARTIAL
    assert exit_code_for(aggregate_outcome((component,)), (component,)) == 3


@pytest.mark.parametrize("include_attributes", [False, True])
def test_enumeration_ignores_history_without_any_pointer(
    tmp_path: Path, include_attributes: bool
) -> None:
    repository = make_repository(tmp_path, {"README.md": "ordinary content\n"})

    inventory = enumerate_lfs_oids(
        repository, GitRunner(), include_attributes=include_attributes
    )

    assert not isinstance(inventory, CommandResult)
    assert inventory.required_oids == ()
    assert inventory.tracking_declared is False
