"""Integration coverage for the single authoritative LFS requirement set.

These tests build Git history with plumbing commands so no clean filter can run,
which keeps the three divergence cases from issue #15 reproducible whether or
not `git-lfs` is installed on the machine running the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lfs_helpers import (
    LFS_ATTRIBUTES,
    NoLfsToolingRunner,
    make_repository,
    pointer_for,
    store_object,
)

from repo_archive.git import CommandResult, GitRunner
from repo_archive.lfs import archive_lfs, enumerate_lfs_oids, verify_lfs_archive
from repo_archive.results import (
    ComponentStatus,
    Outcome,
    aggregate_outcome,
    exit_code_for,
)


def test_pointer_outside_filter_lfs_attributes_is_still_required(
    tmp_path: Path,
) -> None:
    """Divergence 2: `ls-files` is attribute-driven; a real pointer still counts."""
    payload = b"content stored outside the tracked glob\n"
    oid, pointer = pointer_for(payload)
    repository = make_repository(
        tmp_path,
        {
            ".gitattributes": f"*.media {LFS_ATTRIBUTES}\n",
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
            ".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n",
            "data.bin": "raw bytes committed with the filter disabled\n",
        },
    )

    inventory = enumerate_lfs_oids(repository, GitRunner(), include_attributes=True)

    assert not isinstance(inventory, CommandResult)
    assert inventory.required_oids == ()
    assert inventory.tracking_declared is True

    component = verify_lfs_archive(repository, GitRunner()).component

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
            ".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n",
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
            ".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n",
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
            ".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n",
            "payload.bin": pointer,
        },
    )
    object_path = repository / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"truncated")

    verified = verify_lfs_archive(repository, NoLfsToolingRunner())
    component = verified.component

    assert component.status is ComponentStatus.PARTIAL
    assert verified.manifest["corrupt_objects"] == [oid]
    assert verified.manifest["tooling_available"] is False
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
