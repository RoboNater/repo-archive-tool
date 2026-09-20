"""Public-command coverage for how the authoritative LFS gap is reported.

These are the review-round regressions for PR #22: current verification must
override a stale historical status, the exact gap must reach the CLI result,
the report, and the persisted manifest, and a failed transfer must still be
measured against the local store.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from lfs_helpers import (
    LFS_ATTRIBUTES,
    NoLfsToolingRunner,
    git,
    make_repository,
    pointer_for,
    store_object,
)

from repo_archive.archive import ArchiveLayout, backup_archive
from repo_archive.cli import main
from repo_archive.git import CommandResult, GitRunner
from repo_archive.inspection import verify_archive
from repo_archive.manifest import load_manifest, write_json_atomic
from repo_archive.results import ComponentStatus, Outcome
from repo_archive.snapshots import create_snapshot

_PAYLOAD = b"review payload\n"


class AvailableLfsToolingRunner(GitRunner):
    """Use real Git while simulating a machine that has `git-lfs` installed."""

    def lfs(self, *arguments: str, **kwargs: object) -> CommandResult:
        return CommandResult(("git", "lfs", *arguments), 0, "git-lfs/3.0.0", "")


class FetchFailureRunner(GitRunner):
    """Real Git with available Git LFS tooling whose archival fetch fails."""

    def lfs(self, *arguments: str, **kwargs: object) -> CommandResult:
        if arguments and arguments[0] == "fetch":
            return CommandResult(
                ("git", "lfs", *arguments), 1, "", "simulated transfer failure"
            )
        return CommandResult(("git", "lfs", *arguments), 0, "git-lfs/3.0.0", "")


def make_source(tmp_path: Path) -> tuple[Path, str]:
    """Build a one-pointer remote and return it with its required OID."""
    oid, pointer = pointer_for(_PAYLOAD)
    source = make_repository(
        tmp_path / "source",
        {
            ".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n",
            "payload.bin": pointer,
        },
    )
    return source, oid


def archive_without_payload(tmp_path: Path) -> tuple[ArchiveLayout, str]:
    """Create a real archive whose one required LFS object is absent."""
    source, oid = make_source(tmp_path)
    layout = ArchiveLayout(tmp_path / "archive")
    result = backup_archive(str(source), layout, runner=NoLfsToolingRunner())
    assert result.outcome is Outcome.PARTIAL
    return layout, oid


def lfs_component(result: object) -> dict[str, object]:
    components = result.to_dict()["components"]  # type: ignore[attr-defined]
    return next(item for item in components if item["name"] == "lfs")


def test_verify_overrides_a_stale_partial_manifest(tmp_path: Path) -> None:
    """r1-1: an intact store is complete regardless of an older attempt."""
    layout, _ = archive_without_payload(tmp_path)
    store_object(layout.mirror_path, _PAYLOAD)
    manifest = load_manifest(layout.manifest_path)
    stale = {
        "detected": True,
        "status": "partial",
        "reason": "tool-unavailable",
        "tooling_available": False,
    }
    write_json_atomic(layout.manifest_path, {**manifest.to_dict(), "lfs": stale})

    result = verify_archive(layout, runner=NoLfsToolingRunner())

    assert result.outcome is Outcome.COMPLETE
    assert result.exit_code == 0
    assert lfs_component(result)["status"] == ComponentStatus.COMPLETE
    refreshed = load_manifest(layout.manifest_path).lfs
    assert refreshed["status"] == "complete"
    assert refreshed["missing_objects"] == []
    assert "reason" not in refreshed


def test_archive_and_snapshot_agree_on_the_same_content(tmp_path: Path) -> None:
    """r1-1: the discrepancy issue #15 targets must not survive verification."""
    layout, _ = archive_without_payload(tmp_path)
    store_object(layout.mirror_path, _PAYLOAD)
    manifest = load_manifest(layout.manifest_path)
    write_json_atomic(
        layout.manifest_path,
        {
            **manifest.to_dict(),
            "lfs": {
                "detected": True,
                "status": "partial",
                "reason": "tool-unavailable",
            },
        },
    )

    verified = verify_archive(layout, runner=NoLfsToolingRunner())
    snapshot = create_snapshot(layout, runner=NoLfsToolingRunner())

    assert verified.outcome is Outcome.COMPLETE
    assert snapshot.outcome is Outcome.COMPLETE
    assert verified.exit_code == snapshot.exit_code == 0


