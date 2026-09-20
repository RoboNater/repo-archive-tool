"""Shared helpers for building LFS-bearing Git history in integration tests.

History is built with plumbing commands so no clean filter can rewrite the
content, which keeps these fixtures reproducible whether or not Git LFS is
installed on the machine running the suite.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from repo_archive.git import CommandResult, GitRunner

LFS_ATTRIBUTES = "filter=lfs diff=lfs merge=lfs -text"


class NoLfsToolingRunner(GitRunner):
    """Use real Git while simulating a machine with no `git-lfs` executable."""

    def lfs(self, *arguments: str, **kwargs: object) -> CommandResult:
        return CommandResult(
            ("git", "lfs", *arguments), 127, "", "git: 'lfs' is not a git command"
        )


def git(*arguments: str, cwd: Path | None = None, stdin: str = "") -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        input=stdin,
    )
    return completed.stdout.strip()


def pointer_for(content: bytes) -> tuple[str, str]:
    """Build a valid LFS pointer blob for *content* and return (oid, pointer)."""
    oid = hashlib.sha256(content).hexdigest()
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {len(content)}\n"
    )
    return oid, pointer


def make_repository(base_path: Path, files: dict[str, str]) -> Path:
    """Commit *files* verbatim into a bare repository, bypassing any filters."""
    repository = base_path / "mirror.git"
    repository.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--bare", "-b", "main", str(repository))
    git("config", "user.name", "Archive Test", cwd=repository)
    git("config", "user.email", "archive@example.test", cwd=repository)

    entries = []
    for path, content in sorted(files.items()):
        blob = git("hash-object", "-w", "--stdin", cwd=repository, stdin=content)
        entries.append(f"100644 blob {blob}\t{path}")
    tree = git("mktree", cwd=repository, stdin="\n".join(entries) + "\n")
    commit = git("commit-tree", tree, "-m", "test history", cwd=repository)
    git("update-ref", "refs/heads/main", commit, cwd=repository)
    return repository


def store_object(mirror_path: Path, content: bytes) -> str:
    """Write *content* into an archive-local LFS store and return its OID."""
    oid = hashlib.sha256(content).hexdigest()
    object_path = mirror_path / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(content)
    return oid
