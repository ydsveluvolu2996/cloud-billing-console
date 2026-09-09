# Recovery, rollback and customer offboarding

These are procedures for a separately authorized deployment. No production backup was downloaded, restored or deleted during this build.

## Backup and isolated restore

`deploy/backup.sh` makes a PostgreSQL custom-format dump, validates its catalog, records a SHA-256 digest, and uploads through TLS to the private encrypted backup prefix. It uses the separate administration database secret and write-only host backup permission. Never place database dumps or secrets in Git. Read/restore/delete permissions belong to a separately approved recovery identity. Monitor successful backup age and test recoverability, not merely file existence.

Before a release, record the actual source SHA, schema/migration versions, encrypted volume identifiers, backup object/version/checksum and authorization reference in restricted evidence. Agree RPO and RTO with the service owner. Proposed planning objectives are RPO at most 24 hours with daily backups, and RTO at most 2 hours; these are **not agreed or demonstrated production objectives**. Billing can often be recollected, but manual Alliance changes and identity records require backups. Define a tighter backup schedule if their allowed loss is smaller.

For an approved production backup drill, provision a new encrypted isolated database with no public access, no running collector, disabled notifications/portal and separate credentials. Download the approved backup using the recovery identity, verify its checksum and `pg_restore --list`, then restore with `--exit-on-error --no-owner --no-acl`. Apply runtime policies using the schema administrator. Validate constraints, important row counts and hashes, relationships, ownership intervals, negative/estimated historical facts, Alliance fields/notes/revisions/snapshots, saved reports, users, MFA and session records. Revoke restored sessions before permitting access. Test actual web/collector logins and TLS. Record elapsed time, errors, recovery point and disposal authorization; do not expose sensitive record contents in the evidence report.

The reproducible **local synthetic** drill is:

```sh
# Export DB_HOST/DB_PORT/DB_USER/DB_PASSWORD from protected local test configuration.
# Create an EMPTY billing_security_restore_* target first using the test administrator.
.venv/bin/python scripts/restore_drill.py \
  --source billing_security_checks --target billing_security_restore_new \
  --bin-dir /opt/homebrew/opt/postgresql@17/bin \
  --output docs/evidence/restore-drill.json
```

The tool rejects non-loopback hosts, the source as target and populated targets. Stop local writers while taking comparison manifests. It verifies complete canonical row digests, relative column order/types, constraints and indexes, canonicalizing PostgreSQL's equivalent text-array cast syntax and dropped-column ordinal gaps. It validates the dump catalog and writes counts/digests only. The recorded drill restored 182,306 bytes in 1.517 seconds, with all rows/schema matched and every constraint validated. This small synthetic recovery fixture is **not** a recovery-time measurement for the million-row scale database or production.

## Rotation and recovery

Stage application, database and optional IdP secrets only from each host's exact approved Secrets Manager ARN. Use separate web, collector and administration database passwords. Rotate with a maintenance window or a reviewed dual-credential procedure; update only the affected secret file, restart that runtime and verify its actual identity/TLS. Rotating the Django secret invalidates signed sessions and requires a planned login/recovery process. Revoke sessions after identity compromise using `security_admin`; rotate OIDC client secrets at both IdP and deployment, and verify federation. Never rotate a customer's External ID silently: it requires coordinated customer trust updates, a new connection version and fresh approval/trust evidence.

MFA recovery requires out-of-band identity verification and an audited administrator action, for example:

```sh
RUNTIME_ROLE=admin python manage.py security_admin recover-mfa \
  --username APPROVED_INDIVIDUAL --actor APPROVED_ADMIN --evidence TICKET_REFERENCE
```

This revokes sessions and forces enrollment. The command does not send recovery secrets through email. Keep an individually controlled emergency administration route tested before retiring routine shared credentials.

## Offboarding sequence

1. An authorized operator offboards the selected customer. The application disables its connections, increments versions, cancels queued/leased jobs, prevents late worker publication, clears customer cache payloads, closes current ownership intervals and revokes memberships/sessions. Historical facts and Alliance history remain retained. If other customers depend on a shared connection owned by this customer, the operation stops until custody is transferred through separately approved administration and reconciled. Do not break their connection as a shortcut.
2. An authorized administrator removes exact roles from both the provider AssumeRole policy and deployed collector allowlist. Coordinate customer-side trust revocation and record actual evidence. Database records alone do not edit IAM. Avoid removing a shared role while approved customers still depend on it.
3. Allow already-issued STS sessions to expire or use an explicitly authorized AWS session-revocation process. New sessions last 15 minutes; the offboarding record conservatively reserves one hour for credentials issued by the old runtime. Trust deletion alone does not invalidate all existing sessions immediately.
4. Review the customer's retention agreement, downloaded exports, cached reports, backups, audit retention and legal holds. `offboarding_control plan` lists exact database row counts. No retention period means deletion stays blocked. A shared backup cannot selectively erase one customer; expiry/disposal needs a documented approved process.
5. Record approved evidence using the separate administration login:

```sh
RUNTIME_ROLE=admin python manage.py offboarding_control plan --record RECORD_ID --actor ADMIN
RUNTIME_ROLE=admin python manage.py offboarding_control record-allowlist-removal --record RECORD_ID --actor ADMIN --evidence IAM_CHANGE_REFERENCE
RUNTIME_ROLE=admin python manage.py offboarding_control record-trust-revocation --record RECORD_ID --actor ADMIN --evidence CUSTOMER_REFERENCE
RUNTIME_ROLE=admin python manage.py offboarding_control approve-deletion --record RECORD_ID --actor ADMIN --evidence RETENTION_APPROVAL
# Only after the session window, retention period and external disposition gates:
RUNTIME_ROLE=admin python manage.py offboarding_control purge --record RECORD_ID --actor ADMIN --evidence APPROVED_ACTION --external-evidence EXPORT_BACKUP_DISPOSITION
```

The purge is audited, transactional and idempotent. It deletes the offboarded customer's financial facts, reports, Alliance revisions/notes/snapshots, reconciliation results and budget results, and invalidates relevant previews/aggregate caches. It retains identity/ownership tombstones, approval references and protected audit records. It does not delete neighboring customers' facts or erase external downloads/backups. No real purge was executed during this build.

## Rollback

Stop collectors and freeze writes first. Keep separated runtime identities, RLS and MFA. Prefer a forward fix; compatible additive schema can remain when reverting application code. Never down-migrate away security/history records or restore the old broad role policy. If incompatible, restore a verified pre-release backup to a new database/volume, validate it, preserve the failed database and account for post-backup writes before an approved cutover. The full deployment order and stop conditions are in [deployment-runbook.md](deployment-runbook.md).
