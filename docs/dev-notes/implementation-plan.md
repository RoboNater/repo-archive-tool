# repo-archive-tool Implementation Plan

**Status:** In progress
**Last reviewed:** 2026-08-15  
**Governing specification:** [`repo-archive-tool-spec.md`](../../repo-archive-tool-spec.md)

This is the working implementation plan for `repo-archive-tool`. Keep it aligned with the repository as design decisions are made and phases are completed. The specification defines product requirements; this document records the intended implementation sequence and current project state.

## Current State

- [x] Initial product specification exists.
- [x] MIT license selected.
- [x] Python project and development tooling bootstrapped.
- [ ] Core archive MVP implemented.
- [ ] LFS-aware archival implemented.
- [ ] Bundle snapshots implemented.
- [ ] Usage and project documentation completed.

## Implementation Decisions

- Python 3.11 or newer.
- Python versions, virtual environments, dependencies, locking, packaging commands, and project scripts are managed with [uv](https://docs.astral.sh/uv/).
- Commit `pyproject.toml` and `uv.lock`; do not maintain parallel `requirements.txt` files unless a specific integration requires an exported file.
- Use the standard-library `argparse` module for the CLI so the installed tool has minimal runtime dependencies.
- Invoke the system `git` and conditional `git-lfs` executables through `subprocess`; do not substitute a Python Git implementation.
- Use `pytest` for tests and `ruff` for linting and formatting, both installed and invoked through uv.
- Keep generic Git archival independent from provider-specific metadata exporters.
- Favor correctness and recoverability over update speed. Existing valid archives must survive failed refresh attempts.

## Target Project Structure

```text
repo-archive-tool/
|-- AGENTS.md
|-- README.md
|-- LICENSE
|-- pyproject.toml
|-- uv.lock
|-- repo-archive-tool-spec.md
|-- docs/
|   |-- usage.md
|   `-- dev-notes/
|       `-- implementation-plan.md
|-- src/
|   `-- repo_archive/
|       |-- __init__.py
|       |-- cli.py
|       |-- archive.py
|       |-- git.py
|       |-- manifest.py
|       |-- reporting.py
|       |-- results.py
|       |-- security.py
|       |-- lfs.py
|       |-- submodules.py
|       |-- snapshots.py
|       |-- restore.py
|       `-- metadata/
|           |-- __init__.py
|           `-- github.py
`-- tests/
    |-- unit/
    `-- integration/
```

The exact module boundaries may be adjusted as code emerges, but Git operations, archive orchestration, persistence, and provider metadata should remain separable and independently testable.

## Phase 0: Project Bootstrap

- [x] Create `pyproject.toml` with package metadata, Python 3.11+ requirement, and a `repo-archive` console entry point.
- [x] Configure a uv-managed development dependency group containing `pytest` and `ruff`.
- [x] Generate and commit `uv.lock` with `uv lock`.
- [x] Create the `src/repo_archive` package and test directories.
- [x] Configure Ruff linting/formatting and pytest defaults in `pyproject.toml`.
- [x] Add CI for supported Python versions on Linux, Windows, and macOS using `uv sync --locked` followed by the same checks used locally.
- [x] Establish the standard local workflow:

  ```bash
  uv sync --dev
  uv run ruff format --check .
  uv run ruff check .
  uv run pytest
  uv run repo-archive --help
  ```

**Exit criterion:** A fresh checkout can be set up and all empty/skeleton checks run exclusively through uv.

**Completed 2026-08-15:** Bootstrap currently targets Python 3.11–3.13 in CI.
The only CLI behavior is `--help` and `--version`; archive subcommands begin in
the later phases.

## Phase 1: Shared Archive Infrastructure

- [x] Implement a safe subprocess wrapper for Git and Git LFS with structured stdout, stderr, and return-code capture.
- [ ] Redact credentials and tokens from remote URLs, diagnostics, manifests, reports, and exceptions.
- [ ] Normalize HTTPS, SSH/SCP, `file://`, and local-path remotes.
- [ ] Derive stable archive locations while preventing traversal, unsafe names, and accidental collisions; support `--name` overrides.
- [ ] Manage the archive-set layout: `mirror.git`, `manifest.json`, `reports`, `snapshots`, and `metadata`.
- [ ] Define schema-versioned manifest models and atomic JSON/text writers.
- [ ] Define structured component and operation results.
- [ ] Aggregate component results into `complete`, `complete-with-warnings`, `partial`, or `failed`.
- [ ] Define stable machine-readable output with fields for operation, archive path, outcome, components, warnings, errors, and exit code.
- [ ] Implement the documented exit-code convention:

  - `0`: complete or complete with warnings
  - `1`: operation failed
  - `2`: invalid command or configuration
  - `3`: partial archive
  - `4`: verification failed
  - `5`: authentication or authorization failure

**Exit criterion:** Unit-tested primitives can safely resolve an archive, run a redacted command, persist a manifest/report atomically, and calculate an outcome.

## Phase 2: Core Mirror Backup and Update

- [ ] Implement `backup <remote-url> --root <archive-root>`.
- [ ] Create new archives with `git clone --mirror` into a temporary sibling path.
- [ ] Implement repeatable backup/update behavior with mirror fetch and prune semantics.
- [ ] Stage updates separately and promote them only after fetch and core validation succeed, preserving the previous valid mirror on failure.
- [ ] Enumerate refs, branches, tags, symbolic `HEAD`, and the configured source remote.
- [ ] Populate manifest source, archive, and Git fields.
- [ ] Implement `update <archive-path>` using the same safe update path.
- [ ] Add `--root`, `--name`, `--no-lfs`, `--bundle`, `--metadata`, `--json`, and `--verbose` plumbing as applicable.
- [ ] Classify authentication, transport, invalid-repository, and integrity failures when the underlying tools provide reliable evidence.

**Exit criterion:** A repository with multiple branches and tags can be archived and updated idempotently, deleted remote refs are pruned, and a failed update does not replace the last valid mirror.

## Phase 3: Manifests, Reports, Info, and Verification

- [ ] Write schema-versioned `manifest.json` after successful archive state changes.
- [ ] Generate atomic `reports/latest.json` and `reports/latest.txt` for every operation attempt.
- [ ] Keep last-successful archive timestamps distinct from failed-attempt reporting.
- [ ] Implement `info <archive-path>` with source, timestamps, refs, LFS, submodules, snapshots, metadata, and last-verification summaries.
- [ ] Implement `verify <archive-path>` checks for:

  - repository existence and bare-repository structure;
  - source remote configuration;
  - ref enumeration;
  - manifest and path consistency;
  - object integrity through `git fsck --full`;
  - LFS completeness when applicable;
  - selected or all bundle snapshots when requested.

- [ ] Define `--quick` as structural/configuration checks and `--full` as object, LFS, and snapshot verification; use full verification by default.
- [ ] Ensure `--json` writes only the stable JSON result to stdout and routes diagnostics appropriately.

**Exit criterion:** Humans and automation can determine archive contents, integrity, completeness, and the last operation outcome without manually inspecting the mirror.

## Phase 4: Git LFS and Submodule Awareness

- [ ] Detect LFS use by inspecting tracked `.gitattributes` content across archived refs without requiring a worktree.
- [ ] Detect whether `git-lfs` is installed.
- [ ] Run the equivalent of `git lfs fetch --all` when LFS is detected and not explicitly disabled.
- [ ] Verify expected LFS pointers and locally archived objects using installed Git LFS capabilities.
- [ ] Mark missing tooling or objects as `partial` rather than silently succeeding.
- [ ] Treat `--no-lfs` as an intentional partial archive when LFS is present.
- [ ] Find and parse `.gitmodules` across relevant refs without requiring a worktree.
- [ ] Record submodule paths, URLs, and referenced commits in the manifest/report.
- [ ] Warn that the initial release does not recursively archive submodule repositories.

**Exit criterion:** LFS and submodule incompleteness is detected, recorded, surfaced to users, and reflected in exit codes.

## Phase 5: Bundle Snapshots and Restore

- [ ] Implement `snapshot <archive-path>` using a temporary bundle path.
- [ ] Create bundles containing all intended refs and verify them before atomic publication under a unique UTC timestamp.
- [ ] Record snapshot paths and verification outcomes.
- [ ] State in all relevant output that ordinary Git bundles do not contain LFS objects.
- [ ] Implement `restore <archive-path> <destination>` as an offline normal clone from `mirror.git`.
- [ ] Seed restored repositories with archived LFS objects and perform the supported local LFS checkout flow.
- [ ] Support restoration from a selected bundle.
- [ ] Support producing a recovered mirror suitable for `git push --mirror` to a replacement remote.
- [ ] Refuse unsafe destination overwrites unless a future explicit and well-tested policy is added.

**Exit criterion:** A disconnected archive can produce a normal working clone, an LFS-aware checkout where applicable, and a mirror that can be republished.

## Phase 6: Test the Complete Lifecycle

### Unit tests

- [ ] Remote URL normalization and archive naming.
- [ ] Manifest serialization and schema behavior.
- [ ] Credential redaction.
- [ ] Git/Git LFS command construction.
- [ ] Status aggregation and exit-code mapping.
- [ ] LFS detection.
- [ ] `.gitmodules` parsing.
- [ ] JSON result stability.

### Integration tests

- [ ] Mirror creation with multiple branches and lightweight/annotated tags.
- [ ] Idempotent update and fetching new commits/refs.
- [ ] Pruning deleted remote refs.
- [ ] Full object verification.
- [ ] Bundle creation and verification.
- [ ] Offline restoration from a mirror and a bundle.
- [ ] Failed update preserving the prior usable archive.
- [ ] CLI JSON output and exit codes.
- [ ] Conditional LFS archive and restore tests when Git LFS is installed.
- [ ] Separation of network-dependent tests from the default suite.

**Exit criterion:** Automated tests demonstrate the required create, update, prune, verify, snapshot, and restore lifecycle on supported operating systems.

## Phase 7: Usage Guide and README

### Short usage guide

- [ ] Create `docs/usage.md` with a task-oriented quick start:

  ```bash
  repo-archive backup https://github.com/OWNER/REPO.git --root ./archives
  repo-archive info ./archives/github.com/OWNER/REPO
  repo-archive verify ./archives/github.com/OWNER/REPO
  repo-archive snapshot ./archives/github.com/OWNER/REPO
  repo-archive restore ./archives/github.com/OWNER/REPO ./restored-repo
  ```

- [ ] Explain installation with uv, updating, JSON automation, exit codes, offline LFS restore, bundle limitations, submodule warnings, and mirror republishing.

### Project README

- [ ] Add the project purpose and implementation status.
- [ ] Document Git, conditional Git LFS, Python, and uv requirements.
- [ ] Provide installation examples such as `uv tool install .` and the uv-managed contributor setup.
- [ ] Include a concise five-command quick start.
- [ ] Explain archive layout and completeness outcomes.
- [ ] Summarize commands and important options.
- [ ] Provide restore and disaster-recovery examples.
- [ ] Document LFS, submodule, metadata, and credential-handling limitations.
- [ ] Include uv-based development, lint, formatting, and test commands.
- [ ] Link the specification, roadmap, usage guide, and MIT license.

**Exit criterion:** A new user can install, archive, inspect, verify, snapshot, and restore a repository without reading source code or the full specification.

## Post-MVP Phases

These are specified follow-on capabilities and must not compromise the host-independent core.

### Recursive submodule archival

- [ ] Add an opt-in recursive mode.
- [ ] Resolve relative submodule URLs safely.
- [ ] Map parent paths and pinned commits to separate child archive sets.
- [ ] Aggregate completeness across the archive graph.

### GitHub metadata exporter

- [ ] Add a provider-neutral metadata interface.
- [ ] Initially use `gh api` so authentication remains with GitHub CLI rather than the archive tool.
- [ ] Export inspectable JSON for repository data, issues/comments, pull requests/reviews/comments, releases, labels, and milestones.
- [ ] Record permissions, pagination, unavailable data, and per-resource completeness explicitly.
- [ ] Later add Actions artifacts, settings, discussions, Projects, and other accessible resources.

### Multi-repository operation

- [ ] Add a versioned YAML configuration format.
- [ ] Add batch backup/update and per-repository result summaries.
- [ ] Add snapshot retention policies.
- [ ] Provide scheduler-friendly, noninteractive operation.

## MVP Acceptance Gate

The MVP is complete only when all specification acceptance criteria are represented by automated tests and documentation. In particular:

1. One command creates a local mirror archive from a remote URL.
2. Ordinary advertised branches, tags, refs, and Git objects are retained.
3. Re-running backup safely updates the existing archive.
4. Deleted remote refs are pruned.
5. Object integrity can be verified.
6. A versioned manifest describes source and component completeness.
7. A normal repository can be restored using only local archive data.
8. LFS or submodule incompleteness is never silent.
9. The mirror remains usable through ordinary Git commands.
10. Integration tests prove create -> update -> verify -> snapshot -> offline restore behavior.

## Plan Maintenance

Whenever implementation work changes project state, update this file in the same change:

- Mark completed checklist items.
- Revise decisions or sequencing that no longer match the code.
- Add newly discovered work or risks instead of leaving them implicit.
- Update the review date when the plan is substantively checked.
- Keep README and usage-guide tasks synchronized with actual CLI behavior.
