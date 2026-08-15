"""Archive-set layout management."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArchiveLayout:
    """Paths belonging to one archive set."""

    path: Path

    @property
    def mirror_path(self) -> Path:
        return self.path / "mirror.git"

    @property
    def manifest_path(self) -> Path:
        return self.path / "manifest.json"

    @property
    def reports_path(self) -> Path:
        return self.path / "reports"

    @property
    def snapshots_path(self) -> Path:
        return self.path / "snapshots"

    @property
    def metadata_path(self) -> Path:
        return self.path / "metadata"

    def ensure_directories(self) -> None:
        """Create the non-Git archive-set directories if they do not exist."""
        for directory in (
            self.path,
            self.reports_path,
            self.snapshots_path,
            self.metadata_path,
        ):
            directory.mkdir(parents=True, exist_ok=True)
