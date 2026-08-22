"""Human-readable and machine-readable operation reporting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO

from repo_archive.manifest import write_json_atomic, write_text_atomic
from repo_archive.results import ComponentStatus, OperationResult

REPORT_WRITE_WARNING_PREFIX = "Latest operation reports could not be written:"


def render_text(result: OperationResult) -> str:
    """Format a concise human-facing operation result."""
    lines = []
    for component in result.components:
        label = (
            "OK"
            if component.status == ComponentStatus.COMPLETE
            else component.status.upper()
        )
        message = component.message or component.name
        lines.append(f"[{label}] {message}")
    lines.extend(f"[WARN] {message}" for message in result.warnings)
    lines.extend(f"[ERROR] {message}" for message in result.errors)
    lines.append(f"RESULT: {result.outcome}")
    return "\n".join(lines) + "\n"


def emit_result(result: OperationResult, stream: TextIO, *, as_json: bool) -> None:
    """Emit either stable JSON or the concise human result to *stream*."""
    if as_json:
        stream.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    else:
        stream.write(render_text(result))


def write_latest_reports(reports_path: Path, result: OperationResult) -> None:
    """Atomically update the latest JSON and text reports for an operation."""
    write_json_atomic(reports_path / "latest.json", result.to_dict())
    write_text_atomic(reports_path / "latest.txt", render_text(result))


def add_report_write_warning(
    result: OperationResult, error: OSError
) -> OperationResult:
    """Return *result* with one stable warning for an unavailable report target."""
    if any(
        warning.startswith(REPORT_WRITE_WARNING_PREFIX) for warning in result.warnings
    ):
        return result
    return OperationResult(
        operation=result.operation,
        archive_path=result.archive_path,
        components=result.components,
        warnings=result.warnings + (f"{REPORT_WRITE_WARNING_PREFIX} {error}",),
        errors=result.errors,
    )
