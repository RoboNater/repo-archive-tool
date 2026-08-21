# repo-archive-tool — Project Specification

**Status:** Initial specification  
**Repository:** `repo-archive-tool`  
**Primary purpose:** Create and maintain complete, locally restorable archives of remote Git repositories, including normal Git refs/history, with optional host-specific metadata backup handled as a separate layer.

## 1. Purpose

`repo-archive-tool` is a command-line utility for creating, updating, verifying, and restoring local archival copies of remote Git repositories.

The core archive must preserve the Git repository itself as completely as practical: commits, branches, tags, refs, and Git objects. The core design must remain host-agnostic and work with GitHub, GitLab, self-hosted Git servers, or any other Git remote accessible through normal Git transport.

Git-host-specific information such as GitHub Issues, pull requests, discussions, repository settings, and Actions data is valuable but is **not part of the Git object database**. Those capabilities are therefore specified as optional, separate metadata-backup modules rather than part of the core Git archive.

## 2. Design Principles

1. **Use native Git storage and semantics.** The primary archive is a Git mirror created with `git clone --mirror`, not a custom serialization of repository history.
2. **Restorability over convenience.** A backup is not considered successful merely because cloning completed; the tool must support verification and a documented restore path.
3. **Host independence at the core.** Core Git backup must not depend on GitHub APIs or GitHub-specific tooling.
4. **Separate repository data from hosting-service metadata.** Issues, pull requests, CI data, settings, etc. are exported separately and never confused with the Git archive.
5. **No silent data loss.** Missing optional dependencies, inaccessible LFS objects, unreachable submodules, failed refs, or incomplete metadata exports must be reported explicitly.
6. **Idempotent updates.** Re-running backup/update operations should safely bring an existing archive up to date without creating unnecessary duplicate repositories.
7. **Scriptable operation.** Commands must have meaningful exit codes, machine-readable output where practical, and no mandatory interactive prompts.

## 3. Scope

### 3.1 Core Git archive — required

The tool must support:

- Creating a local mirror using `git clone --mirror`.
- Updating an existing mirror from its configured remote.
- Preserving:
  - commit history;
  - branches;
  - tags;
  - annotated tag objects;
  - remote refs available to the mirror;
  - other normal refs advertised/fetched under mirror semantics;
  - Git notes and other refs when exposed by the remote and included by mirror refspecs.
- Pruning refs deleted from the remote during an update, consistent with mirror behavior.
- Verifying repository object integrity.
- Reporting the refs present in the local archive.
- Restoring/cloning a normal working repository from the archive.
- Supporting republishing/restoring the mirror to another remote.

### 3.2 Git LFS — required when detected, if Git LFS is available

The tool must detect whether Git LFS is in use.

When LFS is detected, the tool must:

- clearly report that Git objects alone are insufficient for a complete archive;
- run an archival LFS fetch equivalent to `git lfs fetch --all`;
- preserve the resulting local LFS object store with the mirror;
- verify, as far as the installed Git LFS tooling permits, that expected LFS objects are locally available;
- report an incomplete backup if required LFS content could not be retrieved.

If Git LFS is not installed, the tool must not silently treat the archive as complete.

### 3.3 Submodules — required detection; recursive archival is a planned capability

The tool must inspect `.gitmodules` where present and identify referenced submodule repositories.

For the initial version:

- submodules must be detected and listed;
- the archive report must state that the parent mirror contains only the submodule commit references, not complete copies of the submodule repositories;
- the command must warn if submodule repositories have not also been archived.

A later phase should support recursive archival of submodule repositories into the same archive set.

### 3.4 Bundle snapshots — required

The tool must support generation of single-file Git bundle snapshots from a mirror.

Expected behavior:

- create a bundle containing all refs intended for archival;
- run `git bundle verify` after creation;
- timestamp or otherwise uniquely identify snapshots;
- retain bundle snapshots separately from the updateable mirror;
- explicitly state that LFS objects are not embedded in a normal Git bundle and therefore require separate archival.

### 3.5 Host-specific metadata — separate optional layer

Git-host metadata must be handled separately from the core archive.

Initial target: GitHub.

Potential GitHub metadata includes:

- Issues and comments;
- pull requests;
- pull-request review submissions and inline review comments;
- releases;
- labels;
- milestones;
- discussions;
- repository metadata and selected settings;
- branch protections/rulesets;
- collaborators/permissions where accessible;
- Actions workflow/run metadata;
- Actions artifacts where requested and still available;
- GitHub Projects where practical;
- wiki repository/data where applicable.

Metadata export formats should favor stable, inspectable files such as JSON plus downloaded binary assets where applicable.

A metadata export may be incomplete because of API permissions, API limitations, retention policies, deleted data, or unavailable artifacts. Completeness must be reported explicitly.

