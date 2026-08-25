"""Tests for backup path-policy resolution and legacy compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_archive.archive import resolve_backup_layout
from repo_archive.manifest import Manifest, write_json_atomic
from repo_archive.remote import derive_archive_path, normalize_remote


def _write_manifest(path: Path, source: str) -> None:
    path.mkdir(parents=True)
    manifest = Manifest.new(normalize_remote(source))
    write_json_atomic(path / "manifest.json", manifest.to_dict())


def test_easy_mode_reuses_the_exact_legacy_digest_path(tmp_path: Path) -> None:
    source = "https://example.test/team/repo.git"
    remote = normalize_remote(source)
    legacy = derive_archive_path(tmp_path, remote, naming="pedantic")
    _write_manifest(legacy, source)

    layout = resolve_backup_layout(tmp_path, remote)

    assert layout.path == legacy


def test_easy_mode_finds_a_legacy_path_created_through_another_transport(
    tmp_path: Path,
) -> None:
    old_source = "ssh://alice@example.test/team/repo.git"
    requested = normalize_remote("https://example.test/team/repo.git")
    legacy = derive_archive_path(
        tmp_path, normalize_remote(old_source), naming="pedantic"
    )
    _write_manifest(legacy, old_source)

    layout = resolve_backup_layout(tmp_path, requested)

    assert layout.path == legacy


def test_easy_mode_rejects_ambiguous_matching_legacy_paths(tmp_path: Path) -> None:
    for user in ("alice", "bob"):
        source = f"ssh://{user}@example.test/team/repo.git"
        legacy = derive_archive_path(
            tmp_path, normalize_remote(source), naming="pedantic"
        )
        _write_manifest(legacy, source)

    with pytest.raises(ValueError, match="Multiple legacy archives"):
        resolve_backup_layout(
            tmp_path, normalize_remote("https://example.test/team/repo.git")
        )


def test_pedantic_and_custom_modes_do_not_search_for_legacy_paths(
    tmp_path: Path,
) -> None:
    remote = normalize_remote("https://example.test/team/repo.git")

    assert resolve_backup_layout(tmp_path, remote, naming="pedantic").path == (
        derive_archive_path(tmp_path, remote, naming="pedantic")
    )
    assert resolve_backup_layout(tmp_path, remote, name="daily archive").path == (
        tmp_path / "daily-archive"
    )


def test_easy_mode_reuses_a_local_legacy_path(tmp_path: Path) -> None:
    source_path = tmp_path / "source" / "repo.git"
    remote = normalize_remote(str(source_path))
    legacy = derive_archive_path(tmp_path / "archives", remote, naming="pedantic")
    _write_manifest(legacy, str(source_path))

    layout = resolve_backup_layout(tmp_path / "archives", remote)

    assert layout.path == legacy
