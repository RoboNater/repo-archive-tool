"""Integration coverage for native Git mirror archive operations."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from repo_archive.archive import (
    ArchiveLayout,
    _remove_readonly,
    backup_archive,
    update_archive,
)
from repo_archive.git import CommandResult, GitRunner
from repo_archive.inspection import info_archive, verify_archive
from repo_archive.manifest import Manifest, load_manifest, write_json_atomic
from repo_archive.remote import normalize_remote
from repo_archive.results import Outcome


def git(*arguments: str, cwd: Path | None = None) -> str:
    """Run Git in a test repository and return its standard output."""
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class DetectionFailureRunner(GitRunner):
    """Use real Git except for the LFS attribute-history query."""

    def git_stream_stdout(
        self,
        *arguments: str,
        cwd: Path | str | None = None,
        on_line: Callable[[str], None],
    ) -> CommandResult:
        if "rev-list" in arguments and "--objects" in arguments:
            return CommandResult(
                ("git", *arguments), 1, "", "simulated rev-list failure"
            )
        return super().git_stream_stdout(*arguments, cwd=cwd, on_line=on_line)


def create_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a local remote with branches and both kinds of tag."""
    remote = tmp_path / "remote.git"
    worktree = tmp_path / "worktree"
    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(worktree))
    git("config", "user.name", "Archive Test", cwd=worktree)
    git("config", "user.email", "archive@example.test", cwd=worktree)
    (worktree / "README.md").write_text("first\n", encoding="utf-8")
    git("add", "README.md", cwd=worktree)
    git("commit", "-m", "initial", cwd=worktree)
    git("remote", "add", "origin", str(remote), cwd=worktree)
    git("push", "-u", "origin", "main", cwd=worktree)
    git("tag", "lightweight", cwd=worktree)
    git("tag", "-a", "annotated", "-m", "annotated tag", cwd=worktree)
    git("push", "origin", "--tags", cwd=worktree)
    git("switch", "-c", "stale", cwd=worktree)
    git("push", "-u", "origin", "stale", cwd=worktree)
    git("switch", "main", cwd=worktree)
    return remote, worktree


