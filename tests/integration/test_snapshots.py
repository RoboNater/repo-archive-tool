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
from repo_archive.filesystem import remove_readonly, safe_sha256_file
from repo_archive.git import CommandResult, GitRunner
from repo_archive.inspection import verify_archive
from repo_archive.manifest import load_manifest, write_json_atomic
from repo_archive.results import ComponentStatus, Outcome
from repo_archive.snapshots import (
    create_snapshot,
    load_snapshot_record,
    verify_snapshot_path,
)


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


def test_preflight_does_not_count_hardlinked_payload_bytes(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    payload = b"x" * 100_000
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
        "repo_archive.snapshots.shutil.disk_usage",
        return_value=SimpleNamespace(free=50_000),
    ):
        result = create_snapshot(layout)

    assert result.outcome is Outcome.COMPLETE


def test_preflight_does_not_hash_payloads(tmp_path: Path) -> None:
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
        "repo_archive.snapshots.safe_sha256_file", wraps=safe_sha256_file
    ) as hashed:
        result = create_snapshot(layout)

    assert result.outcome is Outcome.COMPLETE
    assert sum(call.args[0].name == oid for call in hashed.call_args_list) == 2


def test_bundle_creation_failure_uses_general_failure_exit(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    result = create_snapshot(layout, runner=BundleFailureRunner())

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 1
    assert "bundle creation failure" in result.errors[0]


def test_unwritable_snapshot_staging_is_a_structured_failure(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    with patch(
        "repo_archive.snapshots.tempfile.mkdtemp",
        side_effect=PermissionError("read-only archive"),
    ):
        result = create_snapshot(layout, record_result=False)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 2
    assert "read-only archive" in result.errors[0]


def test_manifest_index_failure_warns_after_snapshot_publication(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    def fail_manifest_index(path: Path, value: object) -> None:
        if path == layout.manifest_path:
            raise PermissionError("read-only manifest")
        write_json_atomic(path, value)

    with patch(
        "repo_archive.snapshots.write_json_atomic", side_effect=fail_manifest_index
    ):
        result = create_snapshot(
            layout,
            created_at=datetime(2026, 8, 21, 14, 0, tzinfo=UTC),
            record_result=False,
        )

    snapshot_path = layout.snapshots_path / "2026-08-21T140000.000000Z"
    assert result.outcome is Outcome.COMPLETE_WITH_WARNINGS
    assert result.exit_code == 0
    manifest_component = next(
        item for item in result.components if item.name == "manifest"
    )
    assert "snapshots/2026-08-21T140000.000000Z" in (manifest_component.message or "")
    assert "read-only manifest" in (manifest_component.message or "")
    assert verify_snapshot_path(snapshot_path).succeeded
    assert load_manifest(layout.manifest_path).archive["snapshots"] == []


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


def test_verification_warns_about_a_stale_manifest_snapshot_index(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert create_snapshot(layout).outcome is Outcome.COMPLETE
    snapshot_path = next(
        path.parent for path in layout.snapshots_path.glob("*/snapshot.json")
    )
    shutil.rmtree(snapshot_path, onerror=remove_readonly)

    result = verify_archive(layout, full=False)

    assert result.outcome is Outcome.COMPLETE_WITH_WARNINGS
    assert result.exit_code == 0
    component = next(
        item for item in result.components if item.name == "snapshot index"
    )
    assert "missing on disk" in (component.message or "")


def test_snapshot_creation_rebuilds_the_manifest_index(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert (
        create_snapshot(
            layout, created_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        ).outcome
        is Outcome.COMPLETE
    )
    manifest_data = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    manifest_data["archive"]["snapshots"] = []
    layout.manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    assert (
        create_snapshot(
            layout, created_at=datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
        ).outcome
        is Outcome.COMPLETE
    )

    indexed = load_manifest(layout.manifest_path).archive["snapshots"]
    assert [entry["path"] for entry in indexed] == [
        "snapshots/2026-08-21T120000.000000Z",
        "snapshots/2026-08-21T130000.000000Z",
    ]


def test_snapshot_index_rebuild_names_skipped_records(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert (
        create_snapshot(
            layout, created_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        ).outcome
        is Outcome.COMPLETE
    )
    skipped_path = layout.snapshots_path / "2026-08-21T120000.000000Z"
    real_load = load_snapshot_record

    def fail_old_record(snapshot_path: Path) -> dict[str, object]:
        if snapshot_path == skipped_path:
            raise PermissionError("record is unreadable")
        return real_load(snapshot_path)

    with patch(
        "repo_archive.snapshots.load_snapshot_record", side_effect=fail_old_record
    ):
        result = create_snapshot(
            layout, created_at=datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
        )

    assert result.outcome is Outcome.COMPLETE_WITH_WARNINGS
    manifest_component = next(
        item for item in result.components if item.name == "manifest"
    )
    assert "snapshots/2026-08-21T120000.000000Z" in (manifest_component.message or "")
    assert "record is unreadable" in (manifest_component.message or "")


def test_snapshot_verification_warns_about_unexpected_root_entries(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert create_snapshot(layout).outcome is Outcome.COMPLETE
    snapshot_path = next(
        path.parent for path in layout.snapshots_path.glob("*/snapshot.json")
    )
    (snapshot_path / ".lfs-hardlink-probe").write_text("stray", encoding="utf-8")

    verified = verify_snapshot_path(snapshot_path)

    assert verified.outcome == "verified-complete"
    assert verified.succeeded
    assert ".lfs-hardlink-probe" in verified.message
    assert verified.warnings

    archive_result = verify_archive(layout)

    assert archive_result.outcome is Outcome.COMPLETE_WITH_WARNINGS
    assert archive_result.exit_code == 0
    snapshot_component = next(
        item
        for item in archive_result.components
        if item.name == f"snapshot {snapshot_path.name}"
    )
    assert snapshot_component.status is ComponentStatus.WARNING
    assert ".lfs-hardlink-probe" in (snapshot_component.message or "")


def test_snapshot_verification_rejects_non_directory_lfs_root_entry(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    assert create_snapshot(layout).outcome is Outcome.COMPLETE
    snapshot_path = next(
        path.parent for path in layout.snapshots_path.glob("*/snapshot.json")
    )
    lfs_path = snapshot_path / "lfs"
    if lfs_path.is_dir():
        shutil.rmtree(lfs_path)
    lfs_path.write_text("collision", encoding="utf-8")

    verified = verify_snapshot_path(snapshot_path)

    assert verified.outcome == "failed"
    assert "lfs entry is not a directory" in verified.message


def test_verification_rejects_a_malformed_manifest_snapshot_index(
    tmp_path: Path,
) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    manifest_data = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    manifest_data["archive"]["snapshots"] = "invalid"
    layout.manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    result = verify_archive(layout, full=False)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 4
