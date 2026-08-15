"""Credential-safe formatting for URLs and diagnostics."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)
_TOKEN_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<name>token|access_token|password|authorization)=(?P<value>[^\s&]+)"
)
_AUTHORIZATION = re.compile(r"(?im)^(authorization:\s*)(.+)$")


def redact_sensitive_text(value: str) -> str:
    """Remove credentials, tokens, and authorization values from *value*."""
    value = _URL_CREDENTIALS.sub(r"\g<scheme>***@", value)
    value = _TOKEN_ASSIGNMENT.sub(r"\g<name>=***", value)
    return _AUTHORIZATION.sub(r"\g<1>***", value)


def redact_url(url: str) -> str:
    """Return a display-safe remote URL without embedded user credentials."""
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return redact_sensitive_text(url)
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.scheme == "ssh" and parsed.username:
        host = f"{parsed.username}@{host}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
