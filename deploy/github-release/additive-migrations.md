# Automatic additive migrations

The installed release executor supports one deliberately small schema change per
release: a canonical Django billing migration adding one nullable `DateTimeField`
to the existing `SavedReport` model. `blank` may be a literal boolean. Defaults,
indexes, uniqueness, foreign keys, custom columns, extra Python statements and
other operation types are rejected. The migration must be the next numbered
migration and depend only on the installed billing leaf. All previous migration,
database-policy and service-configuration checksums remain unchanged.

Candidate migration Python is parsed with `ast`; it is never imported or executed
with administration credentials. The interpreter is `additive_migrations.py`,
installed beside `agent.py`. The executor pins its SHA-256 and checks its owner,
permissions and parent directories before loading it. The existing manifest's
executor digest therefore also pins this interpreter.

## Execution and recovery

Staging validates the declarative plan before loading a release. Activation runs
the existing encrypted off-server backup and writes a root-only journal at
`RELEASE_DIR/additive-migration.json`. The journal preserves the prior release
configuration and records the backup stamp, exact migration checksum and phase.

A short-lived named `psql` client uses the current database image, existing
internal network, existing admin address `172.30.50.254`, and read-only mounts of
the existing admin password and CA. No application source, Python dependencies,
AWS credentials or Docker socket is mounted into this client. TLS verification,
connection timeout, statement timeout and lock timeout are mandatory. Cleanup
targets only the exact client container; an unconfirmed cleanup blocks progress.

The installed SQL locks the saved-report and migration-record tables, checks the
complete installed billing migration history and existing owner, RLS, policies,
role attributes and grants, then adds the nullable column and its Django record
in one transaction. Existing saved-report values, counts and access controls
must compare equal before commit. A matching existing column and migration
record are verified on retry; an unrecorded or differently defined column fails.

Only after successful database verification does the executor copy the verified
migration into the current runtime and atomically extend the existing baseline.
The file and directory are synced before advancing the baseline. An interrupted
copy or a committed database change with a pending baseline can be resumed by
retrying the exact release coordinates. A completed journal must still match the
installed migration file and baseline.

The normal code backup happens after this expansion. An application rollback
therefore retains the nullable database column, its migration record and file.
Automatic rollback never drops a column or restores the whole database over
newer customer activity. Saved-report requests may briefly wait on the migration
table lock. Inspect an uncertain release/container state before retrying.

## One-time installation

Install the reviewed `agent.py` and `additive_migrations.py` as one root-owned pair
under `/usr/local/lib/cloud-billing-release`, holding the existing release lock.
Verify the new module against `ADDITIVE_MODULE_SHA256` before switching the pair;
retain a root-only rollback copy of both files. Preserve the installed release
configuration and its compatibility hashes. Do not derive a replacement live
baseline from a newer checkout or pre-add an unapplied migration.

This helper upgrade uses the existing administrative installation path once.
The GitHub release role, fixed SSM document and IAM permissions remain unchanged.
Subsequent supported additions run through the ordinary exact-artifact release
flow. Other schema or infrastructure changes remain separate reviewed maintenance.

## Dependency staging boundary

Native collector dependencies are built under UID/GID 65534, with supplementary
groups and capabilities cleared and a minimal environment. Each fixed command
runs in a transient systemd sandbox with a private network and temporary area,
read-only system/input paths, and write access only to the new virtualenv.
Candidate wheel startup code, including `.pth` files, never executes as root.

Every worker's control group must be stopped and empty before publication.
The output is checked without following links: symlinks, hardlinks, devices and
FIFOs are rejected. After validation, files become root-owned with ordinary
read/executable permissions and no setuid/setgid or writable group/other bits.
Interrupted dependency builds fail closed and require inspection or a new
release attempt; they are not recursively deleted by the executor.

## Validation

Run `python -m unittest discover -s deploy/github-release/tests -v`. Tests cover
declarative rejection, unchanged protected files, backup ordering, database and
baseline interruption recovery, code rollback retention, container cleanup and
adversarial dependency output. The workflow additionally exercises generated SQL
against disposable PostgreSQL with synthetic data and administration/runtime
roles. No test requires production database access.
