# Usage guide

`repo-archive-tool` maintains a verified native Git mirror and describes its
completeness in a manifest and latest-operation reports. The currently shipped
workflow can create, update, inspect, verify, snapshot, and restore an archive.

## Requirements and installation

You need:

- Python 3.11 or newer;
- [uv](https://docs.astral.sh/uv/);
- Git available on `PATH`;
- Git LFS available on `PATH` when a source repository uses LFS and the archive
  must be complete.

From a checkout of this repository, install the command in a uv-managed tool
environment:

```bash
uv tool install .
repo-archive --version
```

For contributor setup instead, install the locked development environment and
run the command through uv:

```bash
uv sync --dev
uv run repo-archive --help
```

The examples below use the installed `repo-archive` command. Prefix them with
`uv run` when working from a contributor checkout without a tool installation.

## Create and find an archive

Create an archive under a chosen root directory:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives
```

The command creates a native bare mirror in a deterministic archive-set path.
Human-readable output for every archive-targeting operation begins with the
archive path:

```text
ARCHIVE: <resolved-archive-path>
```

For scripts, `--json` continues to report the same path in the stable
`archive_path` field:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives --json
```

### Archive naming and collisions

The default `easy` policy makes paths predictable. Hosted remotes retain their
complete repository path:

```text
<root>/<host>/<repository-path>/
```

Local remotes use `<root>/local/<repository>/`. The same default applies to
hosted and local sources even though local repository names collide more often.
An easy hosted identity consists of host, explicit port, and repository path;
transport and SSH user are access details, so HTTPS and SSH forms of the same
host/path update the same archive. A local identity is its complete resolved
filesystem path. Hosted repository paths remain case-sensitive because a
host-independent tool cannot assume that every Git server follows GitHub's
case-folding rules. Use consistent remote-path casing: on a case-sensitive
filesystem, case-only spellings create different easy paths; on a
case-insensitive filesystem, the second spelling produces a source-identity
collision instead of updating the first.

Filesystem-safe sanitization is lossy, and case-insensitive filesystems can
make otherwise distinct names share a path. Before updating any occupied path,
the tool compares source identity. A same-source backup updates normally; a
different source produces exit code 2 without staging or changing the existing
archive and suggests a custom name or pedantic naming.

Easy naming can otherwise make one repository path a prefix of another, such
as `group/repo` and `group/repo/sub`. Before staging, the tool refuses any path
that would put a new archive above or below an existing archive marked by
`manifest.json` or `mirror.git`. Use `--name` or pedantic naming to keep the
archive sets disjoint.

Pedantic naming preserves the previous digest-based behavior and incorporates
the complete normalized remote identity, including transport and SSH user:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives --naming pedantic
```

Hosted pedantic paths use
`<root>/<host>/<parent-path...>/<repository>--<identity-digest>/`; local paths
use `<root>/local/<identity-digest>/<repository>/`.

For a custom single-directory path, use `--name`:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives --name owner-repo
```

That archive is stored at `./archives/owner-repo`. An existing named archive
cannot be reused for a different source. `--name` and `--naming` are mutually
exclusive because a custom name replaces the derived policy.

When easy mode's destination does not exist, backup looks for digest-named
archives created by older releases. One matching logical source is reused in
place, including across HTTPS/SSH or SSH-user changes, so the changed default
does not create a second archive. Multiple matching legacy archives are
ambiguous and produce an error; pass the intended path directly to `update`.
Legacy directories are not renamed automatically. If the easy destination
already exists, it is authoritative and collision validation runs there. To
deliberately create or update the easy path without searching legacy archives,
use:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives --no-legacy-reuse
```

`--no-legacy-reuse`, `--name`, and `--naming` are mutually exclusive path
choices.

`info`, `verify`, `update`, `snapshot`, and `restore` continue to require an
explicit archive path; repository-identity and short-name lookup are not
implemented.

Backup stages and validates a new mirror before publishing it. Repeating the
same command safely refreshes the existing archive and prunes refs deleted from
the source. A failed refresh leaves the previously published mirror intact.

## Inspect, verify, and update

Once you know the archive-set path, use it for the remaining commands:

```bash
repo-archive info <archive-path>
repo-archive verify <archive-path>
repo-archive update <archive-path>
```

`info` reports the saved source, creation and update timestamps, ref counts,
LFS and submodule state, available bundle count, metadata state, and the last
recorded verification outcome. The current human-readable renderer omits component
names, so use `info <archive-path> --json` when you need to distinguish fields
such as LFS, submodules, and metadata unambiguously.

`verify` performs full verification by default. It checks archive structure,
the configured source, refs, manifest consistency, the Git object database with
`git fsck --full`, archived LFS objects when applicable, and any `.bundle`
self-contained snapshots under `snapshots/`:

```bash
repo-archive verify <archive-path> --full
```

Deep verification implies full verification, also materializes each bundle
into a temporary mirror, and independently recomputes every historical LFS OID
reachable from its refs. It may be combined with the explicit `--full` flag:

```bash
repo-archive verify <archive-path> --deep
repo-archive verify <archive-path> --full --deep
```

`--quick` and `--deep` are mutually exclusive because deep verification is the
slowest and most comprehensive mode.

Quick verification omits Git object, LFS object, and bundle verification. Use
it for a faster structural and configuration check, not as a substitute for
periodic full verification:

```bash
repo-archive verify <archive-path> --quick
```

Verification is not read-only. Every attempt writes `reports/latest.json` and
`reports/latest.txt`; every complete or declared-partial verification with no
failed integrity component also updates `last_verified_at`,
`last_verification_mode`, and `last_verification_outcome` in `manifest.json`.
The archive set must therefore be writable even when the mirror itself is only
being checked.

`update` reads the source from `mirror.git` and follows the same staged,
validated update process as a repeated `backup`:

```bash
repo-archive update <archive-path>
```

## Create a snapshot

Create a point-in-time recovery unit from the current mirror:

```bash
repo-archive snapshot <archive-path>
```

Or request one immediately after a successful backup/update of the mirror:

```bash
repo-archive backup https://github.com/OWNER/REPO.git --root ./archives --bundle
```

With `--bundle`, the snapshot is requested work: a non-empty repository whose
snapshot creation fails returns a nonzero overall result even though the
already validated mirror remains published and usable. Bundle-creation and
free-space failures use the general-failure exit code; detected integrity
failures use the verification-failure exit code.

Snapshot creation writes a temporary sibling, creates a bundle with all
archived refs, independently finds valid LFS pointers throughout the reachable
history, materializes available payloads, verifies the complete subtree, and
publishes it with one rename. A failed staging or verification attempt is
removed and never appears as a timestamped snapshot.

An archive with no refs has no bundleable Git content. `snapshot` and
`backup --bundle` report that condition as a warning and leave the valid empty
mirror archived without publishing a snapshot.

Each snapshot has this shape:

```text
snapshots/<UTC-timestamp>/
|-- snapshot.bundle
|-- snapshot.json
`-- lfs/objects/<aa>/<bb>/<oid>
```

An ordinary Git bundle does not contain LFS objects. `snapshot.json` records
the bundle digest and refs plus every required, present, and unavailable LFS
OID. A complete snapshot owns all Git and LFS content required to recover its
included refs without the mutable archive mirror. If the mirror lacks required
LFS content, creation still publishes a verified `partial` snapshot that owns
its Git history and reports the exact LFS gap; it never silently falls back to
the mirror during future recovery. A present object whose content does not
match its OID is corruption, not an unavailable payload, so it blocks snapshot
publication. Run an LFS-enabled `backup` or `update` to refetch and verify the
archive before retrying the snapshot.

A snapshot's logical size is approximately its full Git history plus every
historical LFS object reachable from its refs. Reflinks or hard links can lower
physical use, but storage can approach the full logical size for every
snapshot. Copying a snapshot with `cp -r`, `tar`, or `rsync` without preserving
hard links may expand deduplicated files but does not reduce completeness.
Creation also needs transient room for staging and deep verification; peak use
can include the bundle, LFS payload, and a temporary materialized copy of the
bundled Git history. A best-effort free-space preflight rejects clearly
insufficient staging space before payload materialization. The estimate probes
same-volume hard-link support and does not charge the logical payload size when
those links can be used; otherwise it conservatively assumes payload copies.

The manifest keeps a convenience index of published snapshots, but every
snapshot subtree remains self-describing. If a subtree is manually deleted for
retention, or publication completes just before an interrupted manifest write,
`verify` reports the index mismatch as a warning rather than declaring the
remaining snapshots corrupt. The next successful `snapshot` command rebuilds
the index from the published `snapshot.json` records. A malformed index shape
is still a verification failure. Only remove whole timestamped subtrees while
no archive operation is running. The subtree rename is the publication commit
point: if the later manifest-index write fails, `snapshot` returns
`complete-with-warnings` and names the published path rather than incorrectly
claiming that no snapshot was created. Rebuild warnings also name any existing
record that could not be read. Snapshot verification rejects files or
directories that collide with required paths or the recorded payload
inventory. Other unrecorded root entries, including operating-system sidecar
files, are named in a verification warning but do not make the recorded
recovery unit unrestorable. The same rule applies to unrecorded entries directly
under `lfs/` and unrecorded files under `lfs/objects`: they are named and
ignored, while every recorded payload must still exist and hash to its OID.
Snapshot creation treats any such warning in its tool-controlled staging tree
as a failure and does not publish it.

## Restore offline

Restore a normal working clone from the mutable archive mirror:

```bash
repo-archive restore <archive-path> <destination>
```

This reads only `mirror.git`; it does not contact the mirror's configured
source remote. To use an immutable recovery unit instead, select its UTC
directory name:

```bash
repo-archive restore <archive-path> <destination> --snapshot 2026-08-21T140000.000000Z
```

Snapshot selection accepts a single timestamp directory name under
`snapshots/`, never an arbitrary filesystem path. The selected subtree is the
only archived recovery data required: snapshot restore still works if the
mutable mirror and archive manifest are unavailable.

Unrecorded sidecar entries at the snapshot root, directly under `lfs/`, or
under `lfs/objects` are reported as restore-source warnings and ignored.
Restore still requires the recorded bundle, record, and LFS inventory to pass
their integrity checks.

If recovery media is read-only, restore still publishes a successfully
validated destination and returns a warning when it cannot update that
archive's `reports/latest.*` files.

Add `--mirror` to either command to create a recovered bare mirror:

```bash
repo-archive restore <archive-path> <destination.git> --mirror
repo-archive restore <archive-path> <destination.git> --snapshot 2026-08-21T140000.000000Z --mirror
git -C <destination.git> push --mirror <replacement-remote>
git -C <destination.git> lfs push --all <replacement-remote>
```

`git push --mirror` publishes Git refs and objects only; it does not upload LFS
payloads from a bare recovered mirror. When the archive contains LFS data, run
the separate `git lfs push --all` command with Git LFS installed.

Every restore refuses an existing destination. It clones into a temporary
sibling on the destination volume, copies verified LFS objects into the staged
repository, checks out a working tree locally when requested, runs Git object
validation, and publishes the destination with one rename. A failed restore
removes staging and leaves the destination absent.

For a working clone, Git LFS smudging is disabled during the Git checkout so it
cannot fetch from a remote. The tool seeds the local LFS store and then runs
`git lfs checkout`, which uses those local objects without downloading. Missing
Git LFS tooling or unavailable payloads produce a partial result and leave
pointer files where content cannot be materialized. A partial snapshot or
mirror still restores Git history and lists every unavailable historical OID
in the result. A recovered mirror carries the verified LFS store but needs no
working-tree checkout. Restore payload seeding uses reflinks where supported
and independent copies otherwise, so the writable destination never depends
on the continued lifetime of the archive.

The restored repository keeps the local mirror or bundle as `origin`. Change
it explicitly after recovery when the restored working clone should track a
new remote.

## Output and automation

Human output lists each component and ends with the aggregate result. Add
`--json` to any shipped subcommand for a single stable JSON object on standard
output:

```bash
repo-archive verify <archive-path> --json
```

The object includes `operation`, `archive_path`, `outcome`, `components`,
`warnings`, `errors`, and `exit_code`. `--json` can also appear before the
subcommand.

Every archive-targeted operation attempt atomically updates:

```text
<archive-path>/reports/latest.json
<archive-path>/reports/latest.txt
```

These files describe the most recent attempt, including a failed attempt. The
manifest keeps the last successfully published archive timestamps and the last
structurally valid verification timestamp, mode, and outcome. Failed integrity
verification does not replace those fields, while a valid declared-partial
verification records `partial` explicitly rather than masquerading as
complete.

The accepted `--verbose` flag is reserved for expanded diagnostics. It does not
currently make output more detailed. On `backup`, requesting it also produces
a warning that expanded diagnostics are not implemented.

### Exit codes and completeness

| Code | Meaning |
| ---: | --- |
| `0` | Complete, including `complete-with-warnings` |
| `1` | General operation failure |
| `2` | Invalid command, configuration, archive, or source identity |
| `3` | Partial archive with known omitted or unavailable data |
| `4` | Verification failure |
| `5` | Authentication or authorization failure recognized from Git diagnostics |

The aggregate outcomes have distinct meanings:

- `complete`: all requested, implemented components completed.
- `complete-with-warnings`: the core operation completed, but a non-fatal
  limitation or concern was reported. This still exits with code `0`.
- `partial`: known data was intentionally or unavoidably omitted. This exits
  with code `3`.
- `failed`: the requested operation did not complete successfully. The specific
  exit code identifies the recognized failure category.

Automation should inspect both the exit code and structured component results.

## Git LFS behavior

Backup and update inspect reachable historical `.gitattributes` files across
archived refs. When LFS use is detected and Git LFS is available, the tool runs
the equivalent of `git lfs fetch --all`, stores objects under
`mirror.git/lfs`, enumerates expected objects, and verifies their content
hashes.

If Git LFS is missing, fetching fails, or an expected object is missing or
corrupt, the archive is reported as `partial`. The Git mirror may still be
usable, but it is not a complete backup of the repository's LFS content.

Use `--no-lfs` on `backup` or `update` only when intentionally accepting a
partial archive:

```bash
repo-archive update <archive-path> --no-lfs
```

When LFS is detected, this records a partial LFS status and exits with code
`3`. A later full verification keeps that partial status even if the objects
currently present pass checks. Run a successful LFS-enabled `backup` or
`update` to refresh the manifest's completeness state.

## Submodule behavior

The tool discovers `.gitmodules` definitions and pinned gitlink commits across
archived refs without requiring a worktree. It records the full discovered
paths, redacted URLs, commits, refs, and inspection warnings in `manifest.json`.

The parent mirror contains submodule commit references, not complete copies of
the submodule repositories. Recursive submodule archival is not implemented.
Detected submodules therefore produce `complete-with-warnings`; archive each
submodule repository separately if it must be recoverable.

## Archive contents

A current archive set uses this layout:

```text
<archive-path>/
|-- mirror.git/          Native updateable bare Git mirror and LFS store
|-- manifest.json        Versioned last-successful archive state
|-- reports/
|   |-- latest.json      Structured result of the latest operation attempt
|   `-- latest.txt       Human-readable result of the latest operation attempt
|-- snapshots/
|   `-- <UTC-timestamp>/ Self-contained bundle, record, and LFS payload
`-- metadata/            Reserved for provider-specific exports
```

The mirror remains usable through ordinary Git commands. A timestamped
snapshot is published only after its record and payload verify. Do not
interpret the presence of the reserved `metadata/` directory as evidence that
provider data has been archived.

## Credentials and sensitive data

Let Git handle authentication through an SSH agent, SSH configuration, or a
credential helper. Avoid putting passwords or access tokens directly in a
remote URL. The tool redacts supported credential forms from manifests,
reports, emitted diagnostics, and exceptions. Credential-embedded source URLs
are not currently supported by `update`: internal command-result redaction
makes the stored URL unusable when it is read back for a later refresh.

Before sharing an entire archive set, inspect the mirror's configured URL:

```bash
git -C <archive-path>/mirror.git remote get-url origin
```

This raw Git command shows the URL exactly as Git recorded it, including any
embedded credentials. Treat its output as sensitive.

## Deferred capabilities

The following capabilities are planned and are not shipped yet:

- Recursively archiving submodule repositories.
- Exporting GitHub or other provider metadata. `backup --metadata github` is
  accepted but performs no export and reports a deferred-work warning.

A deferred-option warning by itself yields `complete-with-warnings` and exit
code `0`, but partial or failed components take precedence. A zero exit code
does not mean that the deferred request was performed. Check warnings and
component details as well as the exit code.
