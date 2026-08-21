"""Unit tests for snapshot pointer parsing and record safety."""

from __future__ import annotations

from repo_archive.snapshots import _parse_lfs_pointer


def test_valid_lfs_pointer_is_parsed() -> None:
    oid = "a" * 64
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        "ext-1-example value\n"
        f"oid sha256:{oid}\n"
        "size 42\n"
    )

    assert _parse_lfs_pointer(pointer) == oid


def test_invalid_lfs_pointer_is_rejected() -> None:
    assert _parse_lfs_pointer("oid sha256:" + "a" * 64) is None
