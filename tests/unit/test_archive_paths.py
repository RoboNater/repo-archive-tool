"""Tests for backup path-policy resolution and legacy compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_archive.archive import resolve_backup_layout
from repo_archive.manifest import Manifest, write_json_atomic
from repo_archive.remote import derive_archive_path, normalize_remote


def _write_manifest(path: Path, source: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
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

    with pytest.raises(ValueError, match="--no-legacy-reuse"):
        resolve_backup_layout(
            tmp_path, normalize_remote("https://example.test/team/repo.git")
        )

    easy = resolve_backup_layout(
        tmp_path,
        normalize_remote("https://example.test/team/repo.git"),
        reuse_legacy=False,
    )
    assert easy.path == tmp_path / "example.test" / "team" / "repo"


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


def test_easy_mode_refuses_to_nest_a_child_inside_an_existing_archive(
    tmp_path: Path,
) -> None:
    parent_source = "https://gitlab.test/group/repo.git"
    parent = derive_archive_path(tmp_path, normalize_remote(parent_source))
    _write_manifest(parent, parent_source)

    with pytest.raises(ValueError, match="Choose --name") as error:
        resolve_backup_layout(
            tmp_path, normalize_remote("https://gitlab.test/group/repo/sub.git")
        )

    assert "--naming pedantic" not in str(error.value)


def test_archive_root_requires_a_different_root_for_nested_backup(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path, "https://gitlab.test/group/repo.git")

    with pytest.raises(ValueError, match="Choose a different --root") as error:
        resolve_backup_layout(
            tmp_path, normalize_remote("https://gitlab.test/group/other.git")
        )

    assert "--name" not in str(error.value)
    assert "--naming pedantic" not in str(error.value)


def test_easy_mode_refuses_to_make_a_parent_of_an_existing_child_archive(
    tmp_path: Path,
) -> None:
    child_source = "https://gitlab.test/group/repo/sub.git"
    child = derive_archive_path(tmp_path, normalize_remote(child_source))
    _write_manifest(child, child_source)

    with pytest.raises(ValueError, match="would contain"):
        resolve_backup_layout(
            tmp_path, normalize_remote("https://gitlab.test/group/repo.git")
        )


def test_existing_parent_archive_refuses_an_already_nested_child(
    tmp_path: Path,
) -> None:
    parent_source = "https://gitlab.test/group/repo.git"
    child_source = "https://gitlab.test/group/repo/sub.git"
    _write_manifest(
        derive_archive_path(tmp_path, normalize_remote(parent_source)), parent_source
    )
    _write_manifest(
        derive_archive_path(tmp_path, normalize_remote(child_source)), child_source
    )

    with pytest.raises(ValueError, match="Move the nested archive") as error:
        resolve_backup_layout(tmp_path, normalize_remote(parent_source))

    assert "without repairing the layout" in str(error.value)
    assert "run update directly" in str(error.value)
    assert "--name" not in str(error.value)
    assert "--naming pedantic" not in str(error.value)


@pytest.mark.parametrize(
    "scratch_name", [".mirror-staging-crash", ".mirror-previous-crash"]
)
def test_tool_scratch_directories_do_not_count_as_nested_archives(
    tmp_path: Path, scratch_name: str
) -> None:
    source = "https://gitlab.test/group/repo.git"
    archive = derive_archive_path(tmp_path, normalize_remote(source))
    _write_manifest(archive, source)
    (archive / "leftovers" / scratch_name / "mirror.git").mkdir(parents=True)

    layout = resolve_backup_layout(tmp_path, normalize_remote(source))

    assert layout.path == archive
