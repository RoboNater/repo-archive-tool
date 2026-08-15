"""Archive-set layout management and core mirror backup operations."""

from __future__ import annotations

import shutil
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from repo_archive.git import CommandResult, GitRunner
from repo_archive.manifest import Manifest, load_manifest, write_json_atomic
from repo_archive.remote import Remote, display_remote, normalize_remote
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
)


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


def backup_archive(
    remote_url: str,
    layout: ArchiveLayout,
    *,
    runner: GitRunner | None = None,
) -> OperationResult:
    """Create or safely refresh *layout* from *remote_url*.

    Existing mirrors are never fetched in place.  A local mirror clone is
    refreshed and validated in a sibling staging directory before it replaces
    the current mirror.
    """
    remote = normalize_remote(remote_url)
    return _create_or_update(layout, remote, runner or GitRunner(), "backup")


def update_archive(
    layout: ArchiveLayout, *, runner: GitRunner | None = None
) -> OperationResult:
    """Safely refresh an archive using the source remote configured in its mirror."""
    runner = runner or GitRunner()
    source = runner.git("remote", "get-url", "origin", cwd=layout.mirror_path)
    if not source.succeeded:
        return _failure_result("update", layout, "source remote", source)
    try:
        remote = normalize_remote(source.stdout.strip())
    except ValueError as error:
        return OperationResult(
            operation="update",
            archive_path=layout.path,
            components=(
                ComponentResult(
                    "source remote",
                    ComponentStatus.FAILED,
                    str(error),
                    ErrorKind.CONFIGURATION,
                ),
            ),
            errors=(str(error),),
        )
    return _create_or_update(layout, remote, runner, "update")


def _create_or_update(
    layout: ArchiveLayout,
    remote: Remote,
    runner: GitRunner,
    operation: str,
) -> OperationResult:
    layout.ensure_directories()
    existing = layout.mirror_path.exists()
    staging_root = Path(tempfile.mkdtemp(prefix=".mirror-staging-", dir=layout.path))
    staged_mirror = staging_root / "mirror.git"
    try:
        if existing:
            cloned = runner.git(
                "clone", "--mirror", str(layout.mirror_path), str(staged_mirror)
            )
            action = "Mirror staged from the previous valid archive."
        else:
            cloned = runner.git(
                "clone", "--mirror", remote.original, str(staged_mirror)
            )
            action = "Git mirror cloned."
        if not cloned.succeeded:
            return _failure_result(operation, layout, "git mirror", cloned)

        set_remote = runner.git(
            "remote", "set-url", "origin", remote.original, cwd=staged_mirror
        )
        if not set_remote.succeeded:
            return _failure_result(operation, layout, "source remote", set_remote)

        refreshed = runner.git("remote", "update", "--prune", cwd=staged_mirror)
        if not refreshed.succeeded:
            return _failure_result(operation, layout, "git mirror", refreshed)

        validated = runner.git("fsck", "--full", cwd=staged_mirror)
        if not validated.succeeded:
            return _failure_result(
                operation, layout, "git integrity", validated, ErrorKind.VERIFICATION
            )

        state = _read_mirror_state(staged_mirror, runner)
        if isinstance(state, CommandResult):
            return _failure_result(operation, layout, "git refs", state)

        _promote(staged_mirror, layout.mirror_path)
        manifest = _updated_manifest(layout, remote, state)
        write_json_atomic(layout.manifest_path, manifest.to_dict())
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    components = (
        ComponentResult("git mirror", ComponentStatus.COMPLETE, action),
        ComponentResult("git refs", ComponentStatus.COMPLETE, _state_message(state)),
        ComponentResult("git integrity", ComponentStatus.COMPLETE, "Git fsck passed."),
        ComponentResult("manifest", ComponentStatus.COMPLETE, "Manifest updated."),
    )
    return OperationResult(
        operation=operation, archive_path=layout.path, components=components
    )


