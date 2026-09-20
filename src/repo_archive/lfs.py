"""Git LFS requirement enumeration, archival fetch, and local-object verification.

The set of LFS objects a Git history requires is determined here, by parsing
valid LFS pointer blobs reachable from every archived ref. That enumeration is
authoritative for archive status, snapshot creation, and restore, and it does
not depend on the ``git-lfs`` executable. Git LFS tooling fetches payloads; it
never decides which payloads should exist.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repo_archive.filesystem import sha256_file
from repo_archive.git import CommandResult, GitRunner
from repo_archive.results import ComponentResult, ComponentStatus

_LFS_ATTRIBUTE = re.compile(r"(?:^|\s)filter=lfs(?:\s|$)")
_MAX_POINTER_SIZE = 1024
_OID_LINE = re.compile(r"^oid sha256:([0-9a-fA-F]{64})$")
_SIZE_LINE = re.compile(r"^size ([0-9]+)$")
_EXTENSION_LINE = re.compile(r"^ext-[0-9]+-[A-Za-z0-9][A-Za-z0-9.-]* .+$")
_SUMMARIZED_OIDS = 5


@dataclass(frozen=True)
class LfsInventory:
    """Historical LFS objects required by all refs of one repository.

    ``required_oids`` is the authoritative requirement set. ``tracking_declared``
    and ``attribute_files`` describe the secondary ``.gitattributes`` signal,
    which is reported for operator context but never decides completeness.
    """

    required_oids: tuple[str, ...]
    attribute_files: int = 0
    tracking_declared: bool = False


@dataclass(frozen=True)
class LfsArchiveResult:
    """Manifest state and operation component for Git LFS work."""

    manifest: dict[str, object]
    component: ComponentResult
    promotable: bool = True


@dataclass(frozen=True)
class _LocalObjectState:
    """Which required objects are absent from or corrupt in a local store."""

    missing: list[str]
    corrupt: list[str]

    @property
    def intact(self) -> bool:
        return not self.missing and not self.corrupt


def enumerate_lfs_oids(
    repository: Path,
    runner: GitRunner,
    *,
    include_attributes: bool = False,
) -> LfsInventory | CommandResult:
    """Find valid LFS pointer blobs reachable from every ref without git-lfs.

    ``include_attributes`` additionally reads reachable ``.gitattributes`` blobs
    so callers can report declared ``filter=lfs`` tracking alongside the
    authoritative requirement set. Both signals come from one object walk.
    """
    required: set[str] = set()
    attribute_oids: set[str] = set()
    with (
        tempfile.TemporaryFile(mode="w+b") as object_ids,
        tempfile.TemporaryFile(mode="w+b") as candidates,
    ):

        def collect_object(line: str) -> None:
            oid, separator, path = line.partition(" ")
            if not oid:
                return
            object_ids.write(oid.encode("ascii") + b"\n")
            if (
                include_attributes
                and separator
                and PurePosixPath(path).name == ".gitattributes"
            ):
                attribute_oids.add(oid)

        listed = runner.git_stream_stdout(
            "-c",
            "core.quotePath=false",
            "rev-list",
            "--objects",
            "--all",
            cwd=repository,
            on_line=collect_object,
        )
        if not listed.succeeded:
            return listed
        object_ids.seek(0)

        def collect_candidate(line: str) -> None:
            parts = line.split()
            if len(parts) != 3 or parts[1] != "blob":
                return
            try:
                size = int(parts[2])
            except ValueError:
                return
            if size <= _MAX_POINTER_SIZE:
                candidates.write(parts[0].encode("ascii") + b"\n")

        checked = runner.git_stream_stdout(
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            cwd=repository,
            on_line=collect_candidate,
            input_stream=object_ids,
        )
        if not checked.succeeded:
            return checked
        candidates.seek(0)

        def inspect_blob(_oid: str, content: bytes) -> None:
            try:
                pointer = content.decode("ascii")
            except UnicodeDecodeError:
                return
            pointer_oid = parse_lfs_pointer(pointer)
            if pointer_oid is not None:
                required.add(pointer_oid)

        inspected = runner.git_batch_blobs(
            cwd=repository, object_ids=candidates, on_blob=inspect_blob
        )
        if not inspected.succeeded:
            return inspected

    tracking_declared = False
    for oid in sorted(attribute_oids):
        content = runner.git("cat-file", "blob", oid, cwd=repository)
        if not content.succeeded:
            return content
        if _uses_lfs(content.stdout):
            tracking_declared = True
            break

    return LfsInventory(
        tuple(sorted(required)),
        attribute_files=len(attribute_oids),
        tracking_declared=tracking_declared,
    )


def parse_lfs_pointer(content: str) -> str | None:
    """Return the OID named by a valid Git LFS pointer blob, or ``None``."""
    lines = content.splitlines()
    if not lines or lines[0] != "version https://git-lfs.github.com/spec/v1":
        return None
    index = 1
    while index < len(lines) and _EXTENSION_LINE.fullmatch(lines[index]):
        index += 1
    if index + 2 != len(lines):
        return None
    oid_match = _OID_LINE.fullmatch(lines[index])
    size_match = _SIZE_LINE.fullmatch(lines[index + 1])
    if oid_match is None or size_match is None:
        return None
    return oid_match.group(1).lower()


def archive_lfs(
    mirror_path: Path,
    runner: GitRunner,
    *,
    enabled: bool,
    previous_mirror: Path | None = None,
) -> LfsArchiveResult:
    """Fetch and verify all LFS objects required by the staged mirror."""
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

    inventory = enumerate_lfs_oids(mirror_path, runner, include_attributes=True)
    if isinstance(inventory, CommandResult):
        return _partial(
            None,
            "Git LFS requirements could not be determined: "
            + _command_message(inventory),
            reason="enumeration-failed",
        )
    if not inventory.required_oids:
        return _no_objects_required(inventory)

    required = inventory.required_oids
    if not enabled:
        return _partial(
            True,
            "Git LFS is in use, but --no-lfs intentionally skipped fetching the "
            f"{len(required)} required object(s).",
            reason="disabled",
            expected_object_count=len(required),
            attribute_files=inventory.attribute_files,
            tracking_declared=inventory.tracking_declared,
        )

    version = runner.lfs("version", cwd=mirror_path)
    if not version.succeeded:
        return _measure_after_incomplete_transfer(
            mirror_path,
            runner,
            inventory,
            reason="tool-unavailable",
            diagnostic="git-lfs is unavailable: " + _command_message(version),
            tooling_available=False,
        )

    storage = runner.git("config", "lfs.storage", "lfs", cwd=mirror_path)
    if not storage.succeeded:
        return _partial(
            True,
            "Could not configure archive-local LFS storage: "
            + _command_message(storage),
            reason="storage-configuration-failed",
            expected_object_count=len(required),
        )

    fetched = runner.lfs("fetch", "--all", cwd=mirror_path)
    if not fetched.succeeded:
        return _measure_after_incomplete_transfer(
            mirror_path,
            runner,
            inventory,
            reason="fetch-failed",
            diagnostic="Git LFS fetch failed: " + _command_message(fetched),
            tooling_available=True,
        )

    verified = verify_lfs_objects(
        mirror_path, runner, inventory=inventory, tooling_available=True
    )
    if verified.component.status == ComponentStatus.COMPLETE:
        return LfsArchiveResult(
            verified.manifest,
            ComponentResult(
                "lfs",
                ComponentStatus.COMPLETE,
                "Git LFS fetch and verification passed for "
                f"{len(required)} required object(s).",
            ),
        )
    return verified


def verify_lfs_archive(mirror_path: Path, runner: GitRunner) -> LfsArchiveResult:
    """Verify LFS completeness without fetching or changing archived refs.

    Returns the full result, not just its component, so callers can publish the
    exact current gap and refresh the persisted LFS record. Git LFS is probed
    only to report whether tooling is available to close a gap; its presence
    never affects the verdict.
    """
    inventory = enumerate_lfs_oids(mirror_path, runner, include_attributes=True)
    if isinstance(inventory, CommandResult):
        return _partial(
            None,
            "Git LFS requirements could not be determined: "
            + _command_message(inventory),
            reason="enumeration-failed",
        )
    tooling_available = None
    if inventory.required_oids:
        tooling_available = runner.lfs("version", cwd=mirror_path).succeeded
    return verify_lfs_objects(
        mirror_path, runner, inventory=inventory, tooling_available=tooling_available
    )


def verify_lfs_objects(
    mirror_path: Path,
    runner: GitRunner,
    *,
    inventory: LfsInventory | None = None,
    tooling_available: bool | None = None,
) -> LfsArchiveResult:
    """Check the archive-local store against the authoritative requirement set.

    Neither the requirement set nor the integrity check uses ``git-lfs``: the
    set comes from pointer blobs and each required object is confirmed by
    hashing its archived content, so missing tooling cannot hide a known gap.
    Availability is reported separately by :func:`verify_lfs_archive`.
    """
    if inventory is None:
        resolved = enumerate_lfs_oids(mirror_path, runner, include_attributes=True)
        if isinstance(resolved, CommandResult):
            return _partial(
                None,
                "Git LFS requirements could not be determined: "
                + _command_message(resolved),
                reason="enumeration-failed",
                tooling_available=tooling_available,
            )
        inventory = resolved

    required = inventory.required_oids
    if not required:
        return _no_objects_required(inventory, tooling_available=tooling_available)

    state = _local_object_state(mirror_path / "lfs" / "objects", required)
    if not state.intact:
        details = []
        if state.missing:
            details.append(f"{len(state.missing)} missing")
        if state.corrupt:
            details.append(f"{len(state.corrupt)} corrupt")
        return _partial(
            True,
            f"Git LFS object verification found {' and '.join(details)} of "
            f"{len(required)} required object(s).",
            reason="objects-incomplete",
            tooling_available=tooling_available,
            expected_object_count=len(required),
            missing_objects=state.missing,
            corrupt_objects=state.corrupt,
            attribute_files=inventory.attribute_files,
            tracking_declared=inventory.tracking_declared,
        )

    manifest: dict[str, object] = {
        "detected": True,
        "status": "complete",
        "expected_object_count": len(required),
        "missing_objects": [],
        "corrupt_objects": [],
        "attribute_files_inspected": inventory.attribute_files,
        "tracking_declared": inventory.tracking_declared,
    }
    if tooling_available is not None:
        manifest["tooling_available"] = tooling_available
    return LfsArchiveResult(
        manifest,
        ComponentResult(
            "lfs",
            ComponentStatus.COMPLETE,
            f"Git LFS object verification passed for {len(required)} "
            "required object(s).",
        ),
    )


def _measure_after_incomplete_transfer(
    mirror_path: Path,
    runner: GitRunner,
    inventory: LfsInventory,
    *,
    reason: str,
    diagnostic: str,
    tooling_available: bool,
) -> LfsArchiveResult:
    """Report the exact gap when a transfer could not run or did not complete.

    The requirement set is already known, so a failed or impossible fetch is
    still measured against the local store rather than reported as an unknown.
    """
    verified = verify_lfs_objects(
        mirror_path, runner, inventory=inventory, tooling_available=tooling_available
    )
    count = len(inventory.required_oids)
    manifest = dict(verified.manifest)
    manifest["diagnostic"] = diagnostic
    if verified.component.status == ComponentStatus.COMPLETE:
        return LfsArchiveResult(
            manifest,
            ComponentResult(
                "lfs",
                ComponentStatus.COMPLETE,
                f"All {count} required Git LFS object(s) are archived and verified, "
                f"although {diagnostic}",
            ),
        )
    manifest["reason"] = reason
    return LfsArchiveResult(
        manifest,
        ComponentResult(
            "lfs",
            ComponentStatus.PARTIAL,
            f"{verified.component.message} {summarize_lfs_gap(manifest)} {diagnostic}",
        ),
    )


def describe_lfs_tooling(manifest: dict[str, object]) -> str:
    """State whether Git LFS tooling was found, for emitted and persisted output.

    Empty when availability was not measured, which is the case when the history
    requires no objects and there is nothing a fetch could do.
    """
    available = manifest.get("tooling_available")
    if available is True:
        return "Git LFS tooling is available."
    if available is False:
        return "Git LFS tooling is unavailable on this machine."
    return ""


def summarize_lfs_gap(manifest: dict[str, object]) -> str:
    """Return a bounded human-readable list of the missing and corrupt OIDs.

    The manifest retains every OID; human output names only the first few so a
    large gap cannot flood a component message or report.
    """
    parts = []
    for label, key in (("Missing", "missing_objects"), ("Corrupt", "corrupt_objects")):
        oids = manifest.get(key)
        if not isinstance(oids, list) or not oids:
            continue
        shown = ", ".join(str(oid) for oid in oids[:_SUMMARIZED_OIDS])
        if len(oids) > _SUMMARIZED_OIDS:
            shown += f", and {len(oids) - _SUMMARIZED_OIDS} more"
        parts.append(f"{label}: {shown}.")
    return " ".join(parts)


def _no_objects_required(
    inventory: LfsInventory, *, tooling_available: bool | None = None
) -> LfsArchiveResult:
    """No reachable pointer blob names an object, so nothing can be missing."""
    manifest: dict[str, object] = {
        "detected": False,
        "status": "not-applicable",
        "expected_object_count": 0,
        "missing_objects": [],
        "corrupt_objects": [],
        "attribute_files_inspected": inventory.attribute_files,
        "tracking_declared": inventory.tracking_declared,
    }
    if tooling_available is not None:
        manifest["tooling_available"] = tooling_available
    if inventory.tracking_declared:
        message = (
            "Git LFS tracking is declared in .gitattributes, but no reachable "
            "pointer blob requires an object; nothing needs to be archived."
        )
    else:
        message = "No Git LFS usage detected."
    return LfsArchiveResult(
        manifest, ComponentResult("lfs", ComponentStatus.COMPLETE, message)
    )


def _local_object_state(
    objects_path: Path, required: tuple[str, ...]
) -> _LocalObjectState:
    missing: list[str] = []
    corrupt: list[str] = []
    for oid in required:
        object_path = objects_path / oid[:2] / oid[2:4] / oid
        if not object_path.is_file():
            missing.append(oid)
            continue
        try:
            actual_oid = sha256_file(object_path)
        except OSError:
            corrupt.append(oid)
        else:
            if actual_oid != oid:
                corrupt.append(oid)
    return _LocalObjectState(missing, corrupt)


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
    attribute_files: int | None = None,
    tracking_declared: bool | None = None,
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
    if attribute_files is not None:
        manifest["attribute_files_inspected"] = attribute_files
    if tracking_declared is not None:
        manifest["tracking_declared"] = tracking_declared
    return LfsArchiveResult(
        manifest,
        ComponentResult("lfs", ComponentStatus.PARTIAL, message),
    )


def _command_message(command: CommandResult) -> str:
    return (
        command.stderr.strip()
        or command.stdout.strip()
        or f"command failed with exit code {command.returncode}"
    )
