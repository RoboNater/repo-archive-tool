"""Offline staged restoration from archive mirrors and snapshots."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from repo_archive.archive import ArchiveLayout
from repo_archive.filesystem import (
    copy_independent,
    remove_readonly,
    safe_sha256_file,
    sha256_file,
)
from repo_archive.git import CommandResult, GitRunner
from repo_archive.manifest import load_manifest
from repo_archive.reporting import write_latest_reports
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
)
from repo_archive.snapshots import (
    enumerate_lfs_oids,
    load_snapshot_record,
    verify_snapshot_path,
)

_SNAPSHOT_ID = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}\.[0-9]{6}Z$")


@dataclass(frozen=True)
class RestoreSource:
    """Local Git and LFS inputs for one restore operation."""

    git_path: Path
    lfs_objects_path: Path
    required_oids: tuple[str, ...]
    present_oids: tuple[str, ...]
    unavailable_oids: tuple[str, ...]
    description: str


class RestoreError(RuntimeError):
    """A restore could not be staged and validated safely."""

    def __init__(self, message: str, kind: ErrorKind = ErrorKind.GENERAL) -> None:
        self.kind = kind
        super().__init__(message)


def restore_archive(
    layout: ArchiveLayout,
    destination: Path,
    *,
    snapshot: str | None = None,
    recovered_mirror: bool = False,
    runner: GitRunner | None = None,
) -> OperationResult:
    """Restore a working clone or recovered mirror using local archive data only."""
    runner = runner or GitRunner()
    try:
        if snapshot is None:
            load_manifest(layout.manifest_path)
        final_path = _validate_destination(destination)
        source = _select_source(layout, snapshot, runner)
        final_path.parent.mkdir(parents=True, exist_ok=True)
    except (FileNotFoundError, OSError, RestoreError, ValueError) as error:
        kind = (
            error.kind if isinstance(error, RestoreError) else ErrorKind.CONFIGURATION
        )
        return _record(layout, _failure(layout, str(error), kind))

    staging_path = final_path.parent / (f".{final_path.name}.restore-{uuid4().hex}")
    lfs_checkout_message: str | None = None
    lfs_checkout_partial = False
    try:
        clone_arguments = ["clone"]
        clone_arguments.append("--mirror" if recovered_mirror else "--no-checkout")
        clone_arguments.extend((str(source.git_path), str(staging_path)))
        cloned = runner.git(
            *clone_arguments,
            environment={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if not cloned.succeeded:
            raise RestoreError("Offline Git clone failed: " + _command_message(cloned))

        destination_objects = (
            staging_path / "lfs" / "objects"
            if recovered_mirror
            else staging_path / ".git" / "lfs" / "objects"
        )
        _seed_lfs_objects(source, destination_objects)

        if recovered_mirror:
            _validate_recovered_mirror(staging_path, runner)
        else:
            lfs_checkout_message, lfs_checkout_partial = _checkout_worktree(
                staging_path, source, runner
            )
            _validate_working_clone(staging_path, runner)

        integrity = runner.git("fsck", "--full", cwd=staging_path)
        if not integrity.succeeded:
            raise RestoreError(
                "Restored Git object verification failed: "
                + _command_message(integrity),
                ErrorKind.VERIFICATION,
            )
        if final_path.exists():
            raise RestoreError(
                "Destination appeared during restore and was not overwritten.",
                ErrorKind.CONFIGURATION,
            )
        staging_path.rename(final_path)
    except (OSError, RestoreError) as error:
        kind = error.kind if isinstance(error, RestoreError) else ErrorKind.GENERAL
        return _record(layout, _failure(layout, str(error), kind))
    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path, onerror=remove_readonly)

    lfs_partial = bool(source.unavailable_oids) or lfs_checkout_partial
    if source.unavailable_oids:
        lfs_message = (
            f"Seeded {len(source.present_oids)} verified LFS object(s); unavailable "
            f"OIDs: {', '.join(source.unavailable_oids)}."
        )
    elif lfs_checkout_message:
        lfs_message = lfs_checkout_message
    else:
        lfs_message = (
            f"Seeded {len(source.present_oids)} verified LFS object(s); no LFS gap."
        )

    mode = "recovered mirror" if recovered_mirror else "working clone"
    result = OperationResult(
        "restore",
        layout.path,
        components=(
            ComponentResult(
                "restore source", ComponentStatus.COMPLETE, source.description
            ),
            ComponentResult(
                "git restore",
                ComponentStatus.COMPLETE,
                f"Published offline {mode} at {final_path}.",
            ),
            ComponentResult(
                "restore lfs",
                ComponentStatus.PARTIAL if lfs_partial else ComponentStatus.COMPLETE,
                lfs_message,
            ),
            ComponentResult(
                "restore validation",
                ComponentStatus.COMPLETE,
                "Restored Git object validation passed.",
            ),
        ),
    )
    return _record(layout, result)


def _select_source(
    layout: ArchiveLayout, snapshot: str | None, runner: GitRunner
) -> RestoreSource:
    if snapshot is not None:
        if not _SNAPSHOT_ID.fullmatch(snapshot):
            raise RestoreError(
                "Snapshot must be a UTC timestamp directory name from this archive.",
                ErrorKind.CONFIGURATION,
            )
        snapshot_path = layout.snapshots_path / snapshot
        if (
            not snapshot_path.is_dir()
            or not (snapshot_path / "snapshot.json").is_file()
        ):
            raise RestoreError(
                f"No such snapshot exists in this archive: {snapshot}",
                ErrorKind.CONFIGURATION,
            )
        verified = verify_snapshot_path(snapshot_path, runner=runner)
        if not verified.succeeded:
            raise RestoreError(
                "Selected snapshot failed verification: " + verified.message,
                ErrorKind.VERIFICATION,
            )
        record = load_snapshot_record(snapshot_path)
        lfs = record["lfs"]
        return RestoreSource(
            git_path=snapshot_path / str(record["bundle"]["path"]),
            lfs_objects_path=snapshot_path / str(lfs["objects_path"]),
            required_oids=tuple(lfs["required_oids"]),
            present_oids=tuple(lfs["present_oids"]),
            unavailable_oids=tuple(lfs["unavailable_oids"]),
            description=f"Verified {record['status']} snapshot {snapshot}.",
        )

    bare = runner.git("rev-parse", "--is-bare-repository", cwd=layout.mirror_path)
    if not bare.succeeded or bare.stdout.strip().lower() != "true":
        raise RestoreError(
            "Archive mirror is missing or is not a bare Git repository.",
            ErrorKind.CONFIGURATION,
        )
    integrity = runner.git("fsck", "--full", cwd=layout.mirror_path)
    if not integrity.succeeded:
        raise RestoreError(
            "Archive mirror failed Git fsck: " + _command_message(integrity),
            ErrorKind.VERIFICATION,
        )
    inventory = enumerate_lfs_oids(layout.mirror_path, runner)
    if isinstance(inventory, CommandResult):
        raise RestoreError(
            "Could not enumerate historical LFS requirements: "
            + _command_message(inventory),
            ErrorKind.VERIFICATION,
        )
    objects_path = layout.mirror_path / "lfs" / "objects"
    present, unavailable = _classify_lfs_objects(objects_path, inventory.required_oids)
    return RestoreSource(
        git_path=layout.mirror_path,
        lfs_objects_path=objects_path,
        required_oids=inventory.required_oids,
        present_oids=present,
        unavailable_oids=unavailable,
        description="Validated current archive mirror.",
    )


def _validate_destination(destination: Path) -> Path:
    final_path = destination.resolve()
    if final_path.exists():
        raise RestoreError(
            f"Destination already exists and will not be overwritten: {final_path}",
            ErrorKind.CONFIGURATION,
        )
    if not final_path.name or final_path == final_path.parent:
        raise RestoreError("Destination path is unsafe.", ErrorKind.CONFIGURATION)
    if final_path.parent.exists() and not final_path.parent.is_dir():
        raise RestoreError(
            "Destination parent is not a directory.", ErrorKind.CONFIGURATION
        )
    return final_path


def _classify_lfs_objects(
    objects_path: Path, required_oids: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    present: list[str] = []
    unavailable: list[str] = []
    for oid in required_oids:
        path = objects_path / oid[:2] / oid[2:4] / oid
        if path.is_file() and safe_sha256_file(path) == oid:
            present.append(oid)
        else:
            unavailable.append(oid)
    return tuple(present), tuple(unavailable)


def _seed_lfs_objects(source: RestoreSource, destination_root: Path) -> None:
    reflink_supported = True
    for oid in source.present_oids:
        source_path = source.lfs_objects_path / oid[:2] / oid[2:4] / oid
        if sha256_file(source_path) != oid:
            raise RestoreError(f"LFS payload changed during restore: {oid}")
        destination = destination_root / oid[:2] / oid[2:4] / oid
        destination.parent.mkdir(parents=True, exist_ok=True)
        _, reflink_supported = copy_independent(
            source_path, destination, try_clone=reflink_supported
        )


def _checkout_worktree(
    staging_path: Path, source: RestoreSource, runner: GitRunner
) -> tuple[str | None, bool]:
    head = runner.git("rev-parse", "--verify", "HEAD", cwd=staging_path)
    if head.succeeded:
        checked_out = runner.git(
            "reset",
            "--hard",
            "HEAD",
            cwd=staging_path,
            environment={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if not checked_out.succeeded:
            raise RestoreError(
                "Working-tree checkout failed: " + _command_message(checked_out)
            )
    elif head.returncode not in {1, 128}:
        raise RestoreError("Could not resolve restored HEAD: " + _command_message(head))

    if not source.required_oids:
        return None, False
    version = runner.lfs("version", cwd=staging_path)
    if not version.succeeded:
        return (
            "LFS payloads were seeded, but git-lfs is unavailable; the working "
            "tree retains pointer files.",
            True,
        )
    checked_out_lfs = runner.lfs("checkout", cwd=staging_path)
    if checked_out_lfs.succeeded:
        return (
            f"Seeded {len(source.present_oids)} verified LFS object(s) and ran "
            "the local Git LFS checkout.",
            False,
        )
    if source.unavailable_oids:
        return (
            "Git LFS checkout was incomplete because declared payloads are "
            "unavailable; pointer files were retained where content was missing.",
            True,
        )
    raise RestoreError(
        "Local Git LFS checkout failed: " + _command_message(checked_out_lfs)
    )


def _validate_working_clone(path: Path, runner: GitRunner) -> None:
    checked = runner.git("rev-parse", "--is-inside-work-tree", cwd=path)
    if not checked.succeeded or checked.stdout.strip().lower() != "true":
        raise RestoreError(
            "Staged restore is not a normal working repository.",
            ErrorKind.VERIFICATION,
        )


def _validate_recovered_mirror(path: Path, runner: GitRunner) -> None:
    bare = runner.git("rev-parse", "--is-bare-repository", cwd=path)
    mirrored = runner.git("config", "--bool", "remote.origin.mirror", cwd=path)
    if (
        not bare.succeeded
        or bare.stdout.strip().lower() != "true"
        or not mirrored.succeeded
        or mirrored.stdout.strip().lower() != "true"
    ):
        raise RestoreError(
            "Staged restore is not a recovered mirror suitable for push --mirror.",
            ErrorKind.VERIFICATION,
        )


def _command_message(command: CommandResult) -> str:
    return (
        command.stderr.strip()
        or command.stdout.strip()
        or f"Git command failed with exit code {command.returncode}."
    )


def _failure(layout: ArchiveLayout, message: str, kind: ErrorKind) -> OperationResult:
    return OperationResult(
        "restore",
        layout.path,
        components=(ComponentResult("restore", ComponentStatus.FAILED, message, kind),),
        errors=(message,),
    )


def _record(layout: ArchiveLayout, result: OperationResult) -> OperationResult:
    try:
        write_latest_reports(layout.reports_path, result)
    except OSError as error:
        return OperationResult(
            operation=result.operation,
            archive_path=result.archive_path,
            components=result.components,
            warnings=result.warnings
            + (f"Latest restore reports could not be written: {error}",),
            errors=result.errors,
        )
    return result
