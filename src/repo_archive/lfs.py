"""Git LFS detection, archival fetch, and local-object verification."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repo_archive.git import CommandResult, GitRunner
from repo_archive.results import ComponentResult, ComponentStatus

_LFS_ATTRIBUTE = re.compile(r"(?:^|\s)filter=lfs(?:\s|$)")
_LFS_FILE = re.compile(r"^([0-9a-fA-F]{64})\s+[-*]\s+")


@dataclass(frozen=True)
class LfsDetection:
    """Result of inspecting reachable ``.gitattributes`` blobs."""

    detected: bool
    attribute_files: int


@dataclass(frozen=True)
class LfsArchiveResult:
    """Manifest state and operation component for Git LFS work."""

    manifest: dict[str, object]
    component: ComponentResult
    promotable: bool = True


def detect_lfs(mirror_path: Path, runner: GitRunner) -> LfsDetection | CommandResult:
    """Detect LFS attributes anywhere in history reachable from archived refs."""
    attribute_oids: set[str] = set()

    def collect_attribute_oid(line: str) -> None:
        oid, separator, path = line.partition(" ")
        if separator and PurePosixPath(path).name == ".gitattributes":
            attribute_oids.add(oid)

    objects = runner.git_stream_stdout(
        "-c",
        "core.quotePath=false",
        "rev-list",
        "--objects",
        "--all",
        cwd=mirror_path,
        on_line=collect_attribute_oid,
    )
    if not objects.succeeded:
        return objects

    for oid in sorted(attribute_oids):
        content = runner.git("cat-file", "blob", oid, cwd=mirror_path)
        if not content.succeeded:
            return content
        if _uses_lfs(content.stdout):
            return LfsDetection(True, len(attribute_oids))
    return LfsDetection(False, len(attribute_oids))


def archive_lfs(
    mirror_path: Path,
    runner: GitRunner,
    *,
    enabled: bool,
    previous_mirror: Path | None = None,
) -> LfsArchiveResult:
    """Fetch and verify all LFS objects reachable from the staged mirror."""
    copied_error = _copy_previous_store(previous_mirror, mirror_path)
    if copied_error is not None:
        return LfsArchiveResult(
            {
                "detected": None,
                "status": "failed",
                "reason": "storage-copy-failed",
            },
            ComponentResult("lfs", ComponentStatus.FAILED, copied_error),
            promotable=False,
        )

    detection = detect_lfs(mirror_path, runner)
    if isinstance(detection, CommandResult):
        return _partial(
            None,
            "Git LFS use could not be determined: " + _command_message(detection),
            reason="detection-failed",
        )
    if not detection.detected:
        return LfsArchiveResult(
            {
                "detected": False,
                "status": "not-applicable",
                "attribute_files_inspected": detection.attribute_files,
            },
            ComponentResult(
                "lfs", ComponentStatus.COMPLETE, "No Git LFS usage detected."
            ),
        )

    if not enabled:
        return _partial(
            True,
            "Git LFS is in use, but --no-lfs intentionally skipped object fetching.",
            reason="disabled",
        )

    version = runner.lfs("version", cwd=mirror_path)
    if not version.succeeded:
        return _partial(
            True,
            "Git LFS is in use, but git-lfs is unavailable: "
            + _command_message(version),
            reason="tool-unavailable",
            tooling_available=False,
        )

    storage = runner.git("config", "lfs.storage", "lfs", cwd=mirror_path)
    if not storage.succeeded:
        return _partial(
            True,
            "Could not configure archive-local LFS storage: "
            + _command_message(storage),
            reason="storage-configuration-failed",
        )

    fetched = runner.lfs("fetch", "--all", cwd=mirror_path)
    if not fetched.succeeded:
        return _partial(
            True,
            "Git LFS fetch failed: " + _command_message(fetched),
            reason="fetch-failed",
            tooling_available=True,
        )

    verified = verify_lfs_objects(mirror_path, runner)
    if verified.component.status == ComponentStatus.COMPLETE:
        return LfsArchiveResult(
            verified.manifest,
            ComponentResult(
                "lfs",
                ComponentStatus.COMPLETE,
                "Git LFS fetch and verification passed for "
                f"{verified.manifest['expected_object_count']} objects.",
            ),
        )
    return verified


def verify_lfs_archive(mirror_path: Path, runner: GitRunner) -> ComponentResult:
    """Verify LFS completeness without fetching or changing archived refs."""
    detection = detect_lfs(mirror_path, runner)
    if isinstance(detection, CommandResult):
        return ComponentResult(
            "lfs",
            ComponentStatus.PARTIAL,
            "Git LFS use could not be determined: " + _command_message(detection),
        )
    if not detection.detected:
        return ComponentResult(
            "lfs", ComponentStatus.COMPLETE, "No Git LFS usage detected."
        )
    version = runner.lfs("version", cwd=mirror_path)
    if not version.succeeded:
        return ComponentResult(
            "lfs",
            ComponentStatus.PARTIAL,
            "Git LFS is in use, but git-lfs is unavailable: "
            + _command_message(version),
        )
    return verify_lfs_objects(mirror_path, runner).component


def verify_lfs_objects(mirror_path: Path, runner: GitRunner) -> LfsArchiveResult:
    """Enumerate historical LFS pointers and hash their archive-local objects."""
    listed = runner.lfs("ls-files", "--all", "--long", cwd=mirror_path)
    if not listed.succeeded:
        return _partial(
            True,
            "Git LFS pointer enumeration failed: " + _command_message(listed),
            reason="pointer-enumeration-failed",
            tooling_available=True,
        )

    expected: set[str] = set()
    unrecognized: list[str] = []
    for line in listed.stdout.splitlines():
        if not line.strip():
            continue
        match = _LFS_FILE.match(line)
        if match is None:
            unrecognized.append(line)
        else:
            expected.add(match.group(1).lower())
    if unrecognized:
        return _partial(
            True,
            "Git LFS returned unrecognized pointer information.",
            reason="pointer-output-invalid",
            tooling_available=True,
            expected_object_count=len(expected),
        )

    missing: list[str] = []
    corrupt: list[str] = []
    for oid in sorted(expected):
        object_path = mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        if not object_path.is_file():
            missing.append(oid)
        else:
            try:
                actual_oid = _sha256(object_path)
            except OSError:
                corrupt.append(oid)
            else:
                if actual_oid != oid:
                    corrupt.append(oid)

    if missing or corrupt:
        details = []
        if missing:
            details.append(f"{len(missing)} missing")
        if corrupt:
            details.append(f"{len(corrupt)} corrupt")
        return _partial(
            True,
            "Git LFS object verification found " + " and ".join(details) + ".",
            reason="objects-incomplete",
            tooling_available=True,
            expected_object_count=len(expected),
            missing_objects=missing,
            corrupt_objects=corrupt,
        )

    return LfsArchiveResult(
        {
            "detected": True,
            "status": "complete",
            "tooling_available": True,
            "expected_object_count": len(expected),
            "missing_objects": [],
            "corrupt_objects": [],
        },
        ComponentResult(
            "lfs",
            ComponentStatus.COMPLETE,
            f"Git LFS object verification passed for {len(expected)} objects.",
        ),
    )


def _uses_lfs(content: str) -> bool:
    return any(
        _LFS_ATTRIBUTE.search(line) is not None
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _copy_previous_store(previous_mirror: Path | None, mirror_path: Path) -> str | None:
    if previous_mirror is None:
        return None
    source = previous_mirror / "lfs"
    if not source.is_dir():
        return None
    try:
        shutil.copytree(
            source,
            mirror_path / "lfs",
            copy_function=_link_or_copy,
            dirs_exist_ok=True,
        )
    except OSError as error:
        return f"Could not preserve the previous LFS object store: {error}"
    return None


def _link_or_copy(source: str, destination: str) -> str:
    """Hard-link immutable LFS content, falling back to a normal file copy."""
    try:
        os.link(source, destination)
    except OSError:
        return shutil.copy2(source, destination)
    return destination


def _partial(
    detected: bool | None,
    message: str,
    *,
    reason: str,
    tooling_available: bool | None = None,
    expected_object_count: int | None = None,
    missing_objects: list[str] | None = None,
    corrupt_objects: list[str] | None = None,
) -> LfsArchiveResult:
    manifest: dict[str, object] = {
        "detected": detected,
        "status": "partial",
        "reason": reason,
    }
    if tooling_available is not None:
        manifest["tooling_available"] = tooling_available
    if expected_object_count is not None:
        manifest["expected_object_count"] = expected_object_count
    if missing_objects is not None:
        manifest["missing_objects"] = missing_objects
    if corrupt_objects is not None:
        manifest["corrupt_objects"] = corrupt_objects
    return LfsArchiveResult(
        manifest,
        ComponentResult("lfs", ComponentStatus.PARTIAL, message),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_message(command: CommandResult) -> str:
    return (
        command.stderr.strip()
        or command.stdout.strip()
        or f"command failed with exit code {command.returncode}"
    )
