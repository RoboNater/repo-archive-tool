"""Archive-set layout management and core mirror backup operations."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from repo_archive.filesystem import remove_readonly
from repo_archive.git import CommandResult, GitRunner
from repo_archive.lfs import archive_lfs
from repo_archive.manifest import Manifest, load_manifest, write_json_atomic
from repo_archive.remote import (
    ArchiveNaming,
    Remote,
    derive_archive_path,
    display_remote,
    legacy_archive_candidates,
    normalize_remote,
    same_repository_identity,
)
from repo_archive.reporting import write_latest_reports
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
)
from repo_archive.submodules import inspect_submodules


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


def resolve_backup_layout(
    root: Path,
    remote: Remote,
    *,
    name: str | None = None,
    naming: ArchiveNaming = "easy",
    reuse_legacy: bool = True,
) -> ArchiveLayout:
    """Resolve a backup destination, reusing a matching digest-era archive."""
    preferred = derive_archive_path(root, remote, name, naming=naming)
    selected = preferred
    if name is None and naming == "easy" and reuse_legacy and not preferred.exists():
        candidates = legacy_archive_candidates(root, remote)
        exact_legacy = derive_archive_path(root, remote, naming="pedantic")
        matches: list[Path] = []
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                source_url = str(
                    load_manifest(candidate / "manifest.json").source["url"]
                )
                archived_remote = normalize_remote(source_url)
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                # The exact digest path may be a failed legacy attempt whose
                # manifest was never published. Let normal identity validation
                # inspect its mirror, or let backup finish that same destination.
                if candidate == exact_legacy:
                    matches.append(candidate)
                continue
            if same_repository_identity(archived_remote, remote):
                matches.append(candidate)

        if len(matches) == 1:
            selected = matches[0]
        elif len(matches) > 1:
            paths = ", ".join(str(path) for path in matches)
            raise ValueError(
                "Multiple legacy archives match this repository: "
                f"{paths}. Pass one path to update, or use --no-legacy-reuse, "
                "--name, or --naming pedantic to choose a new backup path."
            )

    _validate_archive_boundary(root.resolve(), selected)
    return ArchiveLayout(selected)


def _validate_archive_boundary(root: Path, selected: Path) -> None:
    """Refuse layouts that would place one archive set inside another."""
    for ancestor in selected.parents:
        if root != ancestor and root not in ancestor.parents:
            break
        if _looks_like_archive(ancestor):
            if ancestor == root:
                remedy = "Choose a different --root."
            else:
                remedy = "Choose --name to place it outside that archive."
            raise ValueError(
                f"Archive path would be nested inside the existing archive at "
                f"{ancestor}. {remedy}"
            )
        if ancestor == root:
            break

    if not selected.exists():
        return
    selected_is_archive = _looks_like_archive(selected)
    nested = _find_descendant_archive(
        selected, skip_archive_internals=selected_is_archive
    )
    if nested is not None:
        if selected_is_archive:
            remedy = (
                "Move the nested archive outside this archive set. To refresh the "
                "parent without repairing the layout, run update directly on this "
                "archive path."
            )
        else:
            remedy = "Choose --name or --naming pedantic."
        raise ValueError(
            f"Archive path would contain the existing archive at {nested}. {remedy}"
        )


def _find_descendant_archive(
    path: Path, *, skip_archive_internals: bool = False
) -> Path | None:
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            children = tuple(current.iterdir())
        except OSError as error:
            raise ValueError(
                f"Could not inspect archive path boundaries at {current}: {error}"
            ) from error
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if _is_archive_scratch_directory(child):
                continue
            if _looks_like_archive(child):
                return child
            if (
                skip_archive_internals
                and current == path
                and child.name
                in {
                    "mirror.git",
                    "reports",
                    "snapshots",
                    "metadata",
                }
            ):
                continue
            pending.append(child)
    return None


def _looks_like_archive(path: Path) -> bool:
    return (path / "manifest.json").exists() or (path / "mirror.git").exists()


def _is_archive_scratch_directory(path: Path) -> bool:
    return path.name.startswith((".mirror-staging-", ".mirror-previous-"))


def backup_archive(
    remote_url: str,
    layout: ArchiveLayout,
    *,
    lfs_enabled: bool = True,
    runner: GitRunner | None = None,
) -> OperationResult:
    """Create or safely refresh *layout* from *remote_url*.

    Existing mirrors are never fetched in place.  A local mirror clone is
    refreshed and validated in a sibling staging directory before it replaces
    the current mirror.
    """
    remote = normalize_remote(remote_url)
    result = _create_or_update(
        layout,
        remote,
        runner or GitRunner(),
        "backup",
        lfs_enabled=lfs_enabled,
    )
    return _record_result(layout, result)


def update_archive(
    layout: ArchiveLayout,
    *,
    lfs_enabled: bool = True,
    runner: GitRunner | None = None,
) -> OperationResult:
    """Safely refresh an archive using the source remote configured in its mirror."""
    runner = runner or GitRunner()
    source = runner.git("remote", "get-url", "origin", cwd=layout.mirror_path)
    if not source.succeeded:
        return _record_result(
            layout, _failure_result("update", layout, "source remote", source)
        )
    try:
        remote = normalize_remote(source.stdout.strip())
    except ValueError as error:
        return _record_result(
            layout,
            OperationResult(
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
            ),
        )
    return _record_result(
        layout,
        _create_or_update(layout, remote, runner, "update", lfs_enabled=lfs_enabled),
    )


def _create_or_update(
    layout: ArchiveLayout,
    remote: Remote,
    runner: GitRunner,
    operation: str,
    *,
    lfs_enabled: bool,
) -> OperationResult:
    layout.ensure_directories()
    existing = layout.mirror_path.exists()
    identity_error = _validate_source_identity(
        layout, remote, runner, existing, operation
    )
    if identity_error is not None:
        return identity_error
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

        lfs = archive_lfs(
            staged_mirror,
            runner,
            enabled=lfs_enabled,
            previous_mirror=layout.mirror_path if existing else None,
        )
        if not lfs.promotable:
            return OperationResult(
                operation=operation,
                archive_path=layout.path,
                components=(
                    ComponentResult(
                        "git mirror",
                        ComponentStatus.PARTIAL,
                        f"{action} The staged mirror was not promoted; the existing "
                        "archived mirror is unchanged.",
                    ),
                    ComponentResult(
                        "git refs",
                        ComponentStatus.PARTIAL,
                        "Staged refs were enumerated but not published: "
                        + _state_message(state),
                    ),
                    ComponentResult(
                        "git integrity",
                        ComponentStatus.PARTIAL,
                        "The staged mirror passed Git fsck but was not promoted.",
                    ),
                    lfs.component,
                    ComponentResult(
                        "submodules",
                        ComponentStatus.PARTIAL,
                        "Submodule inspection was skipped because the staged mirror "
                        "could not be promoted.",
                    ),
                    ComponentResult(
                        "manifest",
                        ComponentStatus.PARTIAL,
                        "Manifest not updated; the previous manifest was retained.",
                    ),
                ),
            )
        submodules = inspect_submodules(staged_mirror, runner)

        _promote(staged_mirror, layout.mirror_path)
        manifest = _updated_manifest(
            layout, remote, state, lfs.manifest, submodules.manifest
        )
        write_json_atomic(layout.manifest_path, manifest.to_dict())
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    components = (
        ComponentResult("git mirror", ComponentStatus.COMPLETE, action),
        ComponentResult("git refs", ComponentStatus.COMPLETE, _state_message(state)),
        ComponentResult("git integrity", ComponentStatus.COMPLETE, "Git fsck passed."),
        lfs.component,
        submodules.component,
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
    layout: ArchiveLayout,
    remote: Remote,
    state: MirrorState,
    lfs: dict[str, object],
    submodules: dict[str, object],
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
    return replace(
        existing,
        source=source,
        archive=archive,
        git=git,
        lfs=lfs,
        submodules=submodules,
    )


def _validate_source_identity(
    layout: ArchiveLayout,
    requested_remote: Remote,
    runner: GitRunner,
    mirror_exists: bool,
    operation: str,
) -> OperationResult | None:
    """Prevent a named archive from being silently repurposed for another source."""
    if not mirror_exists:
        return None
    try:
        source_url = str(load_manifest(layout.manifest_path).source["url"])
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        configured_remote = runner.git(
            "remote", "get-url", "origin", cwd=layout.mirror_path
        )
        if not configured_remote.succeeded:
            return _failure_result(
                operation,
                layout,
                "source identity",
                configured_remote,
                ErrorKind.CONFIGURATION,
            )
        source_url = configured_remote.stdout.strip()
    try:
        existing_remote = normalize_remote(source_url)
    except ValueError as error:
        return _configuration_failure(operation, layout, str(error))
    if same_repository_identity(existing_remote, requested_remote):
        return None
    return _configuration_failure(
        operation,
        layout,
        "Archive path belongs to a different normalized source identity than the "
        "requested remote. Choose --name NAME or --naming pedantic for a separate "
        "backup.",
    )


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
            shutil.rmtree(previous, onerror=remove_readonly)


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


def _record_result(layout: ArchiveLayout, result: OperationResult) -> OperationResult:
    """Persist the latest operation attempt without changing its archive state."""
    write_latest_reports(layout.reports_path, result)
    return result


def _configuration_failure(
    operation: str, layout: ArchiveLayout, message: str
) -> OperationResult:
    return OperationResult(
        operation=operation,
        archive_path=layout.path,
        components=(
            ComponentResult(
                "source identity",
                ComponentStatus.FAILED,
                message,
                ErrorKind.CONFIGURATION,
            ),
        ),
        errors=(message,),
    )
