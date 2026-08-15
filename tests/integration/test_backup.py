"""Integration coverage for native Git mirror archive operations."""

from __future__ import annotations

import subprocess
from pathlib import Path

from repo_archive.archive import ArchiveLayout, backup_archive, update_archive
from repo_archive.manifest import load_manifest
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
