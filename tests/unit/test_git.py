"""Tests for external Git command execution."""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from repo_archive.git import CommandExecutionError, GitRunner


def test_git_streams_stdout_without_retaining_it() -> None:
    lines: list[str] = []

    result = GitRunner(git_executable=sys.executable).git_stream_stdout(
        "-c",
        "print('first'); print('token=secret')",
        on_line=lines.append,
    )

    assert result.succeeded
    assert result.stdout == ""
    assert lines == ["first", "token=***"]


@patch("repo_archive.git.subprocess.run")
def test_git_captures_a_successful_command(mock_run: Mock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=("git", "status"), returncode=0, stdout="clean\n", stderr=""
    )

    result = GitRunner(timeout=30).git("status", "--short", cwd=Path("archive"))

    assert result.command == ("git", "status", "--short")
    assert result.returncode == 0
    assert result.stdout == "clean\n"
    assert result.stderr == ""
    assert result.succeeded is True
    mock_run.assert_called_once_with(
        ("git", "status", "--short"),
        cwd=Path("archive"),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=None,
        input=None,
        shell=False,
        text=True,
        timeout=30,
    )


@patch("repo_archive.git.subprocess.run")
def test_git_supports_batched_input_and_replacement_decoding(mock_run: Mock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=("git", "cat-file", "--batch-check"),
        returncode=0,
        stdout="tree-id tree\n",
        stderr="",
    )

    result = GitRunner().git("cat-file", "--batch-check", input_text="main^{tree}\n")

    assert result.stdout == "tree-id tree\n"
    assert mock_run.call_args.kwargs["input"] == "main^{tree}\n"
    assert mock_run.call_args.kwargs["errors"] == "replace"


def test_git_batch_blobs_streams_exact_binary_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runner = GitRunner()
    assert runner.git("init", str(repository)).succeeded
    first = b"first\nblob\xff"
    second = b"second blob\n"
    (repository / "first.bin").write_bytes(first)
    (repository / "second.bin").write_bytes(second)
    first_oid = runner.git(
        "hash-object", "-w", "first.bin", cwd=repository
    ).stdout.strip()
    second_oid = runner.git(
        "hash-object", "-w", "second.bin", cwd=repository
    ).stdout.strip()
    received: dict[str, bytes] = {}

    with tempfile.TemporaryFile(mode="w+b") as object_ids:
        object_ids.write(f"{first_oid}\n{second_oid}\n".encode("ascii"))
        object_ids.seek(0)
        result = runner.git_batch_blobs(
            cwd=repository,
            object_ids=object_ids,
            on_blob=lambda oid, content: received.__setitem__(oid, content),
        )

    assert result.succeeded
    assert received == {first_oid: first, second_oid: second}


@patch("repo_archive.git.subprocess.Popen")
def test_git_batch_blobs_honors_the_runner_timeout(mock_popen: Mock) -> None:
    process = Mock()
    process.stdout = io.BytesIO()
    process.wait.side_effect = [
        subprocess.TimeoutExpired(("git", "cat-file", "--batch"), 1),
        0,
    ]
    mock_popen.return_value = process

    with tempfile.TemporaryFile(mode="w+b") as object_ids:
        result = GitRunner(timeout=1).git_batch_blobs(
            cwd=Path("archive"), object_ids=object_ids, on_blob=lambda *_: None
        )

    assert result.returncode == 124
    assert result.stderr == "Command timed out."
    assert process.wait.call_args_list[0].kwargs == {"timeout": 1}
    process.kill.assert_called_once()


@patch("repo_archive.git.subprocess.run")
def test_lfs_uses_its_configured_executable(mock_run: Mock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=("custom-lfs", "fetch", "--all"), returncode=0, stdout="", stderr=""
    )

    GitRunner(git_lfs_executable="custom-lfs").lfs("fetch", "--all")

    assert mock_run.call_args.args[0] == ("custom-lfs", "fetch", "--all")


@patch("repo_archive.git.subprocess.run")
def test_git_merges_command_environment(mock_run: Mock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=("git", "checkout"), returncode=0, stdout="", stderr=""
    )

    GitRunner().git("checkout", environment={"GIT_LFS_SKIP_SMUDGE": "1"})

    assert mock_run.call_args.kwargs["env"]["GIT_LFS_SKIP_SMUDGE"] == "1"


@patch("repo_archive.git.subprocess.run", side_effect=FileNotFoundError)
def test_missing_executable_is_a_structured_failure(mock_run: Mock) -> None:
    result = GitRunner(git_executable="missing-git").git("--version")

    assert result.returncode == 127
    assert result.stdout == ""
    assert result.stderr == "Executable not found: missing-git"
    assert result.succeeded is False
    mock_run.assert_called_once()


@patch(
    "repo_archive.git.subprocess.run",
    side_effect=subprocess.TimeoutExpired(("git", "fsck"), 1, output=b"partial"),
)
def test_timeout_is_a_structured_failure(mock_run: Mock) -> None:
    result = GitRunner(timeout=1).git("fsck")

    assert result.returncode == 124
    assert result.stdout == "partial"
    assert result.stderr == "Command timed out."
    mock_run.assert_called_once()


@patch("repo_archive.git.subprocess.run")
def test_checked_failure_raises_with_result(mock_run: Mock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=("git", "fsck"), returncode=1, stdout="", stderr="bad object"
    )

    with pytest.raises(CommandExecutionError) as error:
        GitRunner().git("fsck", check=True)

    assert error.value.result.returncode == 1
    assert str(error.value) == "Git command failed with exit code 1."


@patch("repo_archive.git.subprocess.run")
def test_command_results_redact_sensitive_diagnostics(mock_run: Mock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=("git", "fetch"),
        returncode=1,
        stdout="token=secret",
        stderr=(
            "fatal: https://user:password@example.test/repo.git\n"
            "Authorization: Bearer secret"
        ),
    )

    result = GitRunner().git("fetch", "https://user:password@example.test/repo.git")

    assert result.command[-1] == "https://***@example.test/repo.git"
    assert result.stdout == "token=***"
    assert (
        result.stderr == "fatal: https://***@example.test/repo.git\nAuthorization: ***"
    )
