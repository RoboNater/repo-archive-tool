# repo-archive-tool

`repo-archive-tool` is a command-line utility for creating and maintaining
locally restorable archives of remote Git repositories.

The project is in its bootstrap phase. The command currently exposes only its
help text; archive operations will be added in the core MVP.

## Contributor setup

Requirements: Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and Git.

```powershell
uv sync --dev
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run repo-archive --help
```

See the [project specification](repo-archive-tool-spec.md) and the
[implementation plan](docs/dev-notes/implementation-plan.md).
