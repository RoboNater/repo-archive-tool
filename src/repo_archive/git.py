"""Safe wrappers around the external Git and Git LFS commands.

The archive format is native Git, so higher-level operations deliberately use
the installed Git executables instead of a Python implementation of Git.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from repo_archive.security import redact_sensitive_text


@dataclass(frozen=True)
class CommandResult:
    """Captured result of invoking an external command without a shell."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        """Whether the command returned a successful exit status."""
        return self.returncode == 0


class CommandExecutionError(RuntimeError):
    """Raised when a checked Git command does not succeed."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(f"Git command failed with exit code {result.returncode}.")


class GitRunner:
    """Run Git and Git LFS commands with captured, structured results."""

    def __init__(
        self,
        *,
        git_executable: str = "git",
        git_lfs_executable: str = "git-lfs",
        timeout: float | None = None,
    ) -> None:
        self.git_executable = git_executable
        self.git_lfs_executable = git_lfs_executable
        self.timeout = timeout

    def git(
        self,
        *arguments: str,
        cwd: Path | str | None = None,
        check: bool = False,
    ) -> CommandResult:
        """Run Git with *arguments* and return its captured result."""
        return self._run(self.git_executable, arguments, cwd=cwd, check=check)

    def lfs(
        self,
        *arguments: str,
        cwd: Path | str | None = None,
        check: bool = False,
    ) -> CommandResult:
        """Run Git LFS with *arguments* and return its captured result."""
        return self._run(self.git_lfs_executable, arguments, cwd=cwd, check=check)

    def _run(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path | str | None,
        check: bool,
    ) -> CommandResult:
        command = (executable, *arguments)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            result = CommandResult(
                command=command,
                returncode=127,
                stdout="",
                stderr=f"Executable not found: {executable}",
            )
        except subprocess.TimeoutExpired as error:
            result = CommandResult(
                command=command,
                returncode=124,
                stdout=_as_text(error.stdout),
                stderr=_as_text(error.stderr) or "Command timed out.",
            )
        else:
            result = CommandResult(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        result = CommandResult(
            command=tuple(redact_sensitive_text(part) for part in result.command),
            returncode=result.returncode,
            stdout=redact_sensitive_text(result.stdout),
            stderr=redact_sensitive_text(result.stderr),
        )

        if check and not result.succeeded:
            raise CommandExecutionError(result)
        return result


def _as_text(value: str | bytes | None) -> str:
    """Return subprocess output as text, including timeout partial output."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
