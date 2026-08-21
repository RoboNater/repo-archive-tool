"""Shared filesystem integrity, cleanup, and copy primitives."""

from __future__ import annotations

import hashlib
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_sha256_file(path: Path) -> str | None:
    """Return a digest, or ``None`` when *path* cannot be read."""
    try:
        return sha256_file(path)
    except OSError:
        return None


def remove_readonly(
    function: Callable[[str], object], path: str, exception_info: tuple[object, ...]
) -> None:
    """Retry Windows cleanup after removing a Git file's read-only attribute."""
    error = exception_info[1]
    if not isinstance(error, PermissionError):
        raise error
    Path(path).chmod(stat.S_IWRITE)
    function(path)


def try_reflink(source: Path, destination: Path) -> bool:
    """Attempt Linux FICLONE without leaving a failed destination behind."""
    if sys.platform != "linux":
        return False
    try:
        import fcntl

        with source.open("rb") as source_stream, destination.open("xb") as target:
            fcntl.ioctl(target.fileno(), 0x40049409, source_stream.fileno())
    except (ImportError, OSError):
        destination.unlink(missing_ok=True)
        return False
    return True


def copy_independent(
    source: Path, destination: Path, *, try_clone: bool
) -> tuple[str, bool]:
    """Create independent content by reflink when possible, otherwise copy."""
    if try_clone and try_reflink(source, destination):
        return "reflink", True
    shutil.copy2(source, destination)
    return "copy", False
