"""Tests for archive layout and manifest persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from repo_archive.archive import ArchiveLayout
from repo_archive.manifest import (
    Manifest,
    load_manifest,
    write_json_atomic,
    write_text_atomic,
)
from repo_archive.remote import normalize_remote


def test_archive_layout_creates_all_non_git_directories(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "archive")
    layout.ensure_directories()

    assert layout.path.is_dir()
    assert layout.reports_path.is_dir()
    assert layout.snapshots_path.is_dir()
    assert layout.metadata_path.is_dir()
    assert not layout.mirror_path.exists()


def test_manifest_round_trips_with_versioned_schema(tmp_path: Path) -> None:
    manifest = Manifest.new(
        normalize_remote("https://secret@example.test/team/repo.git"),
        datetime(2026, 8, 15, tzinfo=UTC),
    )
    path = tmp_path / "manifest.json"
    write_json_atomic(path, manifest.to_dict())

    loaded = load_manifest(path)

    assert loaded == manifest
    assert loaded.source["url"] == "https://example.test/team/repo.git"
    assert loaded.archive["created_at"] == "2026-08-15T00:00:00Z"


def test_atomic_writers_replace_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "value.txt"
    write_text_atomic(path, "first")
    write_text_atomic(path, "second")
    assert path.read_text(encoding="utf-8") == "second"

    json_path = tmp_path / "value.json"
    write_json_atomic(json_path, {"value": 1})
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"value": 1}


def test_manifest_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        Manifest.from_dict({"schema_version": 2})
