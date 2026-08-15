# Repository Agent Instructions

## Git Workflow

Unless the user directs otherwise, create a dedicated branch at the beginning
of work. When the task is complete, commit the changes and create a pull
request. Use the `codex/` branch prefix unless the user specifies another
naming convention.

## Implementation Plan

Before starting any implementation, testing, design, or documentation task, read both `repo-archive-tool-spec.md` and `docs/dev-notes/implementation-plan.md`.

Always check the implementation plan against the current repository state. Update the plan in the same change whenever completed work, new decisions, changed sequencing, discovered risks, CLI behavior, test coverage, or documentation makes any part of it stale. Mark completed checklist items and record material deviations; do not leave the plan describing work or behavior that no longer matches the repository.

Before finishing a task, check the plan again and make any necessary status or content updates.

## Python Workflow

Use uv to manage Python versions, environments, dependencies, lockfiles, package commands, and development tools. Run project commands through `uv run`, keep `uv.lock` current, and avoid introducing a parallel dependency-management workflow unless the repository has a documented integration need.
