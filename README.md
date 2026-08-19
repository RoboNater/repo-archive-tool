# repo-archive-tool

`repo-archive-tool` is a host-independent command-line utility for creating and
maintaining verified local archives of remote Git repositories. It uses native
Git mirrors, records archive completeness explicitly, fetches and verifies Git
LFS content when needed, and reports submodule dependencies without confusing
them with complete submodule backups.

The current `0.1.0` development build supports safe mirror backup and update,
archive inspection, full or quick verification, manifests, operation reports,
LFS archival, and submodule-awareness reporting. Bundle snapshot creation and
offline restore are the next planned core capabilities and are not implemented
yet.

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

The default archive path is derived from the normalized remote and includes an
identity digest. Use the `archive_path` field from `--json`, inspect the archive
root, or pass `--name owner-repo` when you want a predictable single-directory
name.

Then inspect, verify, and refresh the archive using that path:

```bash
repo-archive info <archive-path>
repo-archive verify <archive-path>
repo-archive update <archive-path>
```

`verify` performs full Git object, LFS, and existing-bundle checks by default.
Use `--quick` for structural and configuration checks that deliberately skip
those deeper integrity checks. Add `--json` to any current subcommand for a
stable machine-readable result.

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
|-- snapshots/           Reserved for bundle snapshots
`-- metadata/            Reserved for provider-specific exports
```

`manifest.json` records the normalized, credential-redacted source; creation
and update timestamps; Git ref state; and LFS, submodule, metadata, and
verification status. Reports are replaced atomically for each
archive-targeted attempt. A failed-attempt report does not alter the manifest's
last-successful timestamps.

The mirror remains an ordinary bare Git repository that can be inspected with
normal Git commands independently of this tool.

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
- Bundle snapshots: creation is not implemented. `backup --bundle` is accepted
  but does no snapshot work and reports a deferred-work warning.
- Restore: there is no `restore` subcommand yet. The archive mirror is native
  Git storage, but the supported offline working-clone, LFS seeding,
  restore-from-bundle, and recovered-mirror workflows remain planned work.
- Provider metadata: issues, pull requests, releases, Actions data, settings,
  and similar host data are not exported. `backup --metadata github` is
  accepted but does no export and reports a deferred-work warning.
- Verbose diagnostics: `--verbose` is accepted but expanded diagnostic output
  is not implemented.
- Credentials: emitted results, manifests, reports, and exceptions redact
  supported secret forms. The mirror's native `origin` configuration retains
  its source URL, so use Git credential helpers, SSH configuration, or an SSH
  agent instead of embedding secrets in that URL.

Deferred-option warnings result in `complete-with-warnings` and exit `0`; that
exit code does not mean the deferred feature ran.

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

## Project references

- [Detailed usage guide](docs/usage.md)
- [Product specification](repo-archive-tool-spec.md)
- [Implementation plan and roadmap](docs/dev-notes/implementation-plan.md)
- [MIT license](LICENSE)
- [Feedback and bug reports](https://github.com/RoboNater/repo-archive-tool/issues)
