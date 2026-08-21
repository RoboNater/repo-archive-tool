# Bundled Snapshot and Restore Specification

**Status:** Phase 6 design contract

**Governing specification:** [`repo-archive-tool-spec.md`](repo-archive-tool-spec.md)

## Model

```text
mutable archive mirror
├── Git object database and refs
└── archive-wide LFS store
             │
             │ materialize; never reference as a restore dependency
             ▼
immutable snapshot subtree
├── snapshot.bundle
├── snapshot.json
└── lfs/objects/<aa>/<bb>/<oid>
```

## Immutable Snapshot Contract

An immutable snapshot owns all Git and Git LFS content required to restore its
included refs. Required LFS content is every distinct LFS object referenced by
any commit reachable from those refs, including historical commits, not only
the trees at ref tips.

A snapshot must not depend on the mutable mirror, its shared LFS store, or
another snapshot for restoration. Its timestamped directory is one
self-contained recovery unit: copying that subtree by any means that preserves
file content preserves the recovery unit.

Physical deduplication is permitted when every snapshot has its own directory
entries, independent lifetime, and no path reference to external content. LFS
objects are content-addressed and must never be modified in place. Published
snapshot payloads are treated as immutable.

If required LFS content was unavailable because of `--no-lfs`, unavailable
Git LFS tooling, or a failed or incomplete fetch, the snapshot may be published
as `partial`. It must name every unavailable OID. Such a snapshot restores its
Git history without the mirror and reports its exact LFS gap; the mirror is
never an implicit fallback.

## Invariants

1. **Reachability:** The bundle contains the included refs and their reachable
   Git history; every historical LFS pointer reachable from that history is
   accounted for.
2. **Portability:** Restoration requires only the selected snapshot subtree.
3. **Integrity:** Every present LFS payload re-hashes to its OID; every absent
   required OID is declared by a verified-partial snapshot.
4. **Immutability:** Published snapshot content is never changed in place.
5. **Publication:** A visible final snapshot is either verified complete or
   verified partial. Failed staging is not a snapshot.

## Implementation Requirements

### Layout and record

Use the following layout, replacing the flat bundle example in specification
section 6 under its layout-evolution allowance:

```text
snapshots/
└── <UTC-timestamp>/
    ├── snapshot.bundle
    ├── snapshot.json
    └── lfs/objects/<aa>/<bb>/<oid>
```

`snapshot.json` must be schema-versioned and record at least:

- creation timestamp, status (`complete` or `partial`), and verification result;
- bundle path and digest;
- included refs and object IDs;
- required, present, and unavailable LFS OIDs;
- LFS object counts and total logical bytes; and
- materialization method totals where available.

All recorded paths must be relative to the snapshot subtree.

### Staging and publication

- Stage in a temporary sibling such as
  `snapshots/.<UTC-timestamp>.tmp-<random>/` so materialization and publication
  occur on one volume.
- Create the bundle, materialize the LFS payload, write the record, and verify
  the complete staged subtree before publication.
- Publish with one same-volume rename to the unique timestamped destination.
- Update archive-level snapshot indexes, manifests, and reports atomically.
- Remove failed staging safely. Never replace or expose a final snapshot after
  a creation, materialization, or verification failure.

### LFS enumeration and materialization

- Determine required LFS OIDs from Git history independently of the `git-lfs`
  executable by parsing valid LFS pointer blobs reachable from included refs.
  Git LFS tooling may fetch payloads but is not authoritative for what should
  exist.
- For each available, verified object, attempt materialization in this order:
  **reflink → hard link → copy**.
- Failure of reflink or hard-link optimization is non-fatal when a later method
  succeeds. Falling back to copies affects space, not completeness.
- Failure to copy an object that the archive has is snapshot-creation failure,
  not a partial-LFS condition.
- Hard-linked content must not be mutated or have inode metadata changed merely
  to enforce snapshot read-only policy. Any future operation requiring mutation
  must first create independent storage.

### Verification and restore

Verification must:

- run `git bundle verify` and confirm the bundle refs match `snapshot.json`;
- independently recompute required historical LFS OIDs;
- SHA-256 hash every present LFS object rather than check presence alone; and
- confirm that the recorded present and unavailable sets exactly match the
  subtree.

The valid outcomes are `verified-complete`, `verified-partial`, and `failed`.
A declared partial snapshot passes verification only when all present content
is valid and its unavailable-OID set is exact. Unexpected missing, corrupt, or
unreadable content fails verification and prevents publication.

Restore from a complete snapshot must support an offline working clone and a
recovered mirror without external LFS content. Restore from a partial snapshot
must restore Git history offline, report the unavailable OIDs, and never claim
a complete LFS checkout.

### Space expectations

Snapshot creation transiently requires space for the staged bundle and payload
before staging is released. Without reflink or hard-link support, additional
space may approach the full logical snapshot size. Perform a best-effort
free-space preflight where practical; report space failures clearly and do not
publish the staging directory.

## User-Facing Documentation Requirements

Documentation for snapshots, restore, and retention must state that:

- ordinary Git bundles do not contain LFS payloads;
- a snapshot's logical size is approximately its full Git history plus every
  historical LFS object reachable from its included refs;
- physical storage may be lower through deduplication but can reach the full
  logical size per snapshot;
- copying with tools such as `cp -r`, `tar`, or `rsync` without hard-link
  preservation may expand deduplicated files but does not reduce completeness;
  and
- partial snapshots restore Git history independently while reporting their
  exact LFS gap.
