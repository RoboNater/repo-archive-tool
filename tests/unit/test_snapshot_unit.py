"""Unit tests for snapshot pointer parsing and record safety."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from repo_archive.snapshots import _materialize_lfs_payload, _parse_lfs_pointer


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
    assert (
        _parse_lfs_pointer(
            "version https://git-lfs.github.com/spec/v1\n"
            "size 42\n"
            f"oid sha256:{'a' * 64}\n"
        )
        is None
    )


def test_materialization_stops_retrying_an_unsupported_reflink(
    tmp_path: Path,
) -> None:
    mirror = tmp_path / "mirror.git"
    snapshot = tmp_path / "snapshot"
    oids = []
    for payload in (b"first", b"second"):
        oid = hashlib.sha256(payload).hexdigest()
        source = mirror / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
        oids.append(oid)

    with patch("repo_archive.snapshots.try_reflink", return_value=False) as reflink:
        record = _materialize_lfs_payload(mirror, snapshot, tuple(oids))

    assert reflink.call_count == 1
    assert record["present_object_count"] == 2
