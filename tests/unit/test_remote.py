"""Tests for remote parsing and archive path derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_archive.remote import derive_archive_path, normalize_remote


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("https://github.com/Owner/repo.git", "https://github.com/Owner/repo.git"),
        ("git@github.com:Owner/repo.git", "ssh://git@github.com/Owner/repo.git"),
        ("ssh://git@github.com/Owner/repo", "ssh://git@github.com/Owner/repo.git"),
    ],
)
def test_normalize_network_remote(value: str, canonical: str) -> None:
    remote = normalize_remote(value)

    assert remote.canonical_url == canonical
    assert remote.host == "github.com"
    assert remote.owner == "Owner"
    assert remote.repository == "repo"
    assert remote.is_local is False


def test_normalize_remote_preserves_port_and_ipv6_authority() -> None:
    remote = normalize_remote("https://[2001:db8::1]:8443/team/repo.git")

    assert remote.host == "2001:db8::1"
    assert remote.port == 8443
    assert remote.canonical_url == "https://[2001:db8::1]:8443/team/repo.git"


def test_normalize_local_remote_and_prevent_name_collisions(tmp_path: Path) -> None:
    first = normalize_remote(str(tmp_path / "one" / "repo.git"))
    second = normalize_remote(str(tmp_path / "two" / "repo.git"))

    assert first.is_local is True
    assert derive_archive_path(tmp_path / "archives", first) != derive_archive_path(
        tmp_path / "archives", second
    )


def test_name_override_is_a_single_safe_component(tmp_path: Path) -> None:
    remote = normalize_remote("https://example.test/team/repo.git")

    assert (
        derive_archive_path(tmp_path, remote, "my archive") == tmp_path / "my-archive"
    )
    with pytest.raises(ValueError, match="single"):
        derive_archive_path(tmp_path, remote, "../escape")


def test_archive_identity_disambiguates_lossy_names_and_ssh_users(
    tmp_path: Path,
) -> None:
    first = normalize_remote("ssh://alice@example.test/team/a+b.git")
    second = normalize_remote("ssh://bob@example.test/team/a=b.git")

    assert derive_archive_path(tmp_path, first) != derive_archive_path(tmp_path, second)


def test_remote_rejects_traversal_segments() -> None:
    with pytest.raises(ValueError, match="traversal"):
        normalize_remote("https://example.test/team/../repo.git")
