"""Safe wrappers around the external Git and Git LFS commands.

The archive format is native Git, so higher-level operations deliberately use
the installed Git executables instead of a Python implementation of Git.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import BinaryIO

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
        input_text: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run Git with *arguments* and return its captured result."""
        return self._run(
            self.git_executable,
            arguments,
            cwd=cwd,
            check=check,
            input_text=input_text,
            environment=environment,
        )

    def lfs(
        self,
        *arguments: str,
        cwd: Path | str | None = None,
        check: bool = False,
        input_text: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run Git LFS with *arguments* and return its captured result."""
        return self._run(
            self.git_lfs_executable,
            arguments,
            cwd=cwd,
            check=check,
            input_text=input_text,
            environment=environment,
        )

    def git_stream_stdout(
        self,
        *arguments: str,
        cwd: Path | str | None = None,
        on_line: Callable[[str], None],
        input_stream: BinaryIO | None = None,
    ) -> CommandResult:
        """Run Git while consuming stdout incrementally through *on_line*."""
        command = (self.git_executable, *arguments)
        try:
            return self._run_streaming(
                command, cwd=cwd, on_line=on_line, input_stream=input_stream
            )
        except FileNotFoundError:
            return self._redact_result(
                CommandResult(
                    command=command,
                    returncode=127,
                    stdout="",
                    stderr=f"Executable not found: {self.git_executable}",
                )
            )

    def git_batch_blobs(
        self,
        *,
        cwd: Path | str,
        object_ids: BinaryIO,
        on_blob: Callable[[str, bytes], None],
    ) -> CommandResult:
        """Read blob contents from one ``git cat-file --batch`` process."""
        command = (self.git_executable, "cat-file", "--batch")
        try:
            return self._run_blob_batch(
                command, cwd=cwd, object_ids=object_ids, on_blob=on_blob
            )
        except FileNotFoundError:
            return self._redact_result(
                CommandResult(
                    command=command,
                    returncode=127,
                    stdout="",
                    stderr=f"Executable not found: {self.git_executable}",
                )
            )

    def _run(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path | str | None,
        check: bool,
        input_text: str | None,
        environment: Mapping[str, str] | None,
    ) -> CommandResult:
        command = (executable, *arguments)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                env=(
                    {**os.environ, **environment} if environment is not None else None
                ),
                input=input_text,
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

        return self._finish_result(result, check=check)

    def _run_streaming(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | str | None,
        on_line: Callable[[str], None],
        input_stream: BinaryIO | None,
    ) -> CommandResult:
        reader_errors: list[BaseException] = []
        with tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace"
        ) as stderr_stream:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                encoding="utf-8",
                errors="replace",
                shell=False,
                stderr=stderr_stream,
                stdin=input_stream,
                stdout=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None

            def consume_stdout() -> None:
                try:
                    with process.stdout:
                        for line in process.stdout:
                            on_line(
                                redact_sensitive_text(
                                    line.removesuffix("\n").removesuffix("\r")
                                )
                            )
                except BaseException as error:
                    reader_errors.append(error)
                    process.kill()

            reader = Thread(target=consume_stdout, daemon=True)
            reader.start()
            timed_out = False
            try:
                returncode = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                returncode = process.wait()
            reader.join()
            stderr_stream.seek(0)
            stderr = stderr_stream.read()

        if reader_errors:
            raise reader_errors[0]
        if timed_out:
            returncode = 124
            stderr = stderr or "Command timed out."
        return self._redact_result(CommandResult(command, returncode, "", stderr))

    def _run_blob_batch(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | str,
        object_ids: BinaryIO,
        on_blob: Callable[[str, bytes], None],
    ) -> CommandResult:
        with tempfile.TemporaryFile(mode="w+b") as stderr_stream:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=False,
                stderr=stderr_stream,
                stdin=object_ids,
                stdout=subprocess.PIPE,
            )
            assert process.stdout is not None
            error_message = ""
            try:
                with process.stdout:
                    while header_bytes := process.stdout.readline():
                        header = header_bytes.decode("ascii", errors="replace").strip()
                        parts = header.split()
                        if len(parts) == 2 and parts[1] == "missing":
                            error_message = f"Git object is missing: {parts[0]}"
                            process.kill()
                            break
                        if len(parts) != 3:
                            error_message = "Git cat-file returned an invalid header."
                            process.kill()
                            break
                        oid, object_type, size_text = parts
                        try:
                            size = int(size_text)
                        except ValueError:
                            error_message = "Git cat-file returned an invalid size."
                            process.kill()
                            break
                        content = process.stdout.read(size)
                        terminator = process.stdout.read(1)
                        if len(content) != size or terminator != b"\n":
                            error_message = "Git cat-file returned truncated content."
                            process.kill()
                            break
                        if object_type == "blob":
                            on_blob(oid, content)
            except BaseException:
                process.kill()
                process.wait()
                raise
            returncode = process.wait()
            stderr_stream.seek(0)
            stderr = stderr_stream.read().decode("utf-8", errors="replace")
        if error_message:
            returncode = returncode or 1
            stderr = stderr or error_message
        return self._redact_result(CommandResult(command, returncode, "", stderr))

    def _finish_result(self, result: CommandResult, *, check: bool) -> CommandResult:
        result = self._redact_result(result)
        if check and not result.succeeded:
            raise CommandExecutionError(result)
        return result

    @staticmethod
    def _redact_result(result: CommandResult) -> CommandResult:
        return CommandResult(
            command=tuple(redact_sensitive_text(part) for part in result.command),
            returncode=result.returncode,
            stdout=redact_sensitive_text(result.stdout),
            stderr=redact_sensitive_text(result.stderr),
        )


def _as_text(value: str | bytes | None) -> str:
    """Return subprocess output as text, including timeout partial output."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