def test_backup_updates_and_prunes_a_staged_mirror(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    created = backup_archive(str(remote), layout)

    assert created.outcome is Outcome.COMPLETE
    assert git("show-ref", "--verify", "refs/heads/main", cwd=layout.mirror_path)
    assert git("show-ref", "--verify", "refs/tags/lightweight", cwd=layout.mirror_path)
    assert git("show-ref", "--verify", "refs/tags/annotated", cwd=layout.mirror_path)
    initial_manifest = load_manifest(layout.manifest_path)
    assert initial_manifest.git["status"] == "complete"
    assert initial_manifest.git["ref_count"] >= 4

    (worktree / "README.md").write_text("second\n", encoding="utf-8")
    git("commit", "-am", "second", cwd=worktree)
    git("push", cwd=worktree)
    git("push", "origin", "--delete", "stale", cwd=worktree)

    updated = backup_archive(str(remote), layout)

    assert updated.outcome is Outcome.COMPLETE
    assert git("rev-parse", "main", cwd=layout.mirror_path) == git(
        "rev-parse", "main", cwd=worktree
    )
    missing = subprocess.run(
        ("git", "show-ref", "--verify", "refs/heads/stale"),
        cwd=layout.mirror_path,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 128
    updated_manifest = load_manifest(layout.manifest_path)
    assert (
        updated_manifest.archive["created_at"] == initial_manifest.archive["created_at"]
    )
    assert updated_manifest.git["ref_count"] < initial_manifest.git["ref_count"] + 2


def test_failed_update_keeps_the_last_valid_mirror(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    previous_head = git("rev-parse", "main", cwd=layout.mirror_path)
    git(
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "missing.git"),
        cwd=layout.mirror_path,
    )

    result = update_archive(layout)

    assert result.outcome is Outcome.FAILED
    assert git("rev-parse", "main", cwd=layout.mirror_path) == previous_head
    assert git("fsck", "--full", cwd=layout.mirror_path) == ""
    report = (layout.reports_path / "latest.json").read_text(encoding="utf-8")
    assert '"operation": "update"' in report
    assert '"outcome": "failed"' in report


def test_info_and_full_verification_report_archive_state(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    info = info_archive(layout)
    assert info.outcome is Outcome.COMPLETE
    assert any(component.name == "snapshots" for component in info.components)

    verification = verify_archive(layout)
    assert verification.outcome is Outcome.COMPLETE
    manifest = load_manifest(layout.manifest_path)
    assert manifest.archive["last_verified_at"]
    assert manifest.archive["last_verification_mode"] == "full"
    assert '"operation": "verify"' in (layout.reports_path / "latest.json").read_text(
        encoding="utf-8"
    )


def test_quick_verification_skips_object_integrity(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE

    verification = verify_archive(layout, full=False)

    assert verification.outcome is Outcome.COMPLETE
    assert not any(
        component.name == "git integrity" for component in verification.components
    )


def test_quick_verification_rejects_a_non_bare_mirror(tmp_path: Path) -> None:
    remote, _ = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    shutil.rmtree(layout.mirror_path, onerror=_remove_readonly)
    git("clone", str(remote), str(layout.mirror_path))

    verification = verify_archive(layout, full=False)

    structure = next(
        component
        for component in verification.components
        if component.name == "repository structure"
    )
    assert structure.status.value == "failed"
    assert verification.outcome is Outcome.FAILED
    assert verification.exit_code == 2


def test_backup_refuses_to_replace_a_named_archive_from_another_source(
    tmp_path: Path,
) -> None:
    first_remote, _ = create_remote(tmp_path / "first")
    second_remote, _ = create_remote(tmp_path / "second")
    layout = ArchiveLayout(tmp_path / "archives" / "daily")
    assert backup_archive(str(first_remote), layout).outcome is Outcome.COMPLETE
    original_head = git("rev-parse", "main", cwd=layout.mirror_path)

    result = backup_archive(str(second_remote), layout)

    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 2
    assert git("rev-parse", "main", cwd=layout.mirror_path) == original_head
    assert load_manifest(layout.manifest_path).source["url"] == first_remote.as_uri()


def test_update_identity_failure_preserves_its_operation_name(tmp_path: Path) -> None:
    first_remote, _ = create_remote(tmp_path / "first")
    second_remote, _ = create_remote(tmp_path / "second")
    layout = ArchiveLayout(tmp_path / "archives" / "daily")
    assert backup_archive(str(first_remote), layout).outcome is Outcome.COMPLETE
    manifest = load_manifest(layout.manifest_path).to_dict()
    manifest["source"] = Manifest.new(normalize_remote(str(second_remote))).source
    write_json_atomic(layout.manifest_path, manifest)

    result = update_archive(layout)

    assert result.operation == "update"
    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 2


def test_no_lfs_marks_historical_lfs_use_as_partial(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    (worktree / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    git("add", ".gitattributes", cwd=worktree)
    git("commit", "-m", "declare lfs attributes", cwd=worktree)
    git("push", cwd=worktree)
    git("rm", ".gitattributes", cwd=worktree)
    git("commit", "-m", "remove current attributes", cwd=worktree)
    git("push", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    result = backup_archive(str(remote), layout, lfs_enabled=False)

    assert result.outcome is Outcome.PARTIAL
    assert result.exit_code == 3
    manifest = load_manifest(layout.manifest_path)
    assert manifest.lfs["detected"] is True
    assert manifest.lfs["status"] == "partial"
    assert manifest.lfs["reason"] == "disabled"
    verification = verify_archive(layout)
    assert verification.outcome is Outcome.PARTIAL


def test_nested_lfs_attributes_are_detected(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    attributes = worktree / "assets" / ".gitattributes"
    attributes.parent.mkdir()
    attributes.write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    git("add", "assets/.gitattributes", cwd=worktree)
    git("commit", "-m", "scope lfs to assets", cwd=worktree)
    git("push", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    result = backup_archive(str(remote), layout, lfs_enabled=False)

    assert result.outcome is Outcome.PARTIAL
    assert load_manifest(layout.manifest_path).lfs["detected"] is True


def test_non_utf8_attributes_do_not_escape_structured_results(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    (worktree / ".gitattributes").write_bytes(
        b"*.bin filter=lfs diff=lfs merge=lfs -text\n# invalid: \xff\n"
    )
    git("add", ".gitattributes", cwd=worktree)
    git("commit", "-m", "add non-utf8 attributes", cwd=worktree)
    git("push", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    result = backup_archive(str(remote), layout, lfs_enabled=False)

    assert result.outcome is Outcome.PARTIAL
    assert (layout.reports_path / "latest.json").is_file()


def test_detection_failure_preserves_existing_lfs_store(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    archived_object = layout.mirror_path / "lfs" / "objects" / "existing"
    archived_object.parent.mkdir(parents=True)
    archived_object.write_bytes(b"preserve me")
    (worktree / "README.md").write_text("new git state\n", encoding="utf-8")
    git("commit", "-am", "new git state", cwd=worktree)
    git("push", cwd=worktree)

    result = update_archive(layout, runner=DetectionFailureRunner())

    assert result.outcome is Outcome.PARTIAL
    assert archived_object.read_bytes() == b"preserve me"
    assert git("rev-parse", "main", cwd=layout.mirror_path) == git(
        "rev-parse", "main", cwd=worktree
    )


def test_lfs_copy_failure_does_not_promote_staged_update(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    layout = ArchiveLayout(tmp_path / "archives" / "project")
    assert backup_archive(str(remote), layout).outcome is Outcome.COMPLETE
    previous_head = git("rev-parse", "main", cwd=layout.mirror_path)
    previous_manifest = layout.manifest_path.read_bytes()
    archived_object = layout.mirror_path / "lfs" / "objects" / "existing"
    archived_object.parent.mkdir(parents=True)
    archived_object.write_bytes(b"preserve me")
    (worktree / "README.md").write_text("unpromoted state\n", encoding="utf-8")
    git("commit", "-am", "unpromoted state", cwd=worktree)
    git("push", cwd=worktree)

    with patch("repo_archive.lfs.shutil.copytree", side_effect=OSError("disk full")):
        result = update_archive(layout)

    assert result.outcome is Outcome.FAILED
    assert git("rev-parse", "main", cwd=layout.mirror_path) == previous_head
    assert archived_object.read_bytes() == b"preserve me"
    assert layout.manifest_path.read_bytes() == previous_manifest
    assert [component.name for component in result.components] == [
        "git mirror",
        "git refs",
        "git integrity",
        "lfs",
        "submodules",
        "manifest",
    ]
    assert all(component.status.value != "complete" for component in result.components)


def test_missing_git_lfs_tool_marks_detected_repository_partial(
    tmp_path: Path,
) -> None:
    remote, worktree = create_remote(tmp_path)
    (worktree / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )
    git("add", ".gitattributes", cwd=worktree)
    git("commit", "-m", "declare lfs attributes", cwd=worktree)
    git("push", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    result = backup_archive(
        str(remote),
        layout,
        runner=GitRunner(git_lfs_executable="missing-git-lfs-for-test"),
    )

    assert result.outcome is Outcome.PARTIAL
    manifest = load_manifest(layout.manifest_path)
    assert manifest.lfs["reason"] == "tool-unavailable"
    assert manifest.lfs["tooling_available"] is False


def test_submodules_are_recorded_and_reported_as_a_warning(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    pinned_commit = "1" * 40
    (worktree / ".gitmodules").write_text(
        '[submodule "my library"]\n'
        '\tpath = "vendor/library"\n'
        '\turl = "../library.git"\n',
        encoding="utf-8",
    )
    git("add", ".gitmodules", cwd=worktree)
    git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{pinned_commit},vendor/library",
        cwd=worktree,
    )
    git("commit", "-m", "add submodule", cwd=worktree)
    git("push", cwd=worktree)
    git("switch", "-c", "malformed-submodule", cwd=worktree)
    (worktree / ".gitmodules").write_text(
        '[submodule "incomplete"]\n\tpath = vendor/incomplete\n',
        encoding="utf-8",
    )
    git("add", ".gitmodules", cwd=worktree)
    git("commit", "-m", "leave an incomplete historical definition", cwd=worktree)
    git("push", "-u", "origin", "malformed-submodule", cwd=worktree)
    git("switch", "main", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    result = backup_archive(str(remote), layout)

    assert result.outcome is Outcome.COMPLETE_WITH_WARNINGS
    manifest = load_manifest(layout.manifest_path)
    assert manifest.submodules["detected"] is True
    assert manifest.submodules["status"] == "not-archived"
    repository = manifest.submodules["repositories"][0]
    assert repository["path"] == "vendor/library"
    assert repository["url"] == "../library.git"
    assert pinned_commit in repository["commits"]
    assert "refs/heads/main" in repository["refs"]
    assert manifest.submodules["inspection_warnings"]
    report = (layout.reports_path / "latest.txt").read_text(encoding="utf-8")
    assert "vendor/library -> ../library.git" in report
    assert pinned_commit in report


@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not installed")
def test_lfs_objects_are_fetched_and_verified_when_available(tmp_path: Path) -> None:
    remote, worktree = create_remote(tmp_path)
    git("lfs", "install", "--local", cwd=worktree)
    git("lfs", "track", "*.bin", cwd=worktree)
    (worktree / "asset.bin").write_bytes(b"archived lfs object\n")
    git("add", ".gitattributes", "asset.bin", cwd=worktree)
    git("commit", "-m", "add lfs object", cwd=worktree)
    git("push", cwd=worktree)
    layout = ArchiveLayout(tmp_path / "archives" / "project")

    result = backup_archive(str(remote), layout)

    assert result.outcome is Outcome.COMPLETE
    manifest = load_manifest(layout.manifest_path)
    assert manifest.lfs["status"] == "complete"
    assert manifest.lfs["expected_object_count"] == 1
    verification = verify_archive(layout)
    assert verification.outcome is Outcome.COMPLETE

    archived_objects = {
        path.relative_to(layout.mirror_path)
        for path in (layout.mirror_path / "lfs" / "objects").rglob("*")
        if path.is_file()
    }
    skipped = update_archive(layout, lfs_enabled=False)
    assert skipped.outcome is Outcome.PARTIAL
    assert archived_objects
    assert all((layout.mirror_path / path).is_file() for path in archived_objects)
