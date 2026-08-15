"""Tests for credential-safe strings."""

from __future__ import annotations

from repo_archive.security import redact_sensitive_text, redact_url


def test_redact_url_removes_user_and_password() -> None:
    assert redact_url("https://user:secret@example.test/owner/repo.git") == (
        "https://example.test/owner/repo.git"
    )


def test_redact_sensitive_text_removes_common_secret_forms() -> None:
    value = "token=abc access_token=def password=ghi\nAuthorization: Bearer secret"

    assert redact_sensitive_text(value) == (
        "token=*** access_token=*** password=***\nAuthorization: ***"
    )


def test_redact_url_preserves_ssh_identity_and_ipv6_port() -> None:
    assert redact_url("ssh://alice@[2001:db8::1]:2222/team/repo.git") == (
        "ssh://alice@[2001:db8::1]:2222/team/repo.git"
    )
