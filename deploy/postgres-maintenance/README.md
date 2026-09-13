# PostgreSQL 18 maintenance

This is a separate maintenance deployment, not a normal GitHub release. Normal releases reject changes to compose, policy SQL, and migrations against their installed compatibility baseline. Keep that check. Refresh the baseline only after reviewed maintenance and runtime validation; never bypass it to force a release.

The tool never upgrades the PG17 files in place, removes a volume, or prunes images. It performs a logical globals/database dump and restore into an isolated PG18 container with a new named volume. Existing SCRAM password hashes, role attributes/memberships, owners, schema/table/function/default ACLs and RLS policies are compared using private source/target catalog snapshots. ACL comparison expands PostgreSQL implicit defaults and sorts grant entries, so an explicit owner-only default and its equivalent NULL ACL compare equally. All existing functions retain exact owner, ACL, security-definer and configuration metadata; removed functions or new application functions fail verification. New functions are permitted only as members of an existing extension whose version changes, with the recorded extension owner, default ACL, no security-definer privilege and no function configuration overrides. Extension names, owners and schemas remain identical; source and target versions are recorded in the receipt. Final verification also compares all public table counts and sequence values/called states.

## Preconditions

- Reviewed compose explicitly sets `PGDATA=/var/lib/postgresql/data` and its `pgdata` volume name to `${BILLING_POSTGRES_VOLUME:-cloud-billing_pgdata}`. PG18 must **never** mount the original PG17 volume. The default PG18 image data-directory layout differs; this deployment intentionally retains the explicit path on a new volume.
- Build and verify a PG18 image first. Supply an immutable repository digest or full `sha256:...` image ID loaded on the host. Retain the current PG17 image for rollback.
- Run from reviewed root maintenance access on the combined EC2 instance. The tool reads the existing administrator password file into the child process environment only, never command arguments. No credentials appear in normal output. Work files (including globals and private catalog snapshots containing role password hashes) are kept under a mode0700 `.deployment/postgres18-maintenance` directory, normally mode0600. The short-lived initialization password file is mode0444 inside that private directory so the isolated container PostgreSQL UID can read its read-only mount; it is removed after the restore container stops.
- At least `max(1GiB, 8 × database size)` disk free in Docker storage and backup storage, and384MiB available RAM. The isolated restore is capped at512MiB and one CPU. Do not overlap it with image builds on the small EC2 instance.
- The source must be PG17, with only the `billing` application database and no custom tablespaces. Extra databases/tablespaces need a separate reviewed plan.
- Confirm current encrypted off-host backup via existing `deploy/backup.sh`. Final frozen globals/dump/hash files are also uploaded by the tool to the existing `.deployment/backup-bucket`, under `backups/postgres18/TIMESTAMP/` using SSE-S3. No new bucket or destination is accepted. Failure to upload prevents cutover; writers stay stopped.
- Block normal release activation/rollback while the maintenance marker phase is `frozen`, `switching`, `awaiting-verification`, or `rolled-back`. Every command acquires `/run/lock/cloud-billing-release.lock`, but the lock is released between commands. A persistent marker guard is required in the release agent. Disable any other scheduled writer/restart automation for the maintenance window; the tool stops the app and native collector and rejects remaining client sessions rather than killing unknown sessions.

## Workflow

Use the same root, immutable image, and unique target volume for every command:

```sh
python3 deploy/postgres-maintenance/upgrade.py preflight \
  --image sha256:VERIFIED_64_HEX_IMAGE_ID --volume cloud-billing_pg18_YYYYMMDD
python3 deploy/postgres-maintenance/upgrade.py rehearse \
  --image sha256:VERIFIED_64_HEX_IMAGE_ID --volume cloud-billing_pg18_YYYYMMDD
```

The isolated target initializes as the source cluster owner recorded on `plpgsql`, with `POSTGRES_DB=postgres`. This preserves template databases and inherited extension ownership, including legacy owners. The globals restore removes exactly one `CREATE ROLE` statement for that initializer and preserves all source role attributes and SCRAM password hashes. Its `ALTER ROLE` statements run last under the restored `billing_admin`, preserving restrictions without interrupting the restore. Missing/duplicate declarations fail closed. No temporary login is introduced. Application database restoration runs as `billing_admin`.

