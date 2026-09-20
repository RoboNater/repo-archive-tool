"""Tests for the authoritative LFS requirement set and object verification."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

from repo_archive.git import CommandResult, GitRunner
from repo_archive.lfs import (
    LfsInventory,
    _link_or_copy,
    archive_lfs,
    parse_lfs_pointer,
    verify_lfs_objects,
)
from repo_archive.results import ComponentStatus


def command(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(("git",), returncode, stdout, stderr)


def store_object(mirror_path: Path, content: bytes) -> str:
    """Write *content* into the archive-local LFS store and return its OID."""
    oid = hashlib.sha256(content).hexdigest()
    object_path = mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(content)
    return oid


def test_parse_lfs_pointer_accepts_a_valid_pointer() -> None:
    oid = "b" * 64
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 12\n"

    assert parse_lfs_pointer(pointer) == oid


def test_parse_lfs_pointer_rejects_non_pointer_content() -> None:
    assert parse_lfs_pointer("just some file content\n") is None
    assert parse_lfs_pointer("oid sha256:" + "a" * 64) is None


def test_verify_lfs_objects_reports_missing_objects(tmp_path: Path) -> None:
    oid = "a" * 64
    runner = Mock(spec=GitRunner)

    result = verify_lfs_objects(
        tmp_path, runner, inventory=LfsInventory((oid,), tracking_declared=True)
    )

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["reason"] == "objects-incomplete"
    assert result.manifest["missing_objects"] == [oid]
    runner.lfs.assert_not_called()


def test_verify_lfs_objects_hashes_archive_local_content(tmp_path: Path) -> None:
    oid = store_object(tmp_path, b"archived lfs content\n")
    runner = Mock(spec=GitRunner)

    result = verify_lfs_objects(tmp_path, runner, inventory=LfsInventory((oid,)))

    assert result.component.status is ComponentStatus.COMPLETE
    assert result.manifest["expected_object_count"] == 1
    assert result.manifest["missing_objects"] == []


def test_verify_lfs_objects_reports_corrupt_content(tmp_path: Path) -> None:
    oid = "a" * 64
    object_path = tmp_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"not the expected object")
    runner = Mock(spec=GitRunner)

    result = verify_lfs_objects(tmp_path, runner, inventory=LfsInventory((oid,)))

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["corrupt_objects"] == [oid]


def test_verify_lfs_objects_without_pointers_is_not_applicable(tmp_path: Path) -> None:
    runner = Mock(spec=GitRunner)

    result = verify_lfs_objects(tmp_path, runner, inventory=LfsInventory(()))

    assert result.component.status is ComponentStatus.COMPLETE
    assert result.manifest["status"] == "not-applicable"
    assert result.manifest["expected_object_count"] == 0
    assert result.manifest["tracking_declared"] is False


def test_declared_tracking_without_pointers_is_reported_but_complete(
    tmp_path: Path,
) -> None:
    runner = Mock(spec=GitRunner)

    result = verify_lfs_objects(
        tmp_path,
        runner,
        inventory=LfsInventory((), attribute_files=1, tracking_declared=True),
    )

    assert result.component.status is ComponentStatus.COMPLETE
    assert result.manifest["tracking_declared"] is True
    assert result.manifest["expected_object_count"] == 0
    assert "no reachable" in (result.component.message or "")


def test_archive_lfs_without_tooling_reports_the_exact_missing_objects(
    tmp_path: Path,
) -> None:
    oid = "c" * 64
    runner = Mock(spec=GitRunner)
    runner.lfs.return_value = command(stderr="git-lfs not found", returncode=127)

    with patch(
        "repo_archive.lfs.enumerate_lfs_oids", return_value=LfsInventory((oid,))
    ):
        result = archive_lfs(tmp_path, runner, enabled=True)

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["tooling_available"] is False
    assert result.manifest["expected_object_count"] == 1
    assert result.manifest["missing_objects"] == [oid]
    assert "git-lfs is unavailable" in (result.component.message or "")


def test_archive_lfs_without_tooling_is_complete_when_objects_are_present(
    tmp_path: Path,
) -> None:
    oid = store_object(tmp_path, b"already archived payload\n")
    runner = Mock(spec=GitRunner)
    runner.lfs.return_value = command(stderr="git-lfs not found", returncode=127)

    with patch(
        "repo_archive.lfs.enumerate_lfs_oids", return_value=LfsInventory((oid,))
    ):
        result = archive_lfs(tmp_path, runner, enabled=True)

    assert result.component.status is ComponentStatus.COMPLETE
    assert result.manifest["status"] == "complete"
    assert result.manifest["tooling_available"] is False
    assert result.manifest["missing_objects"] == []


def test_archive_lfs_quantifies_the_gap_skipped_by_no_lfs(tmp_path: Path) -> None:
    runner = Mock(spec=GitRunner)

    with patch(
        "repo_archive.lfs.enumerate_lfs_oids",
        return_value=LfsInventory(("d" * 64, "e" * 64)),
    ):
        result = archive_lfs(tmp_path, runner, enabled=False)

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["reason"] == "disabled"
    assert result.manifest["expected_object_count"] == 2
    runner.lfs.assert_not_called()


def test_lfs_staging_falls_back_to_copy_when_hard_linking_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"lfs object")

    with (
        patch("repo_archive.lfs.os.link", side_effect=OSError("not supported")),
        patch("repo_archive.lfs.shutil.copy2", wraps=shutil.copy2) as copy2,
    ):
        result = _link_or_copy(str(source), str(destination))

    assert result == str(destination)
    assert destination.read_bytes() == b"lfs object"
    copy2.assert_called_once_with(str(source), str(destination))