@dataclass(frozen=True)
class MirrorState:
    """Git metadata needed for the manifest and human-readable result."""

    ref_count: int
    branch_count: int
    tag_count: int
    head: str | None
    source_url: str


def _read_mirror_state(path: Path, runner: GitRunner) -> MirrorState | CommandResult:
    refs = runner.git("show-ref", "--head", cwd=path)
    if not refs.succeeded and refs.returncode != 1:
        return refs
    head = runner.git("symbolic-ref", "-q", "HEAD", cwd=path)
    if not head.succeeded and head.returncode != 1:
        return head
    source = runner.git("remote", "get-url", "origin", cwd=path)
    if not source.succeeded:
        return source
    names = [line.partition(" ")[2] for line in refs.stdout.splitlines() if " " in line]
    return MirrorState(
        ref_count=len(names),
        branch_count=sum(name.startswith("refs/heads/") for name in names),
        tag_count=sum(name.startswith("refs/tags/") for name in names),
        head=head.stdout.strip() or None,
        source_url=source.stdout.strip(),
    )


def _updated_manifest(
    layout: ArchiveLayout, remote: Remote, state: MirrorState
) -> Manifest:
    try:
        existing = load_manifest(layout.manifest_path)
    except (FileNotFoundError, ValueError):
        existing = Manifest.new(remote)
    archive = dict(existing.archive)
    archive["last_updated_at"] = _utc_timestamp()
    archive["tool_version"] = archive.get("tool_version") or "0.1.0"
    git = dict(existing.git)
    git.update(
        {
            "status": "complete",
            "mirror_path": "mirror.git",
            "ref_count": state.ref_count,
            "head": state.head,
        }
    )
    source = {
        "url": display_remote(remote),
        "host": remote.host,
        "owner": remote.owner,
        "repository": remote.repository,
    }
    return replace(existing, source=source, archive=archive, git=git)


def _promote(staged_mirror: Path, mirror_path: Path) -> None:
    """Replace a mirror only after staging has succeeded, retaining rollback safety."""
    previous = mirror_path.with_name(f".mirror-previous-{uuid4().hex}")
    moved_previous = False
    try:
        if mirror_path.exists():
            mirror_path.replace(previous)
            moved_previous = True
        staged_mirror.replace(mirror_path)
    except BaseException:
        if moved_previous and not mirror_path.exists() and previous.exists():
            previous.replace(mirror_path)
        raise
    else:
        if moved_previous:
            shutil.rmtree(previous, onexc=_remove_readonly)


def _failure_result(
    operation: str,
    layout: ArchiveLayout,
    name: str,
    command: CommandResult,
    error_kind: ErrorKind | None = None,
) -> OperationResult:
    message = _command_message(command)
    return OperationResult(
        operation=operation,
        archive_path=layout.path,
        components=(
            ComponentResult(
                name,
                ComponentStatus.FAILED,
                message,
                error_kind or _classify_error(command),
            ),
        ),
        errors=(message,),
    )


def _command_message(command: CommandResult) -> str:
    detail = command.stderr.strip() or command.stdout.strip()
    return detail or f"Git command failed with exit code {command.returncode}."


def _classify_error(command: CommandResult) -> ErrorKind:
    detail = f"{command.stdout}\n{command.stderr}".lower()
    if any(
        term in detail
        for term in ("authentication", "authorization", "permission denied")
    ):
        return ErrorKind.AUTHENTICATION
    if (
        "not a git repository" in detail
        or "does not appear to be a git repository" in detail
    ):
        return ErrorKind.CONFIGURATION
    return ErrorKind.GENERAL


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _state_message(state: MirrorState) -> str:
    return (
        f"{state.ref_count} refs, {state.branch_count} branches, "
        f"{state.tag_count} tags."
    )


def _remove_readonly(function: object, path: str, error: BaseException) -> None:
    """Retry Windows Git object cleanup after removing its read-only attribute."""
    if not isinstance(error, PermissionError):
        raise error
    Path(path).chmod(stat.S_IWRITE)
    function(path)  # type: ignore[operator]