The online rehearsal leaves production writers and volume unchanged. It dumps to private files and restores into `TARGET-rehearsal` with no network or published ports. Role/grant/RLS catalogs must match. Row counts are recorded as rehearsal evidence but are not compared to the changing live source. Rehearsal failures require inspection: the tool refuses to reuse any existing target volume or overwrite existing dump files. Do not delete the original PG17 volume to retry.

```sh
python3 deploy/postgres-maintenance/upgrade.py cutover \
  --image sha256:VERIFIED_64_HEX_IMAGE_ID --volume cloud-billing_pg18_YYYYMMDD
```

Cutover stops both writers, rejects other billing client sessions, takes **fresh final** globals and database dumps, uploads backups, restores into `TARGET`, and compares the catalog, table counts and sequences. The source administrator credentials and attributes replace the initialization password through the globals dump, and no temporary login remains to disable. Only after these gates does it save the original `.env` privately, update its image/volume settings, and recreate **only db** with the existing TLS/HBA/private-port controls. Application/collector writers remain stopped. The source volume and original image remain available.

Before reopening:

1. Verify both actual runtime identities, their lack of superuser/BYPASSRLS/ownership privileges, TLS certificate validation, and application policy checks against PG18. Use the existing web `verify_runtime` in a one-off `docker compose run --rm --no-deps app python manage.py verify_runtime`, and the existing native collector `systemd-run` environment/identity pattern documented in release bootstrap. Do not start Gunicorn or the collector as a verification shortcut.
2. Finish reviewed Python/Django/runtime/schema compatibility checks and any separately approved schema maintenance. The PG17 rollback described here is safe only while no production writes have been accepted by PG18. If schema migrations introduce writes, reason explicitly about rollback before applying them; the tool does not run migrations.
3. Refresh installed release compatibility baselines only after the reviewed compose/runtime changes and health gates match the intended release. Record exact image IDs and maintenance receipt. Keep normal release activation blocked until reopening succeeds.

```sh
python3 deploy/postgres-maintenance/upgrade.py reopen \
  --image sha256:VERIFIED_64_HEX_IMAGE_ID --volume cloud-billing_pg18_YYYYMMDD \
  --runtime-checks-passed
```

The explicit flag attests that the preceding checks passed; it does not run them. The irreversible rollback boundary is recorded **before** starting either writer. Verify web HTTPS/MFA, both services, collector publication and application logs immediately afterward. If a writer start fails, repair forward; the tool refuses automatic PG17 rollback because writes may already exist on PG18.

## Rollback before writers reopen

For `frozen`, `switching`, or `awaiting-verification` only:

```sh
python3 deploy/postgres-maintenance/upgrade.py rollback \
  --image sha256:VERIFIED_64_HEX_IMAGE_ID --volume cloud-billing_pg18_YYYYMMDD
```

The tool stops writers, restores original environment settings while explicitly pinning the saved PG17 image ID and original named volume, and recreates only db. It leaves writers stopped for PG17 TLS/role/runtime verification. Then use `reopen --runtime-checks-passed`. Do not run `docker compose down -v`, volume prune, or mount PG18 against the PG17 data.

After `writers-reopened`, reverting to the frozen PG17 volume would discard new data. A new recovery/migration plan is mandatory. Retain both old/rehearsal volumes and encrypted backups until a separately reviewed retention decision; this tool never removes them.

## Local validation

```sh
python3 -m unittest discover -s deploy/postgres-maintenance/tests -v
```

These tests exercise fail-closed phases, immutable new-volume protection, environment preservation, password transport and sequence verification. The isolated on-host restore rehearsal is the integration gate; unit tests alone do not prove database compatibility.

The initializer is discovered from the source `plpgsql` owner, preserving legacy
cluster ownership even when it differs from `billing_admin`. Its original
`ALTER ROLE` statements are applied last under the restored administrator, so
original login and privilege restrictions are preserved. The tool rejects
unsupported initializer identifiers and missing/duplicate role declarations.
