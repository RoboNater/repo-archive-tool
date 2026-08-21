"""Integration coverage for immutable bundle snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from repo_archive.archive import ArchiveLayout, backup_archive
from repo_archive.filesystem import remove_readonly
from repo_archive.git import CommandResult, GitRunner
from repo_archive.inspection import verify_archive
from repo_archive.manifest import load_manifest
from repo_archive.results import Outcome
from repo_archive.snapshots import create_snapshot, verify_snapshot_path


class CountingBatchRunner(GitRunner):
    """Count historical pointer enumeration process shapes."""

    def __init__(self) -> None:
        super().__init__()
        self.per_blob_calls = 0
        self.batch_blob_calls = 0

    def git(self, *arguments: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if arguments[:2] == ("cat-file", "blob"):
            self.per_blob_calls += 1
        return super().git(*arguments, **kwargs)

    def git_batch_blobs(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.batch_blob_calls += 1
        return super().git_batch_blobs(**kwargs)


class BundleFailureRunner(GitRunner):
    """Use real Git except for bundle creation."""

    def git(self, *arguments: str, **kwargs: object):  # type: ignore[no-untyped-def]
        if arguments[:2] == ("bundle", "create"):
            return CommandResult(
                ("git", *arguments), 1, "", "simulated bundle creation failure"
            )
        return super().git(*arguments, **kwargs)


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
    git("config", "user.name", "Snapshot Test", cwd=worktree)
    git("config", "user.email", "snapshot@example.test", cwd=worktree)
    (worktree / "README.md").write_text("snapshot\n", encoding="utf-8")
    git("add", "README.md", cwd=worktree)
    git("commit", "-m", "initial", cwd=worktree)
    git("tag", "v1", cwd=worktree)
    git("remote", "add", "origin", str(remote), cwd=worktree)
    git("push", "-u", "origin", "main", "--tags", cwd=worktree)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    return remote, worktree


def commit_lfs_pointer(worktree: Path, payload: bytes) -> str:
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
    git("commit", "-m", "add LFS pointer", cwd=worktree)
    git("push", "--no-verify", cwd=worktree)
    return oid


def test_snapshot_is_verified_and_atomically_indexed(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    result = create_snapshot(
        layout, created_at=datetime(2026, 8, 21, 12, 30, tzinfo=UTC)
    )

    assert result.outcome is Outcome.COMPLETE
    snapshot_path = layout.snapshots_path / "2026-08-21T123000.000000Z"
    record = json.loads((snapshot_path / "snapshot.json").read_text(encoding="utf-8"))
    assert record["verification"] == "verified-complete"
    assert {item["name"] for item in record["refs"]} >= {
        "refs/heads/main",
        "refs/tags/v1",
    }
    assert verify_snapshot_path(snapshot_path, deep=True).succeeded
    assert verify_archive(layout, deep=True).outcome is Outcome.COMPLETE
    manifest = load_manifest(layout.manifest_path)
    assert manifest.archive["snapshots"][0]["path"] == (
        "snapshots/2026-08-21T123000.000000Z"
    )
    assert not list(layout.snapshots_path.glob(".*.tmp-*"))


def test_snapshot_records_an_exact_lfs_gap_without_git_lfs(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    oid = commit_lfs_pointer(worktree, b"missing historical payload\n")
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert (
        backup_archive(str(remote), layout, lfs_enabled=False).outcome
        is Outcome.PARTIAL
    )

    runner = CountingBatchRunner()
    result = create_snapshot(layout, runner=runner)

    assert result.outcome is Outcome.PARTIAL
    snapshot_path = next(
        path.parent for path in layout.snapshots_path.glob("*/snapshot.json")
    )
    record = json.loads((snapshot_path / "snapshot.json").read_text(encoding="utf-8"))
    assert record["verification"] == "verified-partial"
    assert record["lfs"]["required_oids"] == [oid]
    assert record["lfs"]["present_oids"] == []
    assert record["lfs"]["unavailable_oids"] == [oid]
    assert verify_snapshot_path(snapshot_path, deep=True).outcome == "verified-partial"
    assert runner.per_blob_calls == 0
    assert runner.batch_blob_calls == 2


def test_materialization_failure_never_publishes_staging(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    payload = b"available payload\n"
    oid = commit_lfs_pointer(worktree, payload)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert (
        backup_archive(str(remote), layout, lfs_enabled=False).outcome
        is Outcome.PARTIAL
    )
    source = layout.mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)

    with patch(
        "repo_archive.snapshots._link_or_copy", side_effect=OSError("disk full")
    ):
        result = create_snapshot(
            layout, created_at=datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
        )

    assert result.outcome is Outcome.FAILED
    assert not list(layout.snapshots_path.glob("*/snapshot.json"))
    assert not list(layout.snapshots_path.glob(".*.tmp-*"))


def test_empty_archive_skips_snapshot_without_failing_backup(tmp_path: Path) -> None:
    remote = tmp_path / "empty.git"
    git("init", "--bare", str(remote))
    layout = ArchiveLayout(tmp_path / "archives" / "empty")
    backup = backup_archive(str(remote), layout)
    assert backup.outcome is Outcome.COMPLETE

    snapshot = create_snapshot(layout)

    assert snapshot.outcome is Outcome.COMPLETE_WITH_WARNINGS
    assert snapshot.exit_code == 0
    assert "No refs exist" in (snapshot.components[0].message or "")
    assert not list(layout.snapshots_path.glob("*/snapshot.json"))
    assert load_manifest(layout.manifest_path).archive["snapshots"] == []


def test_preflight_rejects_insufficient_space_before_publication(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    with patch(
        "repo_archive.snapshots.shutil.disk_usage",
        return_value=SimpleNamespace(free=0),
    ):
        result = create_snapshot(layout)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 1
    assert "Insufficient free space" in result.errors[0]
    assert not list(layout.snapshots_path.glob("*/snapshot.json"))


def test_bundle_creation_failure_uses_general_failure_exit(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    result = create_snapshot(layout, runner=BundleFailureRunner())

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 1
    assert "bundle creation failure" in result.errors[0]


def test_corrupt_archived_lfs_payload_prevents_snapshot_publication(
    tmp_path: Path,
) -> None:
    remote, worktree = create_remote(tmp_path)
    oid = commit_lfs_pointer(worktree, b"expected payload\n")
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert (
        backup_archive(str(remote), layout, lfs_enabled=False).outcome
        is Outcome.PARTIAL
    )
    source = layout.mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    source.parent.mkdir(parents=True)
    source.write_bytes(b"corrupt payload\n")

    result = create_snapshot(layout)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 4
    assert "corrupt" in result.errors[0]
    assert not list(layout.snapshots_path.glob("*/snapshot.json"))


def test_verification_rejects_a_stale_manifest_snapshot_index(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert create_snapshot(layout).outcome is Outcome.COMPLETE
    snapshot_path = next(
        path.parent for path in layout.snapshots_path.glob("*/snapshot.json")
    )
    shutil.rmtree(snapshot_path, onerror=remove_readonly)

    result = verify_archive(layout, full=False)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 4
    component = next(
        item for item in result.components if item.name == "snapshot index"
    )
    assert "missing on disk" in (component.message or "")
