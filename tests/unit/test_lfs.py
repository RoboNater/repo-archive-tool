"""Tests for Git LFS detection and archive-local object verification."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

from repo_archive.git import CommandResult, GitRunner
from repo_archive.lfs import LfsDetection, detect_lfs, verify_lfs_objects
from repo_archive.results import ComponentStatus


def command(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(("git",), returncode, stdout, stderr)


def test_detect_lfs_inspects_historical_attribute_blobs(tmp_path: Path) -> None:
    runner = Mock(spec=GitRunner)
    runner.git.side_effect = [
        command("a" * 40 + " old/.gitattributes\n" + "b" * 40 + " README\n"),
        command("# comment\n*.bin filter=lfs diff=lfs merge=lfs -text\n"),
    ]

    detection = detect_lfs(tmp_path, runner)

    assert detection == LfsDetection(detected=True, attribute_files=1)
    runner.git.assert_any_call("cat-file", "blob", "a" * 40, cwd=tmp_path)


def test_verify_lfs_objects_reports_missing_objects(tmp_path: Path) -> None:
    oid = "a" * 64
    runner = Mock(spec=GitRunner)
    runner.lfs.return_value = command(f"{oid} - assets/file.bin\n")

    result = verify_lfs_objects(tmp_path, runner)

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["reason"] == "objects-incomplete"
    assert result.manifest["missing_objects"] == [oid]


def test_verify_lfs_objects_hashes_archive_local_content(tmp_path: Path) -> None:
    content = b"archived lfs content\n"
    oid = hashlib.sha256(content).hexdigest()
    object_path = tmp_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(content)
    runner = Mock(spec=GitRunner)
    runner.lfs.return_value = command(f"{oid} * assets/file.bin\n")

    result = verify_lfs_objects(tmp_path, runner)

    assert result.component.status is ComponentStatus.COMPLETE
    assert result.manifest["expected_object_count"] == 1
    assert result.manifest["missing_objects"] == []


def test_verify_lfs_objects_reports_corrupt_content(tmp_path: Path) -> None:
    oid = "a" * 64
    object_path = tmp_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"not the expected object")
    runner = Mock(spec=GitRunner)
    runner.lfs.return_value = command(f"{oid} * assets/file.bin\n")

    result = verify_lfs_objects(tmp_path, runner)

    assert result.component.status is ComponentStatus.PARTIAL
    assert result.manifest["corrupt_objects"] == [oid]