def test_verify_json_and_manifest_name_the_missing_oid(
    tmp_path: Path, capsys: object
) -> None:
    """r1-2: the exact gap must reach CLI JSON, the report, and the manifest."""
    layout, oid = archive_without_payload(tmp_path)
    manifest = load_manifest(layout.manifest_path)
    write_json_atomic(
        layout.manifest_path,
        {**manifest.to_dict(), "lfs": {"detected": True, "status": "complete"}},
    )

    with patch("sys.argv", ["repo-archive", "verify", str(layout.path), "--json"]):
        exit_code = main()
    stdout = capsys.readouterr().out  # type: ignore[attr-defined]

    assert exit_code == 3
    emitted = json.loads(stdout)
    lfs = next(item for item in emitted["components"] if item["name"] == "lfs")
    assert oid in lfs["message"]

    report = json.loads((layout.reports_path / "latest.json").read_text("utf-8"))
    reported = next(item for item in report["components"] if item["name"] == "lfs")
    assert oid in reported["message"]

    persisted = load_manifest(layout.manifest_path).lfs
    assert persisted["status"] == "partial"
    assert persisted["missing_objects"] == [oid]
    # Whether git-lfs is installed varies by machine; the contract is that
    # verification records what it found. The False case is covered above with
    # an explicit no-tooling runner.
    assert isinstance(persisted["tooling_available"], bool)


def test_failed_fetch_still_measures_the_local_gap(tmp_path: Path) -> None:
    """r1-3: a failed transfer is measured, not reported as an unknown."""
    source, oid = make_source(tmp_path)
    layout = ArchiveLayout(tmp_path / "archive")

    result = backup_archive(str(source), layout, runner=FetchFailureRunner())

    assert result.outcome is Outcome.PARTIAL
    assert result.exit_code == 3
    message = lfs_component(result)["message"]
    assert oid in message
    assert "simulated transfer failure" in message

    persisted = load_manifest(layout.manifest_path).lfs
    assert persisted["reason"] == "fetch-failed"
    assert persisted["missing_objects"] == [oid]
    assert persisted["expected_object_count"] == 1
    assert persisted["tracking_declared"] is True
    assert persisted["tooling_available"] is True
    assert "simulated transfer failure" in str(persisted["diagnostic"])


def test_failed_fetch_with_an_intact_store_is_complete(tmp_path: Path) -> None:
    """A transfer failure is not incompleteness when nothing is actually missing."""
    source, _ = make_source(tmp_path)
    layout = ArchiveLayout(tmp_path / "archive")
    seeded = backup_archive(str(source), layout, runner=NoLfsToolingRunner())
    assert seeded.outcome is Outcome.PARTIAL
    store_object(layout.mirror_path, _PAYLOAD)

    result = backup_archive(str(source), layout, runner=FetchFailureRunner())

    assert result.outcome is Outcome.COMPLETE
    assert result.exit_code == 0
    persisted = load_manifest(layout.manifest_path).lfs
    assert persisted["status"] == "complete"
    assert persisted["missing_objects"] == []


def test_quick_verification_leaves_the_lfs_record_untouched(tmp_path: Path) -> None:
    """Quick mode does not measure LFS, so it must not rewrite the record."""
    layout, oid = archive_without_payload(tmp_path)
    before = load_manifest(layout.manifest_path).lfs

    result = verify_archive(layout, full=False, runner=NoLfsToolingRunner())

    assert result.outcome is Outcome.COMPLETE
    assert load_manifest(layout.manifest_path).lfs == before


def test_intentional_no_lfs_is_reconciled_by_a_later_verification(
    tmp_path: Path,
) -> None:
    """A --no-lfs archive that already holds everything verifies complete."""
    source, _ = make_source(tmp_path)
    layout = ArchiveLayout(tmp_path / "archive")
    skipped = backup_archive(str(source), layout, lfs_enabled=False)
    assert skipped.outcome is Outcome.PARTIAL
    assert load_manifest(layout.manifest_path).lfs["reason"] == "disabled"
    store_object(layout.mirror_path, _PAYLOAD)

    result = verify_archive(layout, runner=NoLfsToolingRunner())

    assert result.outcome is Outcome.COMPLETE
    assert load_manifest(layout.manifest_path).lfs["status"] == "complete"


