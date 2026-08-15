"""Remote normalization and stable, safe archive path derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlsplit

from repo_archive.security import redact_url

_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class Remote:
    """A parsed remote safe to persist in archive metadata."""

    original: str
    canonical_url: str
    host: str
    path: tuple[str, ...]
    is_local: bool

    @property
    def repository(self) -> str:
        """The repository name without a trailing .git suffix."""
        return self.path[-1]

    @property
    def owner(self) -> str | None:
        """The immediate owner/group, if the remote has one."""
        return self.path[-2] if len(self.path) > 1 else None


def normalize_remote(value: str) -> Remote:
    """Normalize HTTP(S), SSH/SCP, file, and local-path Git remotes."""
    value = value.strip()
    if not value:
        raise ValueError("Remote URL must not be empty.")

    parsed = urlsplit(value)
    scp_match = _SCP_REMOTE.match(value)
    if scp_match and not parsed.scheme and not _WINDOWS_PATH.match(value):
        host = scp_match.group("host").lower()
        path = _path_parts(scp_match.group("path"))
        return Remote(value, f"ssh://{host}/{'/'.join(path)}.git", host, path, False)

    if parsed.scheme in {"http", "https", "ssh"}:
        if not parsed.hostname:
            raise ValueError("Remote URL must include a host.")
        host = parsed.hostname.lower()
        if parsed.port:
            host = f"{host}-{parsed.port}"
        path = _path_parts(parsed.path)
        return Remote(
            value, f"{parsed.scheme}://{host}/{'/'.join(path)}.git", host, path, False
        )

    if parsed.scheme == "file":
        local_path = Path(unquote(parsed.path)).resolve()
    elif not parsed.scheme or _WINDOWS_PATH.match(value):
        local_path = Path(value).expanduser().resolve()
    else:
        raise ValueError(f"Unsupported remote scheme: {parsed.scheme}")

    repository = _strip_git_suffix(local_path.name)
    if not repository:
        raise ValueError("Local remote path must name a repository.")
    return Remote(value, local_path.as_uri(), "local", (repository,), True)


def derive_archive_path(root: Path, remote: Remote, name: str | None = None) -> Path:
    """Derive a deterministic archive-set path contained by *root*."""
    root = root.resolve()
    if name is not None:
        components = (_safe_component(name),)
    elif remote.is_local:
        digest = sha256(remote.canonical_url.encode()).hexdigest()[:12]
        components = ("local", digest, _safe_component(remote.repository))
    else:
        components = (_safe_component(remote.host), *map(_safe_component, remote.path))
    candidate = root.joinpath(*components)
    if root != candidate and root not in candidate.parents:
        raise ValueError("Archive path escapes the archive root.")
    return candidate


def display_remote(remote: Remote) -> str:
    """Return a source URL suitable for diagnostics and persisted metadata."""
    return redact_url(remote.canonical_url)


def _path_parts(raw_path: str) -> tuple[str, ...]:
    raw_parts = tuple(part for part in raw_path.split("/") if part)
    if any(part in {".", ".."} for part in raw_parts):
        raise ValueError("Remote URL must not contain traversal path segments.")
    parts = raw_parts
    if not parts:
        raise ValueError("Remote URL must include a repository path.")
    normalized = (*parts[:-1], _strip_git_suffix(parts[-1]))
    if not normalized[-1]:
        raise ValueError("Remote URL must name a repository.")
    return normalized


def _strip_git_suffix(name: str) -> str:
    return name[:-4] if name.lower().endswith(".git") else name


def _safe_component(value: str) -> str:
    if value in {"", ".", ".."} or "/" in value or "\\" in value:
        raise ValueError("Archive name must be a single non-empty path component.")
    normalized = _SAFE_COMPONENT.sub("-", value).strip(".-")
    if not normalized:
        raise ValueError("Archive path component has no safe characters.")
    return normalized