## 4. Non-Goals for Initial Release

The initial release does not need to:

- provide a graphical user interface;
- replace general-purpose filesystem backup software;
- guarantee recovery of data that the remote service no longer retains;
- reproduce an entire GitHub account or organization exactly;
- automatically recreate every GitHub setting on restore;
- archive arbitrary web pages associated with a repository;
- preserve build caches or ephemeral runner state;
- implement a custom Git object database.

## 5. Terminology

### Mirror

An updateable bare Git repository produced by:

```bash
git clone --mirror <remote-url> <archive-path>
```

This is the canonical local representation of the remote Git repository.

### Snapshot

An immutable or intentionally preserved point-in-time representation of an archive, typically a Git bundle plus associated manifest and, where applicable, separately archived LFS content and metadata.

### Archive set

All files belonging to one archived repository, including its mirror, manifests, logs/reports, optional bundle snapshots, optional LFS data, and optional host metadata.

## 6. Proposed Archive Layout

A backup root should use a predictable structure such as:

```text
archives/
└── github.com/
    └── OWNER/
        └── REPO/
            ├── mirror.git/
            ├── manifest.json
            ├── reports/
            │   ├── latest.json
            │   └── latest.txt
            ├── snapshots/
            │   └── 2026-08-15T105000Z.bundle
            └── metadata/
                └── github/
                    ├── repository.json
                    ├── issues/
                    ├── pull-requests/
                    ├── releases/
                    └── actions/
```

The exact on-disk layout may evolve, but:

- the mirror must remain a valid ordinary bare Git repository;
- optional metadata must not be stored inside the Git object database;
- manifests/reports must make archive completeness discoverable without opening the repository manually.

## 7. Manifest

Each archive set must have a machine-readable manifest.

Suggested fields:

```json
{
  "schema_version": 1,
  "source": {
    "url": "https://github.com/OWNER/REPO.git",
    "host": "github.com",
    "owner": "OWNER",
    "repository": "REPO"
  },
  "archive": {
    "created_at": "2026-08-15T10:50:00Z",
    "last_updated_at": "2026-08-15T10:50:00Z",
    "tool_version": "0.1.0"
  },
  "git": {
    "status": "complete",
    "mirror_path": "mirror.git",
    "ref_count": 0,
    "head": null
  },
  "lfs": {
    "detected": false,
    "status": "not-applicable"
  },
  "submodules": {
    "detected": false,
    "status": "not-applicable",
    "repositories": []
  },
  "metadata": {
    "github": {
      "requested": false,
      "status": "not-run"
    }
  }
}
```

The schema must be versioned from the first release.

## 8. CLI

Executable name:

```text
repo-archive
```

### 8.1 Create or update an archive

Preferred high-level operation:

```bash
repo-archive backup <remote-url> --root <archive-root>
```

Behavior:

- derive a stable archive location from the remote when possible;
- create a mirror if none exists;
- update the existing mirror if it already exists;
- fetch all LFS objects when LFS is detected and Git LFS is available;
- inspect submodules;
- verify the archive;
- update the manifest;
- return nonzero if the requested backup is incomplete or failed.

Useful options:

```text
--root PATH
--name NAME
--no-lfs
--bundle
--metadata github
--json
--verbose
```

`--no-lfs` must mean an intentional partial archive when LFS is present and must be reflected in status/reporting.

### 8.2 Update an existing archive

```bash
repo-archive update <archive-path>
```

Expected Git operation:

```bash
git -C <mirror.git> remote update --prune
```

The implementation may use equivalent native Git commands but must preserve mirror semantics.

### 8.3 Verify

```bash
repo-archive verify <archive-path>
```

Verification should include at minimum:

- repository exists and is recognized as a Git repository;
- object database integrity via an appropriate `git fsck` invocation;
- refs can be enumerated;
- mirror remote configuration is valid;
- LFS status if applicable;
- bundle verification for selected snapshots if requested;
- manifest consistency checks.

Example optional modes:

```text
--quick
--full
--json
```

### 8.4 Create bundle snapshot

```bash
repo-archive snapshot <archive-path>
```

Equivalent core operation:

```bash
git -C <mirror.git> bundle create <snapshot.bundle> --all
git bundle verify <snapshot.bundle>
```

### 8.5 Restore to a normal working clone

```bash
repo-archive restore <archive-path> <destination>
```

The restored repository should be created from the local mirror without requiring access to the original remote.

If LFS is present, restore behavior must document and implement how locally archived LFS objects are made available to the restored checkout.

### 8.6 Show archive information

```bash
repo-archive info <archive-path>
```

Report:

- original remote;
- archive creation/update timestamps;
- current refs/tags/branches summary;
- LFS status;
- submodule status;
- available snapshots;
- host metadata status;
- last verification result.

