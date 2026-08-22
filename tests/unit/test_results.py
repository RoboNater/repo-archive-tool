"""Tests for result aggregation, exit codes, and stable reporting."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from repo_archive.reporting import (
    add_report_write_warning,
    emit_result,
    write_latest_reports,
)
from repo_archive.results import (
    ComponentResult,
    ComponentStatus,
    ErrorKind,
    OperationResult,
    Outcome,
)


@pytest.mark.parametrize(
    ("components", "outcome", "exit_code"),
    [
        ((), Outcome.COMPLETE, 0),
        (
            (ComponentResult("git", ComponentStatus.WARNING),),
            Outcome.COMPLETE_WITH_WARNINGS,
            0,
        ),
        ((ComponentResult("lfs", ComponentStatus.PARTIAL),), Outcome.PARTIAL, 3),
        ((ComponentResult("git", ComponentStatus.FAILED),), Outcome.FAILED, 1),
        (
            (
                ComponentResult(
                    "verify", ComponentStatus.FAILED, error_kind=ErrorKind.VERIFICATION
                ),
            ),
            Outcome.FAILED,
            4,
        ),
        (
            (
                ComponentResult(
                    "remote",
                    ComponentStatus.FAILED,
                    error_kind=ErrorKind.AUTHENTICATION,
                ),
            ),
            Outcome.FAILED,
            5,
        ),
    ],
)
def test_operation_outcome_and_exit_code(
    components: tuple[ComponentResult, ...], outcome: Outcome, exit_code: int
) -> None:
    result = OperationResult("backup", Path("archive"), components)

    assert result.outcome == outcome
    assert result.exit_code == exit_code


def test_json_output_has_stable_required_fields() -> None:
    result = OperationResult("backup", Path("archive"))
    stream = io.StringIO()

    emit_result(result, stream, as_json=True)

    assert json.loads(stream.getvalue()) == {
        "operation": "backup",
        "archive_path": "archive",
        "outcome": "complete",
        "components": [],
        "warnings": [],
        "errors": [],
        "exit_code": 0,
    }


def test_public_error_messages_produce_a_failure_result() -> None:
    result = OperationResult("backup", Path("archive"), errors=("backup failed",))

    assert result.outcome == Outcome.FAILED
    assert result.exit_code == 1


def test_latest_reports_are_written_atomically(tmp_path: Path) -> None:
    result = OperationResult(
        "verify", tmp_path, (ComponentResult("git", ComponentStatus.COMPLETE),)
    )
    write_latest_reports(tmp_path / "reports", result)

    assert (
        json.loads((tmp_path / "reports" / "latest.json").read_text(encoding="utf-8"))[
            "operation"
        ]
        == "verify"
    )
    assert "RESULT: complete" in (tmp_path / "reports" / "latest.txt").read_text(
        encoding="utf-8"
    )


def test_report_write_warning_is_not_duplicated() -> None:
    result = OperationResult("restore", Path("archive"))

    first = add_report_write_warning(result, OSError("read-only media"))
    repeated = add_report_write_warning(first, OSError("still read-only"))

    assert repeated is first
    assert len(repeated.warnings) == 1
