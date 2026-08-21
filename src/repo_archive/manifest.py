"""Versioned archive manifests and atomic file persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repo_archive import __version__
from repo_archive.remote import Remote, display_remote

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Manifest:
    """The initial, versioned archive-manifest schema."""

    source: dict[str, Any]
    archive: dict[str, Any]
    git: dict[str, Any]
    lfs: dict[str, Any]
    submodules: dict[str, Any]
    metadata: dict[str, Any]
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def new(cls, remote: Remote, created_at: datetime | None = None) -> Manifest:
        """Create a manifest with required components in their initial state."""
        timestamp = _timestamp(created_at)
        return cls(
            source={
                "url": display_remote(remote),
                "host": remote.host,
                "owner": remote.owner,
                "repository": remote.repository,
            },
            archive={
                "created_at": timestamp,
                "last_updated_at": timestamp,
                "tool_version": __version__,
                "snapshots": [],
            },
            git={
                "status": "not-run",
                "mirror_path": "mirror.git",
                "ref_count": 0,
                "head": None,
            },
            lfs={"detected": False, "status": "not-applicable"},
            submodules={
                "detected": False,
                "status": "not-applicable",
                "repositories": [],
            },
            metadata={"github": {"requested": False, "status": "not-run"}},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this schema in its stable, machine-readable shape."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Manifest:
        """Read and validate a manifest from parsed JSON."""
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported manifest schema version.")
        required = ("source", "archive", "git", "lfs", "submodules", "metadata")
        missing = [field for field in required if field not in value]
        if missing:
            raise ValueError(
                f"Manifest is missing required fields: {', '.join(missing)}"
            )
        return cls(
            **{field: value[field] for field in required}, schema_version=SCHEMA_VERSION
        )


def write_json_atomic(path: Path, value: object) -> None:
    """Atomically write UTF-8, formatted JSON at *path*."""
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_text_atomic(path: Path, value: str) -> None:
    """Atomically replace *path* after fully writing a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def load_manifest(path: Path) -> Manifest:
    """Load a version-supported manifest from disk."""
    with path.open(encoding="utf-8") as stream:
        return Manifest.from_dict(json.load(stream))


def _timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(UTC)
    return (
        timestamp.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
