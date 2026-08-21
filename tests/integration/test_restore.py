"""Integration coverage for staged offline restore workflows."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from repo_archive.archive import ArchiveLayout, _remove_readonly, backup_archive
from repo_archive.git import GitRunner
from repo_archive.restore import RestoreError, restore_archive
from repo_archive.results import Outcome
from repo_archive.snapshots import create_snapshot


def git(*arguments: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def create_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    worktree = tmp_path / "worktree"
    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(worktree))
    git("config", "user.name", "Restore Test", cwd=worktree)
    git("config", "user.email", "restore@example.test", cwd=worktree)
    (worktree / "README.md").write_text("offline restore\n", encoding="utf-8")
    git("add", "README.md", cwd=worktree)
    git("commit", "-m", "initial", cwd=worktree)
    git("tag", "v1", cwd=worktree)
    git("remote", "add", "origin", str(remote), cwd=worktree)
    git("push", "-u", "origin", "main", "--tags", cwd=worktree)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    return remote, worktree


def commit_pointer(worktree: Path, payload: bytes) -> str:
    oid = hashlib.sha256(payload).hexdigest()
    (worktree / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    (worktree / "asset.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {len(payload)}\n",
        encoding="utf-8",
    )
    git(
        "-c",
        "filter.lfs.clean=cat",
        "-c",
        "filter.lfs.required=false",
        "add",
        ".gitattributes",
        "asset.bin",
        cwd=worktree,
    )
    git("commit", "-m", "add pointer", cwd=worktree)
    git("push", "--no-verify", cwd=worktree)
    return oid


def test_working_clone_restores_from_mirror_without_source_remote(
    tmp_path: Path,
) -> None:
    remote, worktree = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    shutil.rmtree(remote, onerror=_remove_readonly)
    shutil.rmtree(worktree, onerror=_remove_readonly)
    destination = tmp_path / "restored"

    result = restore_archive(layout, destination)

    assert result.outcome is Outcome.COMPLETE
    assert (destination / "README.md").read_text(encoding="utf-8") == (
        "offline restore\n"
    )
    assert git("rev-parse", "HEAD", cwd=destination) == git(
        "rev-parse", "refs/heads/main", cwd=layout.mirror_path
    )


def test_working_clone_restores_from_snapshot_without_mirror(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    created_at = datetime(2026, 8, 21, 14, 0, tzinfo=UTC)
    assert create_snapshot(layout, created_at=created_at).outcome is Outcome.COMPLETE
    shutil.rmtree(layout.mirror_path, onerror=_remove_readonly)
    layout.manifest_path.unlink()
    shutil.rmtree(remote, onerror=_remove_readonly)
    destination = tmp_path / "snapshot-restore"

    result = restore_archive(layout, destination, snapshot="2026-08-21T140000.000000Z")

    assert result.outcome is Outcome.COMPLETE
    assert (destination / "README.md").is_file()
    assert git("show-ref", "--verify", "refs/tags/v1", cwd=destination)


def test_recovered_mirrors_can_be_republished_from_both_sources(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    created_at = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)
    assert create_snapshot(layout, created_at=created_at).outcome is Outcome.COMPLETE

    mirror_restore = tmp_path / "mirror-restore.git"
    assert (
        restore_archive(layout, mirror_restore, recovered_mirror=True).outcome
        is Outcome.COMPLETE
    )
    assert git("config", "--bool", "remote.origin.mirror", cwd=mirror_restore) == "true"

    shutil.rmtree(layout.mirror_path, onerror=_remove_readonly)
    snapshot_restore = tmp_path / "snapshot-restore.git"
    result = restore_archive(
        layout,
        snapshot_restore,
        snapshot="2026-08-21T150000.000000Z",
        recovered_mirror=True,
    )
    assert result.outcome is Outcome.COMPLETE
    replacement = tmp_path / "replacement.git"
    git("init", "--bare", str(replacement))
    git("push", "--mirror", str(replacement), cwd=snapshot_restore)
    assert git("show-ref", "--verify", "refs/heads/main", cwd=replacement)
    assert git("show-ref", "--verify", "refs/tags/v1", cwd=replacement)


def test_restore_refuses_existing_destination_without_changing_it(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    result = restore_archive(layout, destination)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 2
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_failed_validation_cleans_restore_staging(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    destination = tmp_path / "failed"

    with patch(
        "repo_archive.restore._validate_working_clone",
        side_effect=RestoreError("simulated validation failure"),
    ):
        result = restore_archive(layout, destination)

    assert result.outcome is Outcome.FAILED
    assert not destination.exists()
    assert not list(tmp_path.glob(".failed.restore-*"))


def test_partial_snapshot_restores_git_and_reports_exact_lfs_gap(
    tmp_path: Path,
) -> None:
    remote, worktree = create_remote(tmp_path)
    oid = commit_pointer(worktree, b"unavailable payload\n")
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert (
        backup_archive(str(remote), layout, lfs_enabled=False).outcome
        is Outcome.PARTIAL
    )
    assert create_snapshot(layout).outcome is Outcome.PARTIAL
    snapshot_id = next(
        path.name for path in layout.snapshots_path.iterdir() if path.is_dir()
    )
    shutil.rmtree(layout.mirror_path, onerror=_remove_readonly)
    destination = tmp_path / "partial"

    result = restore_archive(
        layout,
        destination,
        snapshot=snapshot_id,
        runner=GitRunner(git_lfs_executable="missing-git-lfs-for-test"),
    )

    assert result.outcome is Outcome.PARTIAL
    assert destination.is_dir()
    lfs_component = next(
        item for item in result.components if item.name == "restore lfs"
    )
    assert oid in (lfs_component.message or "")
    assert oid in (destination / "asset.bin").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not installed")
def test_complete_snapshot_restores_lfs_content_offline(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    git("lfs", "install", "--local", cwd=worktree)
    git("lfs", "track", "*.bin", cwd=worktree)
    payload = b"complete offline payload\n"
    (worktree / "asset.bin").write_bytes(payload)
    git("add", ".gitattributes", "asset.bin", cwd=worktree)
    git("commit", "-m", "add LFS content", cwd=worktree)
    git("push", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert create_snapshot(layout).outcome is Outcome.COMPLETE
    snapshot_id = next(
        path.name for path in layout.snapshots_path.iterdir() if path.is_dir()
    )
    shutil.rmtree(layout.mirror_path, onerror=_remove_readonly)
    shutil.rmtree(remote, onerror=_remove_readonly)
    destination = tmp_path / "lfs-restored"

    result = restore_archive(layout, destination, snapshot=snapshot_id)

    assert result.outcome is Outcome.COMPLETE
    assert (destination / "asset.bin").read_bytes() == payload
