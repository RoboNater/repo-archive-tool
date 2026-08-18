# repo-archive-tool

`repo-archive-tool` is a command-line utility for creating and maintaining
locally restorable archives of remote Git repositories.

The project currently supports safe staged mirror backup and update, archive
information and verification, Git LFS detection/fetch/verification, and
submodule-awareness reporting. Bundle snapshots and offline restore are the
next planned core capabilities.

## Contributor setup

Requirements: Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and Git.
Git LFS is also required to produce complete archives of repositories that use
LFS.

```powershell
uv sync --dev
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run repo-archive --help
```

See the [project specification](repo-archive-tool-spec.md) and the
[implementation plan](docs/dev-notes/implementation-plan.md).