## 9. Core Git Operations

The preferred primitives are ordinary Git commands.

### Create

```bash
git clone --mirror <remote-url> <mirror-path>
```

### Update

```bash
git -C <mirror-path> remote update --prune
```

### Enumerate refs

```bash
git -C <mirror-path> show-ref
```

### Verify object database

```bash
git -C <mirror-path> fsck --full
```

### LFS archival fetch

```bash
git -C <mirror-path> lfs fetch --all
```

### Bundle

```bash
git -C <mirror-path> bundle create <bundle-path> --all
git bundle verify <bundle-path>
```

The application should invoke Git rather than reimplement Git protocols or object handling.

## 10. Completeness Model

A central feature of the tool is an explicit completeness status.

Suggested top-level outcomes:

- `complete` — all requested archive components completed successfully;
- `complete-with-warnings` — core requested data completed, but non-fatal concerns exist;
- `partial` — known data was intentionally or unavoidably omitted;
- `failed` — the requested archive operation did not produce a valid restorable core archive.

Examples:

- Git mirror successful, no LFS, no submodules: `complete`.
- Git mirror successful; submodules detected but recursive archival not requested/supported: `complete-with-warnings` or `partial`, depending on requested policy.
- LFS detected but Git LFS unavailable: `partial`.
- Git mirror update fails: `failed`.
- GitHub metadata requested but API permissions prevent issue export: core Git may remain complete, while overall requested operation is `partial`.

## 11. Error Handling

The tool must:

- preserve an existing valid archive if an update fails;
- never delete the existing mirror merely because a refresh failed;
- use temporary files followed by atomic rename where practical for manifests and snapshot creation;
- distinguish authentication/network failures from repository corruption;
- print actionable diagnostics;
- support noninteractive authentication mechanisms already understood by Git/SSH/credential helpers;
- avoid printing credentials, access tokens, or embedded secrets in logs/manifests.

## 12. Security

The tool must not store remote credentials in its own configuration unless explicitly designed and securely implemented in a later phase.

Preferred authentication:

- Git credential helpers;
- SSH agent/SSH configuration;
- GitHub CLI or environment/token mechanisms for optional GitHub metadata export.

Logs and manifests must redact:

- access tokens;
- passwords;
- credentials embedded in URLs;
- sensitive HTTP authorization headers.

## 13. Platform Requirements

Initial target platforms:

- Linux;
- Windows;
- WSL2;
- macOS where practical.

Required external dependency:

- Git.

Conditional dependency:

- Git LFS when repositories use Git LFS.

GitHub metadata support may use either:

- GitHub REST/GraphQL APIs directly; or
- the GitHub CLI (`gh`) as an implementation dependency.

The final choice should be made during implementation design.

## 14. Implementation Direction

A small Python CLI is a good initial implementation approach because it is portable, easy to test, and well suited to orchestrating external Git commands and JSON manifests.

Suggested baseline:

- Python 3.11+;
- `argparse`, `click`, or `typer` for CLI parsing;
- `subprocess` for invoking Git and Git LFS;
- structured internal result objects;
- JSON manifest/report output;
- `pytest` for tests;
- `ruff` for lint/format checks.

The implementation should **not** use a Python Git reimplementation as a substitute for the system `git` executable for core archival operations.

## 15. Configuration

The MVP can operate entirely from command-line arguments.

A later version may support a configuration file for scheduled multi-repository backups:

```yaml
archive_root: /srv/repo-archives

repositories:
  - url: https://github.com/OWNER/repo-a.git
    bundle: true
    metadata: github

  - url: git@example.internal:team/repo-b.git
    bundle: true
```

Configuration support should not be required for the first usable release.

## 16. Logging and Machine-Readable Output

Human-readable terminal output should be concise and indicate each major phase.

Example:

```text
[OK] Git mirror updated
[OK] 14 branches, 8 tags, 327 refs
[OK] Git fsck passed
[OK] No Git LFS usage detected
[WARN] 2 submodules detected; repositories not recursively archived
[OK] Manifest updated
RESULT: complete-with-warnings
```

For automation:

```bash
repo-archive backup ... --json
```

must emit a stable JSON result structure.

## 17. Exit Codes

Suggested initial convention:

```text
0  complete
1  operation failed
2  invalid command/configuration
3  archive completed only partially
4  verification failed
5  authentication/authorization failure
```

Exact codes may evolve before v1.0 but should be documented and tested.

## 18. Testing Requirements

### Unit tests

Cover:

- remote URL normalization;
- archive path derivation;
- manifest serialization/schema behavior;
- command construction;
- status/completeness aggregation;
- credential redaction;
- submodule parsing;
- LFS detection.

### Integration tests

Create local temporary Git repositories to test:

1. mirror creation;
2. multiple branches;
3. lightweight and annotated tags;
4. mirror update after new commits;
5. deleted remote branch pruning;
6. `git fsck`;
7. bundle creation and verification;
8. restoration from the mirror;
9. restoration from a bundle;
10. failed update without loss of the prior archive.

Where Git LFS is available, integration tests should also exercise LFS archival and restore behavior.

Network-dependent tests must be separable from the default test suite.

## 19. Initial Repository Structure

Suggested structure:

```text
repo-archive-tool/
├── README.md
├── LICENSE
├── pyproject.toml
├── docs/
│   └── spec.md
├── src/
│   └── repo_archive/
│       ├── __init__.py
│       ├── cli.py
│       ├── git.py
│       ├── archive.py
│       ├── manifest.py
│       ├── lfs.py
│       └── metadata/
│           └── github.py
└── tests/
    ├── unit/
    └── integration/
```

## 20. Development Phases

### Phase 0 — Repository/bootstrap

- create repository;
- add this specification;
- choose license;
- create Python package skeleton;
- configure tests/linting/CI.

### Phase 1 — Core mirror archive MVP

Implement:

- `backup`;
- mirror creation;
- mirror update/pruning;
- `info`;
- `verify`;
- manifest/report generation;
- restore to a normal clone.

**Phase 1 success criterion:** a repository with multiple branches and tags can be archived, disconnected from the original remote, and restored locally with history/refs intact.

### Phase 1.5 — LFS

Implement:

- LFS detection;
- `git lfs fetch --all`;
- completeness reporting;
- LFS-aware restore/verification tests.

### Phase 2 — Snapshots

Implement:

- bundle creation;
- verification;
- timestamped snapshot management;
- restore-from-bundle workflow.

### Phase 2.5 — Submodules

Implement:

- robust detection;
- recursive archival option;
- mapping of parent submodule URLs to separately archived repositories;
- completeness reporting across the archive set.

### Phase 3 — GitHub metadata

Implement a separate GitHub metadata exporter beginning with:

- repository metadata;
- issues/comments;
- pull requests/reviews/comments;
- releases;
- labels/milestones.

Later expand to settings, Actions, discussions, Projects, and other API-accessible resources.

### Phase 4 — Multi-repository operation

Add:

- config-file support;
- batch backup/update;
- per-repository result summaries;
- optional retention policies for bundle snapshots;
- scheduler-friendly operation.

## 21. Acceptance Criteria for MVP

The MVP is acceptable when all of the following are true:

1. Given a remote Git URL, one command creates a local mirror archive.
2. The archive contains all ordinary branches and tags advertised by the remote under mirror semantics.
3. Running backup again updates the same archive rather than creating a duplicate.
4. Deleted remote refs are pruned as expected.
5. The tool can verify the local Git object database.
6. The tool generates a versioned manifest containing source and archive status.
7. The tool can create a normal local clone using only the archive.
8. Known incompleteness caused by LFS or submodules is reported and never silently ignored.
9. The archive remains usable with ordinary Git commands independent of `repo-archive-tool`.
10. Automated integration tests demonstrate create → update → verify → restore behavior.

## 22. Key Architectural Boundary

The project should maintain a strict distinction between:

```text
Git repository archival
        |
        +-- mirror.git
        +-- refs / commits / tags / Git objects
        +-- optional LFS object storage
        +-- optional bundle snapshots

Hosting-service archival
        |
        +-- GitHub Issues / PRs / reviews
        +-- releases
        +-- Actions metadata/artifacts
        +-- settings / rules / permissions
        +-- other service-specific data
```

The first is the core product and must work for generic Git remotes.

The second is an extensible set of provider-specific modules and must never be required for the core Git archive to function.

## 23. Open Design Decisions

Snapshot LFS ownership is resolved by the
[bundled snapshot and restore specification](specification-snapshot-for-bundled-snapshots-and-restore.md):
each timestamped snapshot owns a self-contained, full-history LFS payload.

The following can be resolved during implementation without blocking the initial repository:

- final Python CLI framework (`argparse`, Click, or Typer);
- license;
- exact archive-root naming/URL normalization rules;
- direct GitHub API implementation versus `gh` for metadata;
- policy for whether detected-but-unarchived submodules produce `partial` or `complete-with-warnings`;
- retention policy syntax for old snapshots;
- whether provider metadata restoration is eventually in scope or export-only.

## 24. Initial Product Definition

The simplest useful description of `repo-archive-tool` is:

> A command-line tool that maintains verified local Git mirrors, including LFS-aware completeness checks and optional bundle snapshots, while treating GitHub/GitLab hosting metadata as a separate export layer.

That definition should remain the architectural center of the project as additional provider-specific capabilities are added.