def test_bounded_gap_summary_does_not_flood_output(tmp_path: Path) -> None:
    """A large gap is summarized in messages while the manifest keeps every OID."""
    files = {".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n"}
    oids = []
    for index in range(8):
        oid, pointer = pointer_for(f"payload {index}\n".encode())
        oids.append(oid)
        files[f"payload{index}.bin"] = pointer
    source = make_repository(tmp_path / "source", files)
    layout = ArchiveLayout(tmp_path / "archive")

    result = backup_archive(str(source), layout, runner=NoLfsToolingRunner())

    message = str(lfs_component(result)["message"])
    assert "and 3 more" in message
    assert sum(oid in message for oid in oids) == 5
    assert sorted(load_manifest(layout.manifest_path).lfs["missing_objects"]) == sorted(
        oids
    )


def test_update_reports_the_gap_after_a_failed_fetch(tmp_path: Path) -> None:
    """The measured gap reaches `update` as well as `backup`."""
    source, oid = make_source(tmp_path)
    layout = ArchiveLayout(tmp_path / "archive")
    assert backup_archive(str(source), layout, lfs_enabled=False).exit_code == 3
    git("update-ref", "refs/heads/other", "refs/heads/main", cwd=source)

    from repo_archive.archive import update_archive

    result = update_archive(layout, runner=FetchFailureRunner())

    assert result.outcome is Outcome.PARTIAL
    persisted = load_manifest(layout.manifest_path).lfs
    assert persisted["reason"] == "fetch-failed"
    assert persisted["missing_objects"] == [oid]


def test_verify_states_tooling_is_unavailable(tmp_path: Path) -> None:
    """r1-2 residual: availability must reach the emitted and persisted output."""
    layout, oid = archive_without_payload(tmp_path)

    result = verify_archive(layout, runner=NoLfsToolingRunner())

    message = str(lfs_component(result)["message"])
    assert "Git LFS tooling is unavailable on this machine." in message
    assert oid in message
    report = json.loads((layout.reports_path / "latest.json").read_text("utf-8"))
    reported = next(item for item in report["components"] if item["name"] == "lfs")
    assert "unavailable on this machine" in reported["message"]
    assert load_manifest(layout.manifest_path).lfs["tooling_available"] is False


def test_verify_states_tooling_is_available(tmp_path: Path) -> None:
    """The same archive reports differently when Git LFS is present."""
    layout, oid = archive_without_payload(tmp_path)

    result = verify_archive(layout, runner=AvailableLfsToolingRunner())

    message = str(lfs_component(result)["message"])
    assert "Git LFS tooling is available." in message
    assert oid in message
    report = json.loads((layout.reports_path / "latest.json").read_text("utf-8"))
    reported = next(item for item in report["components"] if item["name"] == "lfs")
    assert "Git LFS tooling is available." in reported["message"]
    assert load_manifest(layout.manifest_path).lfs["tooling_available"] is True


def test_reports_differ_by_tooling_availability(tmp_path: Path) -> None:
    """The reviewer's repro: identical archives must not produce identical reports."""
    absent_layout, _ = archive_without_payload(tmp_path / "absent")
    present_layout, _ = archive_without_payload(tmp_path / "present")

    absent = verify_archive(absent_layout, runner=NoLfsToolingRunner())
    present = verify_archive(present_layout, runner=AvailableLfsToolingRunner())

    assert lfs_component(absent)["message"] != lfs_component(present)["message"]
    assert absent.outcome is present.outcome


def test_no_availability_claim_when_nothing_is_required(tmp_path: Path) -> None:
    """With no required objects there is nothing to fetch, so no claim is made."""
    source = make_repository(
        tmp_path / "source",
        {
            ".gitattributes": f"*.bin {LFS_ATTRIBUTES}\n",
            "data.bin": "raw content, not a pointer\n",
        },
    )
    layout = ArchiveLayout(tmp_path / "archive")
    assert (
        backup_archive(str(source), layout, runner=NoLfsToolingRunner()).exit_code == 0
    )

    result = verify_archive(layout, runner=NoLfsToolingRunner())

    message = str(lfs_component(result)["message"])
    assert "tooling" not in message
    assert "tooling_available" not in load_manifest(layout.manifest_path).lfs
