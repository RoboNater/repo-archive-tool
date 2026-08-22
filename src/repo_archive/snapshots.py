"""Self-contained, immutable Git bundle snapshots."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from repo_archive.archive import ArchiveLayout
from repo_archive.filesystem import safe_sha256_file, sha256_file, try_reflink
from repo_archive.git import CommandResult, GitRunner
from repo_archive.manifest import Manifest, load_manifest, write_json_atomic
from repo_archive.reporting import add_report_write_warning, write_latest_reports
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
)

SNAPSHOT_SCHEMA_VERSION = 1
_MAX_POINTER_SIZE = 1024
_OID_LINE = re.compile(r"^oid sha256:([0-9a-fA-F]{64})$")
_SIZE_LINE = re.compile(r"^size ([0-9]+)$")
_EXTENSION_LINE = re.compile(r"^ext-[0-9]+-[A-Za-z0-9][A-Za-z0-9.-]* .+$")


@dataclass(frozen=True)
class LfsInventory:
    """Historical LFS objects required by all archived refs."""

    required_oids: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotVerification:
    """Verification state for one snapshot subtree."""

    outcome: str
    message: str
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome in {"verified-complete", "verified-partial"}


class SnapshotError(RuntimeError):
    """A snapshot could not be safely created or verified."""

    def __init__(self, message: str, kind: ErrorKind = ErrorKind.VERIFICATION) -> None:
        self.kind = kind
        super().__init__(message)


def create_snapshot(
    layout: ArchiveLayout,
    *,
    runner: GitRunner | None = None,
    created_at: datetime | None = None,
    record_result: bool = True,
) -> OperationResult:
    """Create and atomically publish a self-contained snapshot of *layout*."""
    runner = runner or GitRunner()
    try:
        manifest = load_manifest(layout.manifest_path)
    except (FileNotFoundError, ValueError) as error:
        return _record(
            layout,
            _failure(
                "snapshot", layout, "manifest", str(error), ErrorKind.CONFIGURATION
            ),
            record_result,
        )

    has_refs = _has_snapshot_refs(layout.mirror_path, runner)
    if isinstance(has_refs, CommandResult):
        return _record(
            layout,
            _failure(
                "snapshot",
                layout,
                "snapshot refs",
                _command_message(has_refs),
                ErrorKind.GENERAL,
            ),
            record_result,
        )
    if not has_refs:
        return _record(
            layout,
            OperationResult(
                "snapshot",
                layout.path,
                components=(
                    ComponentResult(
                        "snapshot",
                        ComponentStatus.WARNING,
                        "No refs exist to snapshot; no bundle was created.",
                    ),
                ),
            ),
            record_result,
        )

    staging_path: Path | None = None
    try:
        layout.snapshots_path.mkdir(parents=True, exist_ok=True)
        timestamp, snapshot_name = _unique_snapshot_identity(
            layout.snapshots_path, created_at
        )
        final_path = layout.snapshots_path / snapshot_name
        staging_path = Path(
            tempfile.mkdtemp(prefix=f".{snapshot_name}.tmp-", dir=layout.snapshots_path)
        )
        bundle_path = staging_path / "snapshot.bundle"
        bundled = runner.git(
            "bundle", "create", str(bundle_path), "--all", cwd=layout.mirror_path
        )
        if not bundled.succeeded:
            raise SnapshotError(
                "Git bundle creation failed: " + _command_message(bundled),
                ErrorKind.GENERAL,
            )

        refs = _bundle_refs(bundle_path, runner)
        if isinstance(refs, CommandResult):
            raise SnapshotError("Could not list bundle refs: " + _command_message(refs))

        inventory = enumerate_lfs_oids(layout.mirror_path, runner)
        if isinstance(inventory, CommandResult):
            raise SnapshotError(
                "Historical Git LFS pointer enumeration failed: "
                + _command_message(inventory)
            )

        _preflight_snapshot_space(
            staging_path, bundle_path, layout.mirror_path, inventory.required_oids
        )
        lfs_record = _materialize_lfs_payload(
            layout.mirror_path, staging_path, inventory.required_oids
        )
        status = "partial" if lfs_record["unavailable_oids"] else "complete"
        verification_outcome = f"verified-{status}"
        record = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "created_at": timestamp,
            "status": status,
            "verification": verification_outcome,
            "bundle": {
                "path": "snapshot.bundle",
                "sha256": sha256_file(bundle_path),
                "size_bytes": bundle_path.stat().st_size,
            },
            "refs": refs,
            "lfs": lfs_record,
        }
        write_json_atomic(staging_path / "snapshot.json", record)

        verified = verify_snapshot_path(staging_path, runner=runner, deep=True)
        if not verified.succeeded:
            raise SnapshotError(verified.message)

        staging_path.rename(final_path)
        relative_path = final_path.relative_to(layout.path).as_posix()
        index_component = _update_snapshot_index(
            layout, manifest, published_path=relative_path
        )
    except (OSError, SnapshotError, ValueError) as error:
        message = _snapshot_error_message(error)
        return _record(
            layout,
            _failure(
                "snapshot",
                layout,
                "snapshot",
                message,
                _snapshot_error_kind(error),
            ),
            record_result,
        )
    finally:
        if staging_path is not None and staging_path.exists():
            shutil.rmtree(staging_path, ignore_errors=True)

    unavailable = lfs_record["unavailable_oids"]
    lfs_status = ComponentStatus.PARTIAL if unavailable else ComponentStatus.COMPLETE
    result = OperationResult(
        "snapshot",
        layout.path,
        components=(
            ComponentResult(
                "bundle",
                ComponentStatus.COMPLETE,
                f"Created {relative_path}/snapshot.bundle with {len(refs)} refs.",
            ),
            ComponentResult(
                "snapshot lfs",
                lfs_status,
                (
                    "Ordinary Git bundles do not contain LFS objects; "
                    f"the snapshot owns {lfs_record['present_object_count']} verified "
                    "LFS payload(s)."
                    if not unavailable
                    else "Ordinary Git bundles do not contain LFS objects; the "
                    f"snapshot is partial with {len(unavailable)} unavailable OID(s)."
                ),
            ),
            ComponentResult(
                "snapshot verification",
                ComponentStatus.COMPLETE,
                f"Snapshot publication passed as {verification_outcome}.",
            ),
            index_component,
        ),
    )
    return _record(layout, result, record_result)


def enumerate_lfs_oids(
    repository: Path, runner: GitRunner
) -> LfsInventory | CommandResult:
    """Find valid LFS pointer blobs reachable from every ref without git-lfs."""
    required: set[str] = set()
    with (
        tempfile.TemporaryFile(mode="w+b") as object_ids,
        tempfile.TemporaryFile(mode="w+b") as candidates,
    ):

        def collect_object(line: str) -> None:
            oid = line.split(" ", 1)[0]
            if oid:
                object_ids.write(oid.encode("ascii") + b"\n")

        listed = runner.git_stream_stdout(
            "rev-list", "--objects", "--all", cwd=repository, on_line=collect_object
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
            pointer_oid = _parse_lfs_pointer(pointer)
            if pointer_oid is not None:
                required.add(pointer_oid)

        inspected = runner.git_batch_blobs(
            cwd=repository, object_ids=candidates, on_blob=inspect_blob
        )
        if not inspected.succeeded:
            return inspected
    return LfsInventory(tuple(sorted(required)))


def verify_snapshot_path(
    snapshot_path: Path,
    *,
    runner: GitRunner | None = None,
    deep: bool = False,
) -> SnapshotVerification:
    """Verify a published or staged snapshot subtree against its record."""
    runner = runner or GitRunner()
    warnings: tuple[str, ...] = ()
    try:
        warnings = _verify_snapshot_root(snapshot_path)
        record = _load_snapshot_record(snapshot_path / "snapshot.json")
        bundle_path = _record_path(snapshot_path, record["bundle"]["path"])
        if not bundle_path.is_file():
            raise SnapshotError("Recorded bundle is missing.")
        if sha256_file(bundle_path) != record["bundle"]["sha256"]:
            raise SnapshotError("Bundle digest does not match snapshot.json.")
        if bundle_path.stat().st_size != record["bundle"]["size_bytes"]:
            raise SnapshotError("Bundle size does not match snapshot.json.")

        with tempfile.TemporaryDirectory(
            prefix=".snapshot-verify-", dir=snapshot_path.parent
        ) as temporary:
            verification_repo = Path(temporary) / "verify.git"
            initialized = runner.git("init", "--bare", str(verification_repo))
            if not initialized.succeeded:
                raise SnapshotError(_command_message(initialized))
            verified = runner.git(
                "bundle", "verify", str(bundle_path), cwd=verification_repo
            )
            if not verified.succeeded:
                raise SnapshotError(
                    "Git bundle verification failed: " + _command_message(verified)
                )

            actual_refs = _bundle_refs(bundle_path, runner)
            if isinstance(actual_refs, CommandResult):
                raise SnapshotError(_command_message(actual_refs))
            if actual_refs != record["refs"]:
                raise SnapshotError("Bundle refs do not match snapshot.json.")

            _verify_lfs_record(snapshot_path, record["lfs"])
            if record["status"] != record["lfs"]["status"]:
                raise SnapshotError("Snapshot and LFS statuses are inconsistent.")

            if deep:
                recovered = Path(temporary) / "deep.git"
                cloned = runner.git(
                    "clone", "--mirror", str(bundle_path), str(recovered)
                )
                if not cloned.succeeded:
                    raise SnapshotError(
                        "Deep bundle materialization failed: "
                        + _command_message(cloned)
                    )
                inventory = enumerate_lfs_oids(recovered, runner)
                if isinstance(inventory, CommandResult):
                    raise SnapshotError(
                        "Deep LFS enumeration failed: " + _command_message(inventory)
                    )
                if list(inventory.required_oids) != record["lfs"]["required_oids"]:
                    raise SnapshotError(
                        "Recorded LFS requirements do not match bundled history."
                    )
    except (KeyError, OSError, TypeError, ValueError, SnapshotError) as error:
        return SnapshotVerification("failed", str(error))

    outcome = str(record["verification"])
    message = f"Snapshot passed as {outcome}."
    if warnings:
        message += " " + " ".join(warnings)
    return SnapshotVerification(outcome, message, warnings)


def discover_snapshot_paths(layout: ArchiveLayout) -> list[Path]:
    """Return timestamped snapshot subtrees containing a snapshot record."""
    if not layout.snapshots_path.is_dir():
        return []
    return sorted(path.parent for path in layout.snapshots_path.glob("*/snapshot.json"))


def load_snapshot_record(snapshot_path: Path) -> dict[str, Any]:
    """Load and validate a snapshot record for restore and inspection."""
    return _load_snapshot_record(snapshot_path / "snapshot.json")


def _has_snapshot_refs(mirror_path: Path, runner: GitRunner) -> bool | CommandResult:
    refs = runner.git("for-each-ref", "--format=%(refname)", cwd=mirror_path)
    if not refs.succeeded:
        return refs
    if refs.stdout.strip():
        return True
    head = runner.git("rev-parse", "--verify", "HEAD", cwd=mirror_path)
    if head.succeeded:
        return True
    if head.returncode in {1, 128}:
        return False
    return head


def _preflight_snapshot_space(
    staging_path: Path,
    bundle_path: Path,
    mirror_path: Path,
    required_oids: tuple[str, ...],
) -> None:
    available: list[tuple[Path, int]] = []
    for oid in required_oids:
        source = mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        if not source.is_file():
            continue
        try:
            available.append((source, source.stat().st_size))
        except OSError as error:
            raise SnapshotError(
                f"Could not measure archived LFS object {oid}: {error}"
            ) from error
    payload_bytes = sum(size for _, size in available)
    if available and _probe_hardlink(available[0][0], staging_path):
        payload_bytes = 0
    try:
        free_bytes = shutil.disk_usage(staging_path).free
    except OSError:
        return
    required_bytes = bundle_path.stat().st_size + payload_bytes
    if free_bytes < required_bytes:
        raise SnapshotError(
            "Insufficient free space for snapshot staging: "
            f"approximately {required_bytes} additional bytes required, "
            f"{free_bytes} available.",
            ErrorKind.GENERAL,
        )


def _probe_hardlink(source: Path, staging_path: Path) -> bool:
    probe = staging_path / ".lfs-hardlink-probe"
    try:
        os.link(source, probe)
    except OSError:
        return False
    else:
        return True
    finally:
        probe.unlink(missing_ok=True)


def _materialize_lfs_payload(
    mirror_path: Path, snapshot_path: Path, required_oids: tuple[str, ...]
) -> dict[str, object]:
    present: list[str] = []
    unavailable: list[str] = []
    logical_bytes = 0
    methods = {
        name: {"object_count": 0, "logical_bytes": 0}
        for name in ("reflink", "hardlink", "copy")
    }
    reflink_supported = True
    for oid in required_oids:
        source = mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        if not source.is_file():
            unavailable.append(oid)
            continue
        if safe_sha256_file(source) != oid:
            raise SnapshotError(f"Archived LFS object is unreadable or corrupt: {oid}")
        destination = snapshot_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        destination.parent.mkdir(parents=True, exist_ok=True)
        method, reflink_supported = _link_or_copy(
            source, destination, try_clone=reflink_supported
        )
        size = source.stat().st_size
        present.append(oid)
        logical_bytes += size
        methods[method]["object_count"] += 1
        methods[method]["logical_bytes"] += size

    return {
        "status": "partial" if unavailable else "complete",
        "relationship": "snapshot-owned",
        "objects_path": "lfs/objects",
        "required_oids": list(required_oids),
        "present_oids": present,
        "unavailable_oids": unavailable,
        "required_object_count": len(required_oids),
        "present_object_count": len(present),
        "unavailable_object_count": len(unavailable),
        "total_logical_bytes": logical_bytes,
        "materialization": methods,
    }


def _verify_lfs_record(snapshot_path: Path, lfs: dict[str, Any]) -> None:
    if lfs["objects_path"] != "lfs/objects":
        raise SnapshotError("Snapshot LFS object path is invalid.")
    required = set(lfs["required_oids"])
    present = set(lfs["present_oids"])
    unavailable = set(lfs["unavailable_oids"])
    if required != present | unavailable or present & unavailable:
        raise SnapshotError(
            "LFS required, present, and unavailable sets are inconsistent."
        )
    if lfs["required_object_count"] != len(required):
        raise SnapshotError("Recorded required LFS object count is incorrect.")
    if lfs["present_object_count"] != len(present):
        raise SnapshotError("Recorded present LFS object count is incorrect.")
    if lfs["unavailable_object_count"] != len(unavailable):
        raise SnapshotError("Recorded unavailable LFS object count is incorrect.")

    objects_root = snapshot_path / "lfs" / "objects"
    actual: set[str] = set()
    if objects_root.is_dir():
        for path in objects_root.rglob("*"):
            if path.is_file():
                actual.add(path.relative_to(objects_root).as_posix().lower())
    expected_paths = {f"{oid[:2]}/{oid[2:4]}/{oid}" for oid in present}
    if actual != expected_paths:
        raise SnapshotError(
            "Snapshot LFS subtree does not match its recorded inventory."
        )

    logical_bytes = 0
    for oid in sorted(present):
        path = objects_root / oid[:2] / oid[2:4] / oid
        if safe_sha256_file(path) != oid:
            raise SnapshotError(f"Snapshot LFS object is corrupt: {oid}")
        logical_bytes += path.stat().st_size
    if lfs["total_logical_bytes"] != logical_bytes:
        raise SnapshotError("Recorded LFS logical byte total is incorrect.")
    materialization = lfs["materialization"]
    if not isinstance(materialization, dict):
        raise SnapshotError("Snapshot LFS materialization totals are invalid.")
    method_objects = 0
    method_bytes = 0
    for name in ("reflink", "hardlink", "copy"):
        totals = materialization.get(name)
        if not isinstance(totals, dict):
            raise SnapshotError("Snapshot LFS materialization totals are invalid.")
        method_objects += int(totals["object_count"])
        method_bytes += int(totals["logical_bytes"])
    if method_objects != len(present) or method_bytes != logical_bytes:
        raise SnapshotError("Snapshot LFS materialization totals are inconsistent.")

    status = "partial" if unavailable else "complete"
    if status != lfs.get("status", status):
        raise SnapshotError("Snapshot LFS status is inconsistent.")


def _verify_snapshot_root(snapshot_path: Path) -> tuple[str, ...]:
    allowed = {"snapshot.bundle", "snapshot.json", "lfs"}
    unexpected = sorted(
        path.name for path in snapshot_path.iterdir() if path.name not in allowed
    )
    lfs_path = snapshot_path / "lfs"
    if lfs_path.exists() and not lfs_path.is_dir():
        raise SnapshotError("Snapshot root lfs entry is not a directory.")
    if unexpected:
        return (
            "Snapshot root contains unexpected entries outside the recorded "
            "recovery unit: " + ", ".join(unexpected) + ".",
        )
    return ()


def _load_snapshot_record(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
    ):
        raise ValueError("Unsupported snapshot schema version.")
    required = {"created_at", "status", "verification", "bundle", "refs", "lfs"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError("Snapshot record is missing: " + ", ".join(missing))
    expected_verification = f"verified-{value['status']}"
    if (
        value["status"] not in {"complete", "partial"}
        or value["verification"] != expected_verification
    ):
        raise ValueError("Snapshot status and verification outcome are inconsistent.")
    if (
        not isinstance(value["bundle"], dict)
        or not isinstance(value["refs"], list)
        or not isinstance(value["lfs"], dict)
    ):
        raise ValueError("Snapshot record has invalid component shapes.")
    return value


def _record_path(snapshot_path: Path, relative: object) -> Path:
    if not isinstance(relative, str):
        raise ValueError("Snapshot path must be a string.")
    candidate = (snapshot_path / relative).resolve()
    try:
        candidate.relative_to(snapshot_path.resolve())
    except ValueError as error:
        raise ValueError("Snapshot record contains an unsafe path.") from error
    return candidate


def _bundle_refs(
    bundle_path: Path, runner: GitRunner
) -> list[dict[str, str]] | CommandResult:
    listed = runner.git("bundle", "list-heads", str(bundle_path))
    if not listed.succeeded:
        return listed
    refs = []
    for line in listed.stdout.splitlines():
        oid, separator, name = line.partition(" ")
        if separator and oid and name:
            refs.append({"name": name.strip(), "oid": oid.lower()})
    return sorted(refs, key=lambda item: item["name"])


def _parse_lfs_pointer(content: str) -> str | None:
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


def _link_or_copy(
    source: Path, destination: Path, *, try_clone: bool
) -> tuple[str, bool]:
    if try_clone and try_reflink(source, destination):
        return "reflink", True
    try:
        os.link(source, destination)
    except OSError:
        try:
            shutil.copy2(source, destination)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return "copy", False
    return "hardlink", False


def _unique_snapshot_identity(
    snapshots_path: Path, created_at: datetime | None
) -> tuple[str, str]:
    current = (created_at or datetime.now(UTC)).astimezone(UTC)
    for offset in range(1000):
        candidate = current + timedelta(microseconds=offset)
        timestamp = candidate.isoformat(timespec="microseconds").replace("+00:00", "Z")
        name = candidate.strftime("%Y-%m-%dT%H%M%S.%fZ")
        if not (snapshots_path / name).exists():
            return timestamp, name
    raise OSError("Could not allocate a unique UTC snapshot timestamp.")


def _update_snapshot_index(
    layout: ArchiveLayout, manifest: Manifest, *, published_path: str
) -> ComponentResult:
    try:
        entries, skipped = _rebuild_snapshot_index(layout)
        archive = dict(manifest.archive)
        archive["snapshots"] = entries
        write_json_atomic(
            layout.manifest_path, replace(manifest, archive=archive).to_dict()
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        return ComponentResult(
            "manifest",
            ComponentStatus.WARNING,
            f"Snapshot was published at {published_path}, but its manifest index "
            f"could not be updated: {error}",
        )
    if skipped:
        return ComponentResult(
            "manifest",
            ComponentStatus.WARNING,
            "Snapshot index updated after publishing "
            f"{published_path}, but existing snapshot records were skipped: "
            + "; ".join(skipped),
        )
    return ComponentResult(
        "manifest", ComponentStatus.COMPLETE, "Snapshot index updated."
    )


def _rebuild_snapshot_index(
    layout: ArchiveLayout,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    entries: list[dict[str, object]] = []
    skipped: list[str] = []
    for snapshot_path in discover_snapshot_paths(layout):
        relative_path = snapshot_path.relative_to(layout.path).as_posix()
        try:
            record = load_snapshot_record(snapshot_path)
            bundle = record["bundle"]
            lfs = record["lfs"]
            entries.append(
                {
                    "path": relative_path,
                    "created_at": record["created_at"],
                    "status": record["status"],
                    "verification": record["verification"],
                    "bundle_sha256": bundle["sha256"],
                    "ref_count": len(record["refs"]),
                    "required_lfs_object_count": lfs["required_object_count"],
                    "unavailable_lfs_oids": lfs["unavailable_oids"],
                }
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            skipped.append(f"{relative_path} ({error})")
    return entries, tuple(skipped)


def _command_message(command: CommandResult) -> str:
    return (
        command.stderr.strip()
        or command.stdout.strip()
        or f"Git command failed with exit code {command.returncode}."
    )


def _snapshot_error_message(error: OSError | SnapshotError | ValueError) -> str:
    if isinstance(error, OSError) and (
        error.errno == errno.ENOSPC or getattr(error, "winerror", None) == 112
    ):
        return "Snapshot staging ran out of disk space; no snapshot was published."
    return str(error)


def _snapshot_error_kind(
    error: OSError | SnapshotError | ValueError,
) -> ErrorKind:
    if isinstance(error, SnapshotError):
        return error.kind
    if isinstance(error, PermissionError):
        return ErrorKind.CONFIGURATION
    return ErrorKind.GENERAL


def _failure(
    operation: str,
    layout: ArchiveLayout,
    name: str,
    message: str,
    kind: ErrorKind,
) -> OperationResult:
    return OperationResult(
        operation,
        layout.path,
        components=(ComponentResult(name, ComponentStatus.FAILED, message, kind),),
        errors=(message,),
    )


def _record(
    layout: ArchiveLayout, result: OperationResult, enabled: bool
) -> OperationResult:
    if enabled:
        try:
            write_latest_reports(layout.reports_path, result)
        except OSError as error:
            return add_report_write_warning(result, error)
    return result
