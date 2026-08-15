"""Structured operation results, completeness aggregation, and exit codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ComponentStatus(StrEnum):
    COMPLETE = "complete"
    WARNING = "warning"
    PARTIAL = "partial"
    FAILED = "failed"


class Outcome(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_WARNINGS = "complete-with-warnings"
    PARTIAL = "partial"
    FAILED = "failed"


class ErrorKind(StrEnum):
    GENERAL = "general"
    CONFIGURATION = "configuration"
    VERIFICATION = "verification"
    AUTHENTICATION = "authentication"


@dataclass(frozen=True)
class ComponentResult:
    """The result of one independently reportable archive component."""

    name: str
    status: ComponentStatus
    message: str | None = None
    error_kind: ErrorKind | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "error_kind": self.error_kind,
        }


@dataclass(frozen=True)
class OperationResult:
    """Stable result object for CLI output, reports, and callers."""

    operation: str
    archive_path: Path | None
    components: tuple[ComponentResult, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    outcome: Outcome = field(init=False)
    exit_code: int = field(init=False)

    def __post_init__(self) -> None:
        outcome = aggregate_outcome(self.components, self.warnings, self.errors)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "exit_code", exit_code_for(outcome, self.components))

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "archive_path": str(self.archive_path) if self.archive_path else None,
            "outcome": self.outcome,
            "components": [component.to_dict() for component in self.components],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "exit_code": self.exit_code,
        }


def aggregate_outcome(
    components: tuple[ComponentResult, ...],
    warnings: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
) -> Outcome:
    """Aggregate component results using the documented completeness model."""
    statuses = {component.status for component in components}
    if errors:
        return Outcome.FAILED
    if ComponentStatus.FAILED in statuses:
        return Outcome.FAILED
    if ComponentStatus.PARTIAL in statuses:
        return Outcome.PARTIAL
    if ComponentStatus.WARNING in statuses:
        return Outcome.COMPLETE_WITH_WARNINGS
    if warnings:
        return Outcome.COMPLETE_WITH_WARNINGS
    return Outcome.COMPLETE


def exit_code_for(outcome: Outcome, components: tuple[ComponentResult, ...]) -> int:
    """Map a result to the documented, stable exit-code convention."""
    failed_kinds = {
        item.error_kind for item in components if item.status == ComponentStatus.FAILED
    }
    if ErrorKind.AUTHENTICATION in failed_kinds:
        return 5
    if ErrorKind.VERIFICATION in failed_kinds:
        return 4
    if ErrorKind.CONFIGURATION in failed_kinds:
        return 2
    if outcome == Outcome.PARTIAL:
        return 3
    if outcome == Outcome.FAILED:
        return 1
    return 0
