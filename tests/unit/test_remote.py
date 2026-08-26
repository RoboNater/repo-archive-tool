"""Tests for remote parsing and archive path derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_archive.remote import (
    derive_archive_path,
    normalize_remote,
    same_repository_identity,
)


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


def test_easy_local_paths_are_readable_and_identity_checks_detect_collisions(
    tmp_path: Path,
) -> None:
    first = normalize_remote(str(tmp_path / "one" / "repo.git"))
    second = normalize_remote(str(tmp_path / "two" / "repo.git"))

    assert first.is_local is True
    assert derive_archive_path(tmp_path / "archives", first) == (
        tmp_path / "archives" / "local" / "repo"
    )
    assert derive_archive_path(tmp_path / "archives", first) == derive_archive_path(
        tmp_path / "archives", second
    )
    assert same_repository_identity(first, second) is False


def test_normalize_file_url_round_trips_a_local_remote(tmp_path: Path) -> None:
    remote_path = tmp_path / "repo.git"

    remote = normalize_remote(remote_path.as_uri())

    assert remote.is_local is True
    assert remote.canonical_url == remote_path.resolve().as_uri()


def test_name_override_is_a_single_safe_component(tmp_path: Path) -> None:
    remote = normalize_remote("https://example.test/team/repo.git")

    assert (
        derive_archive_path(tmp_path, remote, "my archive") == tmp_path / "my-archive"
    )
    with pytest.raises(ValueError, match="single"):
        derive_archive_path(tmp_path, remote, "../escape")


def test_easy_naming_collides_lossy_names_but_pedantic_naming_disambiguates(
    tmp_path: Path,
) -> None:
    first = normalize_remote("ssh://alice@example.test/team/a+b.git")
    second = normalize_remote("ssh://bob@example.test/team/a=b.git")

    assert derive_archive_path(tmp_path, first) == derive_archive_path(tmp_path, second)
    assert derive_archive_path(
        tmp_path, first, naming="pedantic"
    ) != derive_archive_path(tmp_path, second, naming="pedantic")
    assert same_repository_identity(first, second) is False


def test_easy_naming_ignores_transport_and_ssh_user_for_repository_identity(
    tmp_path: Path,
) -> None:
    https = normalize_remote("https://example.test/team/repo.git")
    alice = normalize_remote("ssh://alice@example.test/team/repo.git")
    bob = normalize_remote("bob@example.test:team/repo.git")

    expected = tmp_path / "example.test" / "team" / "repo"
    assert derive_archive_path(tmp_path, https) == expected
    assert derive_archive_path(tmp_path, alice) == expected
    assert derive_archive_path(tmp_path, bob) == expected
    assert same_repository_identity(https, alice) is True
    assert same_repository_identity(alice, bob) is True
    assert derive_archive_path(
        tmp_path, alice, naming="pedantic"
    ) != derive_archive_path(tmp_path, bob, naming="pedantic")


def test_repository_identity_retains_host_port_path_and_case() -> None:
    base = normalize_remote("https://example.test/team/repo.git")

    assert not same_repository_identity(
        base, normalize_remote("https://example.test:8443/team/repo.git")
    )
    assert not same_repository_identity(
        base, normalize_remote("https://example.test/other/repo.git")
    )
    assert not same_repository_identity(
        base, normalize_remote("https://example.test/team/Repo.git")
    )


def test_remote_rejects_traversal_segments() -> None:
    with pytest.raises(ValueError, match="traversal"):
        normalize_remote("https://example.test/team/../repo.git")
