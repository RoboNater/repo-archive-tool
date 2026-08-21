"""Archive inspection and verification operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from repo_archive.archive import (
    ArchiveLayout,
    MirrorState,
    _read_mirror_state,
    _state_message,
)
from repo_archive.git import CommandResult, GitRunner
from repo_archive.lfs import verify_lfs_archive
from repo_archive.manifest import Manifest, load_manifest, write_json_atomic
from repo_archive.remote import normalize_remote
from repo_archive.reporting import write_latest_reports
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
)
from repo_archive.snapshots import discover_snapshot_paths, verify_snapshot_path
from repo_archive.submodules import summarize_submodule_definitions


def info_archive(
    layout: ArchiveLayout, *, runner: GitRunner | None = None
) -> OperationResult:
    """Return a concise, machine-readable summary of an archive set."""
    runner = runner or GitRunner()
    try:
        manifest = load_manifest(layout.manifest_path)
    except (FileNotFoundError, ValueError) as error:
        return _record(layout, _manifest_failure("info", layout, error))

    state = _read_mirror_state(layout.mirror_path, runner)
    components = [
        ComponentResult("manifest", ComponentStatus.COMPLETE, "Manifest loaded."),
        _source_component(manifest),
        _archive_component(manifest),
        _status_component("lfs", manifest.lfs),
        _status_component("submodules", manifest.submodules),
        ComponentResult(
            "snapshots",
            ComponentStatus.COMPLETE,
            f"{len(discover_snapshot_paths(layout))} self-contained snapshots "
            "available.",
        ),
        _metadata_component(manifest),
        _verification_component(manifest),
    ]
    if isinstance(state, CommandResult):
        components.append(
            _command_component("git refs", state, ErrorKind.CONFIGURATION)
        )
    else:
        components.append(
            ComponentResult("git refs", ComponentStatus.COMPLETE, _state_message(state))
        )
    return _record(
        layout,
        OperationResult("info", layout.path, components=tuple(components)),
    )


def verify_archive(
    layout: ArchiveLayout,
    *,
    full: bool = True,
    deep: bool = False,
    runner: GitRunner | None = None,
) -> OperationResult:
    """Verify archive structure and, in full mode, Git object integrity and bundles."""
    runner = runner or GitRunner()
    components: list[ComponentResult] = []
    try:
        manifest = load_manifest(layout.manifest_path)
    except (FileNotFoundError, ValueError) as error:
        return _record(layout, _manifest_failure("verify", layout, error))

    components.append(
        ComponentResult("manifest", ComponentStatus.COMPLETE, "Manifest loaded.")
    )
    components.append(_snapshot_index_component(layout, manifest))
    components.append(_status_component("submodules", manifest.submodules))
    bare = runner.git("rev-parse", "--is-bare-repository", cwd=layout.mirror_path)
    if not bare.succeeded:
        components.append(
            _command_component("repository structure", bare, ErrorKind.CONFIGURATION)
        )
    elif bare.stdout.strip().lower() != "true":
        components.append(
            ComponentResult(
                "repository structure",
                ComponentStatus.FAILED,
                "Archive mirror is not a bare Git repository.",
                ErrorKind.CONFIGURATION,
            )
        )
    else:
        components.append(
            ComponentResult(
                "repository structure",
                ComponentStatus.COMPLETE,
                "Valid bare Git repository.",
            )
        )

    source = runner.git("remote", "get-url", "origin", cwd=layout.mirror_path)
    components.append(_source_verification_component(manifest, source))
    state = _read_mirror_state(layout.mirror_path, runner)
    if isinstance(state, CommandResult):
        components.append(_command_component("git refs", state, ErrorKind.VERIFICATION))
    else:
        components.append(
            ComponentResult("git refs", ComponentStatus.COMPLETE, _state_message(state))
        )
        components.append(_manifest_path_component(manifest, layout, state))

    if full:
        fsck = runner.git("fsck", "--full", cwd=layout.mirror_path)
        components.append(
            _command_component(
                "git integrity", fsck, ErrorKind.VERIFICATION, "Git fsck passed."
            )
        )
        components.append(_lfs_verification_component(layout, manifest, runner))
        components.extend(_verify_bundles(layout, runner, deep=deep))

    result = OperationResult("verify", layout.path, components=tuple(components))
    if all(component.status != ComponentStatus.FAILED for component in components):
        mode = "deep" if deep else ("full" if full else "quick")
        _write_successful_verification(layout, manifest, mode, result.outcome.value)
    return _record(layout, result)


def _source_component(manifest: Manifest) -> ComponentResult:
    return ComponentResult(
        "source", ComponentStatus.COMPLETE, str(manifest.source["url"])
    )


def _archive_component(manifest: Manifest) -> ComponentResult:
    created = manifest.archive.get("created_at", "unknown")
    updated = manifest.archive.get("last_updated_at", "unknown")
    return ComponentResult(
        "archive",
        ComponentStatus.COMPLETE,
        f"Created: {created}; last updated: {updated}.",
    )


def _status_component(name: str, value: dict[str, object]) -> ComponentResult:
    status = str(value.get("status", "not-run"))
    if status == "partial":
        component_status = ComponentStatus.PARTIAL
    elif status in {"not-archived", "complete-with-warnings"}:
        component_status = ComponentStatus.WARNING
    else:
        component_status = ComponentStatus.COMPLETE
    detail = f"Status: {status}."
    if name == "lfs" and value.get("detected"):
        count = value.get("expected_object_count")
        detail += " Git LFS detected"
        detail += f"; {count} expected objects." if count is not None else "."
    if name == "submodules" and value.get("detected"):
        repositories = value.get("repositories", [])
        count = len(repositories) if isinstance(repositories, list) else 0
        detail += f" {count} definition(s); repositories are not recursively archived."
        summary = summarize_submodule_definitions(repositories)
        if summary:
            detail += f" {summary}."
    return ComponentResult(name, component_status, detail)


def _lfs_verification_component(
    layout: ArchiveLayout, manifest: Manifest, runner: GitRunner
) -> ComponentResult:
    verified = verify_lfs_archive(layout.mirror_path, runner)
    if (
        manifest.lfs.get("status") == "partial"
        and verified.status == ComponentStatus.COMPLETE
    ):
        return ComponentResult(
            "lfs",
            ComponentStatus.PARTIAL,
            (verified.message or "Git LFS verification passed.")
            + " The manifest still records an intentionally or previously partial LFS "
            "archive; run backup or update with LFS enabled.",
        )
    return verified


def _metadata_component(manifest: Manifest) -> ComponentResult:
    github = manifest.metadata.get("github", {})
    return _status_component("metadata", github if isinstance(github, dict) else {})


def _verification_component(manifest: Manifest) -> ComponentResult:
    verified_at = manifest.archive.get("last_verified_at")
    message = "No verification has been recorded."
    if verified_at:
        outcome = manifest.archive.get("last_verification_outcome", "unknown")
        message = f"Last verification: {verified_at}; outcome: {outcome}."
    return ComponentResult("last verification", ComponentStatus.COMPLETE, message)


def _snapshot_index_component(
    layout: ArchiveLayout, manifest: Manifest
) -> ComponentResult:
    entries = manifest.archive.get("snapshots", [])
    if not isinstance(entries, list):
        return ComponentResult(
            "snapshot index",
            ComponentStatus.FAILED,
            "Manifest snapshot index is not a list.",
            ErrorKind.VERIFICATION,
        )
    indexed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return ComponentResult(
                "snapshot index",
                ComponentStatus.FAILED,
                "Manifest snapshot index contains an invalid entry.",
                ErrorKind.VERIFICATION,
            )
        indexed.add(str(entry["path"]).replace("\\", "/"))
    present = {
        path.relative_to(layout.path).as_posix()
        for path in discover_snapshot_paths(layout)
    }
    if indexed == present:
        return ComponentResult(
            "snapshot index",
            ComponentStatus.COMPLETE,
            f"Snapshot index matches {len(present)} published snapshot(s).",
        )
    details = []
    if stale := sorted(indexed - present):
        details.append("missing on disk: " + ", ".join(stale))
    if unindexed := sorted(present - indexed):
        details.append("not indexed: " + ", ".join(unindexed))
    return ComponentResult(
        "snapshot index",
        ComponentStatus.WARNING,
        "Manifest snapshot index does not match disk (" + "; ".join(details) + ").",
    )


def _source_verification_component(
    manifest: Manifest, source: CommandResult
) -> ComponentResult:
    if not source.succeeded:
        return _command_component("source remote", source, ErrorKind.CONFIGURATION)
    try:
        matches = (
            normalize_remote(source.stdout.strip()).canonical_url
            == normalize_remote(str(manifest.source["url"])).canonical_url
        )
    except (KeyError, TypeError, ValueError):
        matches = False
    if matches:
        return ComponentResult(
            "source remote", ComponentStatus.COMPLETE, "Source remote matches manifest."
        )
    return ComponentResult(
        "source remote",
        ComponentStatus.FAILED,
        "Source remote does not match manifest.",
        ErrorKind.CONFIGURATION,
    )


def _manifest_path_component(
    manifest: Manifest, layout: ArchiveLayout, state: MirrorState
) -> ComponentResult:
    if manifest.git.get("mirror_path") != "mirror.git":
        return ComponentResult(
            "manifest consistency",
            ComponentStatus.FAILED,
            "Manifest mirror path is not mirror.git.",
            ErrorKind.CONFIGURATION,
        )
    if manifest.git.get("ref_count") != state.ref_count:
        return ComponentResult(
            "manifest consistency",
            ComponentStatus.WARNING,
            "Manifest ref count differs from the mirror; run update to refresh it.",
        )
    return ComponentResult(
        "manifest consistency",
        ComponentStatus.COMPLETE,
        "Manifest paths and ref count match.",
    )


def _verify_bundles(
    layout: ArchiveLayout, runner: GitRunner, *, deep: bool
) -> list[ComponentResult]:
    snapshots = discover_snapshot_paths(layout)
    legacy_paths = _legacy_bundle_paths(layout)
    if not snapshots and not legacy_paths:
        return [
            ComponentResult(
                "bundle snapshots",
                ComponentStatus.COMPLETE,
                "No bundle snapshots to verify.",
            )
        ]
    components: list[ComponentResult] = []
    for path in snapshots:
        verified = verify_snapshot_path(path, runner=runner, deep=deep)
        if verified.outcome == "verified-complete":
            status = ComponentStatus.COMPLETE
            kind = None
        elif verified.outcome == "verified-partial":
            status = ComponentStatus.PARTIAL
            kind = None
        else:
            status = ComponentStatus.FAILED
            kind = ErrorKind.VERIFICATION
        components.append(
            ComponentResult(f"snapshot {path.name}", status, verified.message, kind)
        )
    components.extend(
        _command_component(
            f"bundle {path.name}",
            runner.git("bundle", "verify", str(path), cwd=layout.mirror_path),
            ErrorKind.VERIFICATION,
            f"Bundle {path.name} verified.",
        )
        for path in legacy_paths
    )
    return components


def _legacy_bundle_paths(layout: ArchiveLayout) -> list[Path]:
    if not layout.snapshots_path.is_dir():
        return []
    return sorted(layout.snapshots_path.glob("*.bundle"))


def _command_component(
    name: str, command: CommandResult, kind: ErrorKind, success: str | None = None
) -> ComponentResult:
    if command.succeeded:
        return ComponentResult(
            name, ComponentStatus.COMPLETE, success or f"{name} passed."
        )
    message = (
        command.stderr.strip()
        or command.stdout.strip()
        or f"Git command failed with exit code {command.returncode}."
    )
    return ComponentResult(name, ComponentStatus.FAILED, message, kind)


def _manifest_failure(
    operation: str, layout: ArchiveLayout, error: Exception
) -> OperationResult:
    return OperationResult(
        operation,
        layout.path,
        components=(
            ComponentResult(
                "manifest", ComponentStatus.FAILED, str(error), ErrorKind.CONFIGURATION
            ),
        ),
        errors=(str(error),),
    )


def _write_successful_verification(
    layout: ArchiveLayout, manifest: Manifest, mode: str, outcome: str
) -> None:
    archive = dict(manifest.archive)
    archive.update(
        {
            "last_verified_at": _timestamp(),
            "last_verification_mode": mode,
            "last_verification_outcome": outcome,
        }
    )
    write_json_atomic(
        layout.manifest_path, replace(manifest, archive=archive).to_dict()
    )


def _record(layout: ArchiveLayout, result: OperationResult) -> OperationResult:
    write_latest_reports(layout.reports_path, result)
    return result


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
