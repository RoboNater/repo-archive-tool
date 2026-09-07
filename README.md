# repo-archive-tool

`repo-archive-tool` is a host-independent command-line utility for creating and
maintaining verified local archives of remote Git repositories. It uses native
Git mirrors, records archive completeness explicitly, fetches and verifies Git
LFS content when needed, and reports submodule dependencies without confusing
them with complete submodule backups.

The current `0.1.0` development build supports safe mirror backup and update,
archive inspection, full, quick, or deep verification, manifests, operation
reports, LFS archival, submodule-awareness reporting, self-contained bundle
snapshots, and staged offline restore to working clones or recovered mirrors.

Early users are welcome to report workflow gaps, unclear output, and platform
issues in the [GitHub issue tracker](https://github.com/RoboNater/repo-archive-tool/issues).

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- Git available on `PATH`
- Git LFS available on `PATH` for complete archives of repositories that use
  LFS

The core archive workflow uses Git directly and does not require a GitHub,
GitLab, or other hosting-provider API.

## Install from a checkout

Install the command in an isolated, uv-managed tool environment:

```bash
uv tool install .
repo-archive --version
```

For contributor setup, use the locked development environment instead:

```bash
uv sync --dev
uv run repo-archive --help
```

## Quick start

Create an archive:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives
```

The default easy naming policy creates a predictable hosted path such as
`./archives/github.com/OWNER/REPO`; local remotes use
`./archives/local/REPO`. Human-readable output for archive operations begins
with the resolved `ARCHIVE:` path, and JSON output retains it in `archive_path`.

Use `--naming pedantic` when paths must encode the complete normalized remote
identity with a digest, or `--name owner-repo` for a custom single-directory
name. `--name` and `--naming` are mutually exclusive. Every policy refuses to
reuse an occupied archive for a different source. Easy naming reuses one
matching digest-named archive created by an older version instead of creating a
duplicate; it never renames the legacy archive automatically. See the
[usage guide](docs/usage.md#archive-naming-and-collisions) for collision and
identity details, the `--no-legacy-reuse` escape for ambiguous matches, hosted
path case behavior, and archive-boundary protection.

Then inspect, verify, and refresh the archive using that path:

```bash
repo-archive info <archive-path>
repo-archive verify <archive-path>
repo-archive update <archive-path>
repo-archive snapshot <archive-path>
repo-archive restore <archive-path> <destination>
```

`verify` performs full Git object, LFS, and snapshot checks by default.
Use `--quick` for structural and configuration checks that deliberately skip
those deeper integrity checks. Use `--deep`, optionally with `--full`, to
materialize each bundle and recompute its historical LFS requirements;
`--quick` and `--deep` cannot be combined. Add `--json` to any current
subcommand for a stable machine-readable result.
Verification is not read-only: every attempt writes the latest reports, and
every structurally valid complete or partial verification records its time,
mode, and outcome in `manifest.json`, so the archive set must remain writable.

See the [usage guide](docs/usage.md) for archive-path examples, all implemented
options, automation output, reports, exit codes, LFS behavior, submodule
details, and credential guidance.

## Safe updates and archive contents

New backups are cloned into a staging location, validated, and only then
published. Existing archives are cloned locally into staging, refreshed with
mirror fetch-and-prune semantics, and validated before replacement. A failed
refresh leaves the previously published mirror intact.

Each archive set has this layout:

```text
<archive-path>/
|-- mirror.git/          Native bare Git mirror and archived LFS objects
|-- manifest.json        Versioned last-successful archive state
|-- reports/
|   |-- latest.json      Structured result of the latest operation attempt
|   `-- latest.txt       Human-readable result of the latest operation attempt
|-- snapshots/
|   `-- <UTC-timestamp>/
|       |-- snapshot.bundle
|       |-- snapshot.json
|       `-- lfs/objects/ Snapshot-owned historical LFS payloads
`-- metadata/            Reserved for provider-specific exports
```

`manifest.json` records the normalized, credential-redacted source; creation
and update timestamps; Git ref state; and LFS, submodule, metadata, and
verification status. Reports are replaced atomically for each
archive-targeted attempt. A failed-attempt report does not alter the manifest's
last-successful timestamps.

The mirror remains an ordinary bare Git repository that can be inspected with
normal Git commands independently of this tool.

## Offline restore

Restore a normal working clone from the current mirror without contacting its
source remote:

```bash
repo-archive restore <archive-path> <destination>
```

Use `--snapshot <UTC-timestamp>` to restore from that immutable snapshot, or
add `--mirror` to either source mode to produce a recovered bare mirror suitable
for republishing. Push Git refs with `git push --mirror <replacement-remote>`;
if LFS is present, separately run `git lfs push --all <replacement-remote>`.
Restores seed verified local LFS payloads before running the local checkout,
stage beside the destination, and publish only after validation. Existing
destinations are never overwritten.

## Completeness and exit status

Operations aggregate independently reported components into four outcomes:

- `complete`: all requested, implemented work completed.
- `complete-with-warnings`: the core operation completed with a non-fatal
  limitation, such as detected submodules that were not recursively archived.
- `partial`: known data was omitted or unavailable, such as detected LFS
  content skipped with `--no-lfs`.
- `failed`: the requested operation did not complete successfully.

Complete and warning-only operations exit `0`; general failures exit `1`;
configuration failures exit `2`; partial archives exit `3`; verification
failures exit `4`; and recognized authentication or authorization failures
exit `5`. Automation should inspect the JSON component details and warnings as
well as the aggregate exit code.

## Current boundaries

- Git LFS: backup and update detect historical LFS use, run the equivalent of
  `git lfs fetch --all`, and verify expected local objects when Git LFS is
  installed. Missing tooling or objects produce a `partial` archive.
  `--no-lfs` intentionally accepts that partial state.
- Submodules: definitions and pinned commits are recorded across archived refs,
  but child repositories are not recursively archived. Detected submodules
  produce `complete-with-warnings`.
- Bundle snapshots: `snapshot` and `backup --bundle` create a timestamped,
  verified recovery subtree. An ordinary Git bundle never contains LFS
  payloads, so each snapshot separately owns every available historical LFS
  object reachable from its included refs. Missing objects produce a verified
  `partial` snapshot with exact unavailable OIDs. A ref-less repository has no
  bundleable content, so snapshot creation reports a warning without failing
  an otherwise successful backup. For a non-empty repository, a requested
  snapshot failure makes `backup --bundle` nonzero while retaining the valid
  published mirror. A present but corrupt LFS object blocks publication; run an
  LFS-enabled backup or update to refetch it. Manifest-to-disk snapshot-index
  drift is a verification warning, and the next successful snapshot rebuilds
  that convenience index from the self-describing published subtrees. If the
  index write fails after publication, the operation warns and names the
  published path rather than reporting the verified snapshot as failed.
  Unrecorded root sidecars are named as verification warnings but do not block
  restore. Unrecorded entries directly under `lfs/` and files under
  `lfs/objects` follow the same rule; missing or corrupt recorded payloads and
  collisions with required snapshot paths remain verification failures.
  Creation refuses to publish a staged snapshot with any verification warning.
- Restore: working clones and recovered mirrors can use either `mirror.git` or
  a selected snapshot. A partial source restores Git history and reports its
  exact LFS gap; missing Git LFS tooling leaves pointer files in a working clone
  and reports a partial checkout. Restored repositories retain the local
  recovery source as `origin`; explicitly set a new remote when ready.
- Provider metadata: issues, pull requests, releases, Actions data, settings,
  and similar host data are not exported. `backup --metadata github` is
  accepted but does no export and reports a deferred-work warning.
- Verbose diagnostics: `--verbose` is accepted but expanded diagnostic output
  is not implemented.
- Credentials: emitted results, manifests, reports, and exceptions redact
  supported secret forms. Credential-embedded source URLs are not currently
  supported by `update`; use Git credential helpers, SSH configuration, or an
  SSH agent instead of embedding secrets in the mirror's `origin` URL.

A deferred-option warning by itself results in `complete-with-warnings` and
exit `0`; partial or failed components still take precedence.

## Development

Use uv for environments, dependencies, package commands, and development tools:

```bash
uv sync --dev
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run repo-archive --help
```

Apply formatting with `uv run ruff format .`. Default tests use local temporary
Git repositories and do not require network access. Git LFS integration tests
run when Git LFS is installed and otherwise skip explicitly.

GitHub Actions dependencies in `.github/workflows` must be pinned to a full
40-character commit SHA. Keep the corresponding exact release tag in an
inline comment (for example, `owner/action@<commit-sha> # v1.2.3`) so reviews
remain readable while execution stays immutable. Dependabot checks these pins
monthly and groups routine GitHub Actions updates into a single pull request
to keep maintenance noise low.

## Project references

- [Detailed usage guide](docs/usage.md)
- [Product specification](repo-archive-tool-spec.md)
- [Snapshot and restore contract](specification-snapshot-for-bundled-snapshots-and-restore.md)
- [Implementation plan and roadmap](docs/dev-notes/implementation-plan.md)
- [MIT license](LICENSE)
- [Feedback and bug reports](https://github.com/RoboNater/repo-archive-tool/issues)
