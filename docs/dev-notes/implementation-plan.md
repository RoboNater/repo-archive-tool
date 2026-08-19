# repo-archive-tool Implementation Plan

**Status:** In progress
**Last reviewed:** 2026-08-18
**Governing specification:** [`repo-archive-tool-spec.md`](../../repo-archive-tool-spec.md)

This is the working implementation plan for `repo-archive-tool`. Keep it aligned with the repository as design decisions are made and phases are completed. The specification defines product requirements; this document records the intended implementation sequence and current project state.

## Current State

- [x] Initial product specification exists.
- [x] MIT license selected.
- [x] Python project and development tooling bootstrapped.
- [x] Core archive MVP implemented.
- [x] LFS-aware archival implemented.
- [x] Submodule awareness and incompleteness reporting implemented.
- [x] Current-capability usage and project documentation completed.
- [ ] Bundle snapshots and offline restore implemented.

## Implementation Decisions

- Python 3.11 or newer.
- Python versions, virtual environments, dependencies, locking, packaging commands, and project scripts are managed with [uv](https://docs.astral.sh/uv/).
- Commit `pyproject.toml` and `uv.lock`; do not maintain parallel `requirements.txt` files unless a specific integration requires an exported file.
- Use the standard-library `argparse` module for the CLI so the installed tool has minimal runtime dependencies.
- Invoke the system `git` and conditional `git-lfs` executables through `subprocess`; do not substitute a Python Git implementation.
- Use `pytest` for tests and `ruff` for linting and formatting, both installed and invoked through uv.
- Keep generic Git archival independent from provider-specific metadata exporters.
- Favor correctness and recoverability over update speed. Existing valid archives must survive failed refresh attempts.
- Preserve remote URL authority (including IPv6 and non-default ports) separately
  from filesystem-safe archive-path components. Archive paths include a stable
  identity digest unless an explicit `--name` override is supplied.
- Detect LFS attributes and submodule definitions across all archived,
  tree-bearing refs without requiring a worktree. Keep LFS storage inside
  `mirror.git/lfs`, and carry the previous store into staged updates before
  fetching and verification.
- Scan the full reachable object-name list for nested historical
  `.gitattributes`. Stage existing immutable LFS objects with same-volume hard
  links and a normal-copy fallback; never promote a staged update when the
  previous LFS store could not be preserved.
- Batch submodule ref-to-tree and root `.gitmodules` resolution, cache parsed
  configuration blobs, and use path-limited tree queries for only the declared
  submodule paths. Preserve valid findings when historical definitions are
  incomplete, reporting those entries as warnings.
- Publish user documentation for the currently usable feature set before
  snapshot and restore work so early adopters can provide feedback. After that
  baseline is published, every implementation phase must update affected user
  documentation and automated tests in the same change.

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
- [x] Redact credentials and tokens from remote URLs, diagnostics, manifests, reports, and exceptions.
- [x] Normalize HTTPS, SSH/SCP, `file://`, and local-path remotes.
- [x] Derive stable archive locations while preventing traversal, unsafe names, and accidental collisions; support `--name` overrides.
- [x] Manage the archive-set layout: `mirror.git`, `manifest.json`, `reports`, `snapshots`, and `metadata`.
- [x] Define schema-versioned manifest models and atomic JSON/text writers.
- [x] Define structured component and operation results.
- [x] Aggregate component results into `complete`, `complete-with-warnings`, `partial`, or `failed`.
- [x] Define stable machine-readable output with fields for operation, archive path, outcome, components, warnings, errors, and exit code.
- [x] Implement the documented exit-code convention:

  - `0`: complete or complete with warnings
  - `1`: operation failed
  - `2`: invalid command or configuration
  - `3`: partial archive
  - `4`: verification failed
  - `5`: authentication or authorization failure

**Exit criterion:** Unit-tested primitives can safely resolve an archive, run a redacted command, persist a manifest/report atomically, and calculate an outcome.

**Completed 2026-08-15:** Shared infrastructure now provides shell-free Git and
Git LFS execution, safe normalized remote metadata, deterministic archive-set
paths, version-1 manifests and atomic report persistence. `OperationResult`
is the stable machine-readable result contract; current CLI `--json` output
uses this contract while archive subcommands are implemented in Phase 2.

## Phase 2: Core Mirror Backup and Update

- [x] Implement `backup <remote-url> --root <archive-root>`.
- [x] Create new archives with `git clone --mirror` into a temporary sibling path.
- [x] Implement repeatable backup/update behavior with mirror fetch and prune semantics.
- [x] Stage updates separately and promote them only after fetch and core validation succeed, preserving the previous valid mirror on failure.
- [x] Enumerate refs, branches, tags, symbolic `HEAD`, and the configured source remote.
- [x] Populate manifest source, archive, and Git fields.
- [x] Implement `update <archive-path>` using the same safe update path.
- [x] Add `--root`, `--name`, `--no-lfs`, `--bundle`, `--metadata`, `--json`, and `--verbose` plumbing as applicable.
- [x] Classify authentication, invalid-repository, and integrity failures when the underlying tools provide reliable evidence; other transport errors remain general failures pending stable Git diagnostics.

**Exit criterion:** A repository with multiple branches and tags can be archived and updated idempotently, deleted remote refs are pruned, and a failed update does not replace the last valid mirror.

**Completed 2026-08-15:** `backup` creates and validates a staged native Git
mirror before publication; existing mirrors are locally cloned, refreshed with
`remote update --prune`, validated with `git fsck --full`, and then promoted
with rollback protection. `update` follows the same path from the configured
`origin`. Both operations persist current source and Git manifest fields.
The future-facing bundle, metadata, and expanded-diagnostics flags are accepted
explicitly and report that their requested work is deferred rather than
silently being ignored; LFS option behavior is implemented in Phase 4. Windows
read-only Git objects are made writable
only while removing the retired promoted mirror using Python 3.11-compatible
`shutil.rmtree(..., onerror=...)` behavior. Existing archives reject a
different requested source before staging, preventing a reused `--name` from
silently replacing its mirror. Local Git integration tests cover branches,
lightweight and annotated tags, idempotent update, pruning, failed-update
preservation, source-identity protection, and Windows `file://` source
round-tripping. Source-identity failures retain the initiating `backup` or
`update` operation name in the stable machine-readable result.

## Phase 3: Manifests, Reports, Info, and Verification

- [x] Write schema-versioned `manifest.json` after successful archive state changes.
- [x] Generate atomic `reports/latest.json` and `reports/latest.txt` for every operation attempt that identifies an archive set.
- [x] Keep last-successful archive timestamps distinct from failed-attempt reporting.
- [x] Implement `info <archive-path>` with source, timestamps, refs, LFS, submodules, snapshots, metadata, and last-verification summaries.
- [x] Implement `verify <archive-path>` checks for:

  - repository existence and bare-repository structure;
  - source remote configuration;
  - ref enumeration;
  - manifest and path consistency;
  - object integrity through `git fsck --full`;
  - LFS completeness when applicable;
  - selected or all bundle snapshots when requested.

- [x] Define `--quick` as structural/configuration checks and `--full` as object, LFS, and snapshot verification; use full verification by default.
- [x] Ensure `--json` writes only the stable JSON result to stdout and routes diagnostics appropriately.

**Exit criterion:** Humans and automation can determine archive contents, integrity, completeness, and the last operation outcome without manually inspecting the mirror.

**Completed 2026-08-15:** Every archive-targeted backup, update, info, and
verification attempt atomically replaces `reports/latest.json` and
`reports/latest.txt`. Failed attempts update only those reports; manifest
archive timestamps remain successful-state timestamps. `info` summarizes the
manifest, source, refs, deferred-component statuses, bundle count, and last
successful verification. `verify` checks bare-repository structure, source and
manifest consistency, refs, and (by default) `git fsck --full` plus all present
bundle snapshots and current LFS completeness. Successful checks record
`last_verified_at` and mode in the
manifest. The CLI rewrites reports after applying command-level deferred-work
warnings so persisted reports exactly match the emitted result. `--quick` skips
object/LFS/bundle work; `--full` is the default.

## Phase 4: Git LFS and Submodule Awareness

- [x] Detect LFS use by inspecting tracked `.gitattributes` content across archived refs without requiring a worktree.
- [x] Detect whether `git-lfs` is installed.
- [x] Run the equivalent of `git lfs fetch --all` when LFS is detected and not explicitly disabled.
- [x] Verify expected LFS pointers and locally archived objects using installed Git LFS capabilities.
- [x] Mark missing tooling or objects as `partial` rather than silently succeeding.
- [x] Treat `--no-lfs` as an intentional partial archive when LFS is present.
- [x] Find and parse `.gitmodules` across relevant refs without requiring a worktree.
- [x] Record submodule paths, URLs, and referenced commits in the manifest/report.
- [x] Warn that the initial release does not recursively archive submodule repositories.

**Exit criterion:** LFS and submodule incompleteness is detected, recorded, surfaced to users, and reflected in exit codes.

**Completed 2026-08-16:** Backup and update inspect historical
`.gitattributes` blobs reachable from archived refs, preserve the prior
archive-local LFS store during staged refreshes, run `git lfs fetch --all`, and
enumerate and SHA-256 verify every object reported by `git lfs ls-files --all`.
Missing tooling, fetch/enumeration failures, missing or corrupt objects, and
intentional `--no-lfs` operation produce a persisted `partial` result and exit
code 3. Full verification rechecks LFS objects without fetching and retains an
intentional partial status until a successful LFS-enabled backup or update.
Submodule discovery reads `.gitmodules` and gitlinks from each tree-bearing ref,
records paths, redacted URLs, commits, and refs, and reports the lack of
recursive child-repository archival as `complete-with-warnings`. Unit and local
integration tests cover historical LFS detection, missing tooling, object
hashing, real LFS transfer when installed, submodule parsing, manifest state,
reports, and exit codes.

**Review hardening 2026-08-16:** Nested `.gitattributes` paths are included in
LFS detection, non-UTF-8 committed content is decoded safely, and staged
updates preserve an existing LFS payload even when later detection fails. A
failure to stage that payload now prevents promotion. Existing LFS files use
hard links where supported to avoid duplicating large stores. Submodule
inspection batches tree resolution, deduplicates identical trees and config
blobs, retains valid definitions when another historical entry is incomplete,
and distinguishes recoverable warnings from incomplete inspection. Both
`backup` and `update` accept `--no-lfs`. Regression tests cover each of these
review scenarios.

**Follow-up review hardening 2026-08-17:** LFS object discovery streams the
full reachable-object walk instead of buffering it in memory. Submodule
inspection batch-checks each unique root tree for `.gitmodules` and limits
gitlink inspection to paths declared by the parsed configuration; all archived
refs remain in scope so dependencies reachable only from non-branch refs are
not hidden.
Human-readable submodule summaries bound historical commit details while the
manifest retains the full list, and valueless config keys flow through the
normal incomplete-definition warning. When an LFS storage-copy failure blocks
promotion, reports retain their normal component shape and mark unpublished or
skipped work as partial rather than complete. Focused regression tests cover
the streaming runner, no-submodule fast path, bounded summaries, valueless
keys, and non-promotion reporting.

**Third review hardening 2026-08-18:** Backup, info, and verify share the same
bounded human-readable submodule summary while manifests retain complete
commit histories. Submodule gitlinks are read with path-limited `ls-tree`
queries rather than full recursive listings. A proposed `cat-file --batch`
lookup was not used because Git reports normal external gitlink targets as
missing when their commit objects are absent from the superproject; `ls-tree`
reads the pinned OIDs directly from the tree without requiring those objects.
Per-tree failures no longer discard successful discovery or inflate
`refs_inspected`, dead tree-listing state was removed, and streamed Git stdout
is redacted before callbacks receive it. Tests include an external-object
gitlink, mixed per-tree discovery outcomes, shared summary formatting, and
streamed-output redaction.

**Fourth review hardening 2026-08-18:** Credential redaction now bypasses its
regular-expression passes when the input contains none of the characters that
can introduce a supported secret form, keeping streamed reachable-object walks
fast without weakening the callback's redaction guarantee. Path-limited
submodule lookup still starts one `ls-tree` process per unique tree containing
`.gitmodules`, and very large declared-path sets remain subject to the Windows
command-line length limit. A future batching replacement must preserve gitlink
OIDs even when their target commits are correctly absent from the superproject;
the tested `cat-file --batch` approach does not, so this non-blocking residual
scalability work is deferred rather than trading away archive correctness.

## Phase 5: Current-Capability Documentation and User Feedback

Document the useful feature set that exists now rather than waiting for restore
support. Documentation in this phase must describe shipped behavior precisely
and identify deferred commands and options explicitly.

**Reprioritized 2026-08-18:** Current-capability documentation moved ahead of
snapshot and restore implementation so the working archive, verification, LFS,
and submodule-awareness features can receive early user feedback.

### Short usage guide

- [x] Create `docs/usage.md` with a task-oriented quick start for the currently
  implemented workflow:

  ```bash
  repo-archive backup https://github.com/OWNER/REPO.git --root ./archives
  repo-archive info <archive-path>
  repo-archive verify <archive-path>
  repo-archive update <archive-path>
  ```

- [x] Explain installation with uv, archive-path discovery, repeatable updates,
  quick and full verification, JSON automation, verbose output, reports, and
  exit codes.
- [x] Explain current LFS archival and verification behavior, `--no-lfs`
  partial archives, submodule detection without recursive archival, and
  credential-handling expectations.
- [x] Clearly label bundle creation, restore, recursive submodule archival, and
  provider metadata export as deferred. Document that accepted `--bundle` and
  `--metadata` requests currently report deferred work rather than performing
  those operations.

**Usage guide completed 2026-08-18:** `docs/usage.md` documents installation,
archive path derivation and overrides, staged repeatable updates, inspection,
quick and full verification, structured output, latest-attempt reports, exit
codes, LFS completeness, and non-recursive submodule discovery. It also labels
snapshot creation, restore, recursive submodule archival, provider metadata,
and expanded verbose diagnostics as deferred. Credential guidance recommends
Git-managed authentication and calls out that the mirror's native `origin`
configuration retains its source URL even though persisted results and emitted
diagnostics are redacted.

### Project README

- [x] Add the project purpose, current implementation status, and an invitation
  to provide early feedback through GitHub issues.
- [x] Document Git, conditional Git LFS, Python, and uv requirements.
- [x] Provide installation examples such as `uv tool install .` and the
  uv-managed contributor setup.
- [x] Include a concise current-command quick start for `backup`, `update`,
  `info`, and `verify`.
- [x] Explain archive layout, manifests, reports, completeness outcomes, and
  important implemented options.
- [x] Document current LFS, submodule, metadata, bundle, restore, and
  credential-handling limitations without implying deferred behavior exists.
- [x] Include uv-based development, lint, formatting, and test commands.
- [x] Link the specification, roadmap, usage guide, MIT license, and feedback
  channel.

**Phase 5 completed 2026-08-18:** The README now gives new users a concise
installation and four-command workflow, explains safe staged refreshes,
archive layout and completeness, and points to the task-oriented usage guide
for operational detail. Both documents distinguish current LFS and submodule
behavior from deferred snapshots, restore, recursive archival, provider
metadata, and expanded diagnostics. Requirements, uv-based contribution
commands, credential expectations, roadmap links, the MIT license, and the
GitHub issue feedback channel are included and aligned with the shipped CLI.

**Exit criterion:** A new user can install the tool, create or update an
archive, inspect and verify it, understand completeness and current
limitations, and provide feedback without reading source code or the full
specification.

## Phase 6: Bundle Snapshots and Restore

Resolve the snapshot and restore contracts before implementation, then deliver
them as separately reviewable snapshot and restore changes with tests and user
documentation updated alongside each change.

- [ ] Decide and document whether an immutable snapshot owns a snapshot-specific
  LFS payload or relies on the archive set's shared LFS store. State the
  resulting point-in-time and portability guarantees explicitly.
- [ ] Define the snapshot record format, including path, creation timestamp,
  verification outcome, included refs, and LFS relationship.
- [ ] Implement `snapshot <archive-path>` using a temporary bundle path.
- [ ] Create bundles containing all intended refs and verify them before atomic
  publication under a unique UTC timestamp.
- [ ] Make the existing `backup --bundle` option create a verified snapshot
  after a successful archive update, using the same snapshot implementation.
- [ ] Record snapshot paths and verification outcomes without weakening atomic
  manifest and report updates.
- [ ] State in all relevant output and user documentation that ordinary Git
  bundles do not contain LFS objects.
- [ ] Define restore CLI modes before coding, including a normal working clone,
  selection of a bundle snapshot, and a recovered mirror suitable for
  `git push --mirror`.
- [ ] Implement `restore <archive-path> <destination>` as an offline normal
  clone from `mirror.git`.
- [ ] Seed restored repositories with archived LFS objects and perform the
  supported local LFS checkout flow without requiring the source remote.
- [ ] Support restoration from a selected bundle.
- [ ] Support producing a recovered mirror suitable for `git push --mirror` to
  a replacement remote.
- [ ] Refuse existing or unsafe destination overwrites. Build restores in a
  temporary sibling and publish the destination only after clone, LFS seeding,
  checkout, and validation succeed; clean up failed staging safely.
- [ ] Add unit and integration coverage for bundle creation, verification,
  atomic publication, destination safety, offline mirror and bundle restores,
  recovered mirrors, and conditional LFS restoration.
- [ ] Update `README.md` and `docs/usage.md` in the same changes with the final
  snapshot and restore syntax, guarantees, examples, and limitations.

**Exit criterion:** A disconnected archive can produce a verified bundle, a
normal working clone, an LFS-aware checkout where applicable, and a mirror that
can be republished; the behavior is covered by automated tests and accurately
documented.

## Phase 7: Complete Lifecycle Validation and MVP Readiness

Treat this phase as an acceptance audit, not the point where testing or
documentation begins. Feature tests and documentation belong in the phase that
introduces each behavior.

### Existing coverage

- [x] Remote URL normalization and archive naming.
- [x] Manifest serialization and schema behavior.
- [x] Credential redaction.
- [x] Git/Git LFS command construction.
- [x] Status aggregation and exit-code mapping.
- [x] LFS detection.
- [x] `.gitmodules` parsing.
- [x] JSON result stability.
- [x] Mirror creation with multiple branches and lightweight/annotated tags.
- [x] Idempotent update and fetching new commits/refs.
- [x] Pruning deleted remote refs.
- [x] Full object verification.
- [x] Failed update preserving the prior usable archive.
- [x] CLI JSON output and exit codes for current commands.

### Final acceptance work

- [ ] Confirm automated coverage for bundle creation and verification.
- [ ] Confirm offline restoration from both a mirror and a selected bundle.
- [ ] Confirm recovered-mirror behavior suitable for `git push --mirror`.
- [ ] Confirm conditional LFS archive and restore tests run when Git LFS is
  installed and skip clearly otherwise.
- [ ] Keep network-dependent tests separate from the default suite.
- [ ] Exercise create -> update -> verify -> snapshot -> offline restore in one
  complete local integration workflow.
- [ ] Validate all README and usage examples against the installed CLI from a
  clean checkout and reconcile any stale behavior or limitations.

**Exit criterion:** Automated tests and verified documentation demonstrate the
required create, update, prune, verify, snapshot, and restore lifecycle on
supported operating systems.

## Post-MVP Phases

These are specified follow-on capabilities and must not compromise the
host-independent core. Each phase includes its automated coverage and updates
to `README.md` and `docs/usage.md`; documentation is part of the feature, not a
later cleanup phase.

### Recursive submodule archival

- [ ] Add an opt-in recursive mode.
- [ ] Resolve relative submodule URLs safely.
- [ ] Map parent paths and pinned commits to separate child archive sets.
- [ ] Aggregate completeness across the archive graph.
- [ ] Add unit and integration coverage for recursive discovery, URL
  resolution, child failures, and aggregate completeness.
- [ ] Update user documentation with recursive-mode syntax, archive layout,
  completeness behavior, and limitations.

### GitHub metadata exporter

- [ ] Add a provider-neutral metadata interface.
- [ ] Initially use `gh api` so authentication remains with GitHub CLI rather than the archive tool.
- [ ] Export inspectable JSON for repository data, issues/comments, pull requests/reviews/comments, releases, labels, and milestones.
- [ ] Record permissions, pagination, unavailable data, and per-resource completeness explicitly.
- [ ] Later add Actions artifacts, settings, discussions, Projects, and other accessible resources.
- [ ] Add deterministic exporter tests and keep live-network coverage separate
  from the default suite.
- [ ] Update user documentation with authentication, permissions, exported
  resources, completeness guarantees, and restore limitations.

### Multi-repository operation

- [ ] Add a versioned YAML configuration format.
- [ ] Add batch backup/update and per-repository result summaries.
- [ ] Add snapshot retention policies.
- [ ] Provide scheduler-friendly, noninteractive operation.
- [ ] Add unit and integration coverage for configuration validation, partial
  batch failures, retention, and noninteractive operation.
- [ ] Update user documentation with configuration examples, automation,
  retention behavior, aggregate exit status, and operational limitations.

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
11. The README and usage guide accurately document all shipped commands,
    completeness guarantees, external dependencies, and known limitations.

## Plan Maintenance

Whenever implementation work changes project state, update this file in the same change:

- Mark completed checklist items.
- Revise decisions or sequencing that no longer match the code.
- Add newly discovered work or risks instead of leaving them implicit.
- Update the review date when the plan is substantively checked.
- Keep README and usage-guide tasks synchronized with actual CLI behavior.
- Add or update affected automated tests and user documentation in the same
  phase and change that introduces or modifies behavior.
- Describe only shipped behavior as available; label accepted-but-deferred
  options and future commands explicitly.
