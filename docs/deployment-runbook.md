> This is the historical two-EC2 deployment procedure. For the current user-requested single-EC2 topology, follow [single-EC2 operations](single-ec2-operations.md). Do not recreate the retired collector host or restore the old web profile on the combined host.

# Deployment runbook — separate authorization required

This build does not deploy, merge, activate connections, send notifications or invite customers. Apply these steps only during a separately authorized deployment. Keep the portal and live notifications disabled throughout the internal pilot.

## Preconditions and stop conditions

Require final-commit CI success, no unresolved critical/high findings, reviewed infrastructure change set, protected-backup restore evidence, individual administrator recovery access, approved region/retention, pilot CIDR/VPN and independent security review before external access. Customer-by-customer approval/trust/reconciliation evidence is a separate gate. Do not substitute development fixtures for approvals.

Inventory the actual stack, instance, attached volumes, database owner, schema version, instance profile, security groups, backup bucket and current release SHA. Preserve these exact identifiers and a rollback image digest in restricted release evidence. The historical release mentioned in the request is not an instruction to downgrade. The inspected latest main was `68e322ae2d4663a13fc2030bae7ffebca2ab6fb8`.

Before migration, run a duplicate External ID check with the administration identity: `SELECT external_id,count(*) FROM billing_billingsource GROUP BY external_id HAVING count(*)>1;`. Resolve any duplicates through customer-approved rotation; never silently replace identifiers. Migration 0019 requires the PostgreSQL `btree_gist` extension and rejects overlapping ownership intervals at the database boundary. Inspect and resolve any existing overlaps from approved evidence before migration; the migration never chooses an owner automatically. Confirm all existing saved reports, Cost rows, ownership intervals, Alliance records/service notes/snapshots, audit events and users are included in a validated backup. Additive migrations preserve them; historical Alliance audit snapshots are copied into protected customer revision records. Migration 0020 retains old bulk previews without an identity/scope binding; re-upload them before applying, rather than granting access by a reused username. Migrating back down would discard new security records and is not an approved rollback.

## Infrastructure and identities

1. Stop the existing collector/worker and remove its old cron fallback before changing identities. Put the web application into maintenance mode and pause queue processing. Back up the database and record the current revision counts/totals.
2. Review `deploy/infrastructure.yaml` as an update to the existing stack. Keep its original `Server`, volumes, address, bucket and `CollectorRole` resources. Reject a change set that unexpectedly replaces the data-bearing server/volume or changes the collector role principal. Retain-on-delete does not prove an update cannot replace a resource.
3. Supply the actual VPC, web/collector subnets in that VPC, approved x86 Ubuntu AMI, pilot CIDR and separate exact Secrets Manager ARNs. The new collector has no ingress and HTTPS egress plus TCP 5432 to the web security group. Put both hosts in the same availability zone to avoid unnecessary transfer fees. There is no NAT gateway in this proposal: the collector uses a public IPv4 address for outbound requests but accepts no public ingress.
4. The web EC2 instance gets `WebRole`, explicitly denying AssumeRole and IAM changes. The collector retains the original `CloudBillingCollector` principal. Set `ApprovedCustomerRoleArns` to an empty list until an authorized administrator approves exact roles. Never restore the old broad role-path wildcard. CloudFormation in this step manages **provider infrastructure only**; customers use manual IAM roles.
5. Permit web HTTPS only from the approved pilot CIDR/VPN. Port 80 remains available for HTTP redirects and ACME HTTP-01 renewal. There is no SSH ingress and no public database ingress. Caddy disables TLS-ALPN challenges so restricted 443 does not prevent HTTP-01 renewal. Verify actual issuance/renewal before rollout. Administration is through SSM; check the chosen AMI has a current SSM agent and outbound SSM connectivity.
6. Both EC2 profiles require IMDSv2 and hop limit 1. The web container drops capabilities, uses a non-root UID, a read-only filesystem and no host network. Its SDK metadata access is disabled. Collector secrets are on a different host. Verify from the real app container that metadata token/credential requests fail and that no collector files or environment credentials are present. A compromised privileged host remains outside the container boundary and requires incident response.

## Database and runtime setup

Use a separate administration login as schema owner. Existing PostgreSQL volumes **do not** create a new admin role merely because `POSTGRES_USER` changes in Compose. Create/transfer administration ownership during the maintenance window using the current database administrator; preserve the original volume and never run `docker compose down -v`.

Run migrations from the release checkout on an administration shell or temporary administration container with a dedicated private TLS connection and `RUNTIME_ROLE=admin`. The app container is deliberately unable to migrate. Apply `deploy/database-roles.sql` afterward as the schema administrator; it resets the two runtime roles to NOSUPERUSER/NOBYPASSRLS/NOCREATEROLE/NOCREATEDB/NOINHERIT, removes role memberships, and refuses runtime-owned tables. Set each role's distinct random password through a protected administrative session; do not include passwords in shell arguments or logs. Reapply policies after every schema change.

Configure `.deployment/pg_hba.conf` from `deploy/pg_hba.conf.example`: exact collector private IP, actual app Docker subnet and administration source only, SCRAM authentication and TLS. Replace every placeholder. `PRIVATE_DB_IP` must be the web host's private interface, never `0.0.0.0`. Use a CA-issued PostgreSQL server certificate with SANs for `db` (the Docker hostname) and the private DNS/IP used by collector/admin. Keep the private key readable only by the PostgreSQL UID (70 for this Alpine image), mode 0600. Mount only the CA certificate in the app.

The actual web login is `billing_web`, collector is `billing_collector`, and migrations use the separate owner. `DB_SSLMODE=verify-full` and `DB_SSLROOTCERT` are required in production. RLS derives customer scope from a transaction-local authenticated user ID; runtime roles never own tables. This protects against missing ORM filters, but arbitrary SQL/code execution in a trusted web process can spoof its context, so it is not a defense against total web-runtime compromise. See the independent-review threat model.

Stage web-only Secrets Manager JSON fields `django_secret` and `web_db_password` with `scripts/stage_runtime_secrets.py --runtime web --secret-arn APPROVED_ARN --region APPROVED_REGION --output .deployment/secrets --owner-uid 10001`. The collector's separate secret contains `collector_db_password` and `collector_django_secret`; stage it to `/etc/cloud-billing/secrets` with the actual collector UID. This script never logs values. Secrets must be at least 20 characters. Stage the administration database password separately as root in `.deployment/secrets/admin_db_password`; neither runtime receives it. Web files must be readable by UID 10001, not just root. These host directories stay mode 0700; Compose's root daemon bind-mounts only the named files.

Copy `.env.example` to an ignored deployment file and fill only non-secret host/region/profile/log-group values. The Compose web environment uses secret files. Configure optional OIDC with an explicit reviewed Compose override exposing the issuer, client ID, HTTPS authorization/token/JWKS endpoints and `OIDC_RP_CLIENT_SECRET_FILE` mount. Pre-provision exact issuer+subject pairs using the administration identity; never link by email. Local MFA remains required after SSO. Use maintained IdP key rotation and test nonce/audience/expiry with the actual IdP before enabling SSO.

On the collector host install Python 3.12, the locked requirements and a dedicated `billing-collector` OS user. Use `/etc/cloud-billing/collector.env` mode 0600, root-owned, with:

```ini
DEBUG=false
RUNTIME_ROLE=collector
DATABASE_RLS_ENABLED=true
DB_HOST=APPROVED_PRIVATE_DB_DNS
DB_NAME=billing
DB_USER=billing_collector
DB_PASSWORD_FILE=/etc/cloud-billing/secrets/collector_db_password
DB_SSLMODE=verify-full
DB_SSLROOTCERT=/etc/cloud-billing/postgres-ca.crt
SECRET_KEY_FILE=/etc/cloud-billing/secrets/collector_django_secret
AWS_REGION=ap-south-1
COLLECTOR_ROLE_ARN=EXACT_COLLECTOR_ROLE_ARN
COLLECTOR_ALLOWLIST_FILE=/etc/cloud-billing/approved-roles.json
EXTERNAL_PORTAL_ENABLED=false
LIVE_NOTIFICATIONS_ENABLED=false
```

Do not set `AWS_EC2_METADATA_DISABLED` on the collector host: its own instance profile supplies temporary credentials. Make the deployed allowlist root-owned/readable to the service, not writable by it. Install `deploy/collector.service`; its pre-start gate checks the actual restricted database role before it starts scheduling. Compose uses the same gate for web startup.

## Audit, backup and start order

Create the retained CloudWatch group from the stack. Set `AUDIT_LOG_GROUP` in Compose. The host Docker daemon sends app logs using the web host profile; the application receives no logging AWS credentials. Install a current CloudWatch agent on the collector/backup hosts using `deploy/cloudwatch-agent.json.example`, replacing the group name and including only the local files present on each host. Retention is configured by the stack, not the agent; runtime roles only append and cannot delete streams/groups or change retention. Verify audit arrival and denied deletion with the actual runtime identities. Alert on missing collector/log delivery through separately approved infrastructure monitoring.

Put the actual approved backup bucket name in `.deployment/backup-bucket` (root-owned, mode 0600), install the cron/logrotate files and make `deploy/backup.sh` executable. Backups use encrypted private S3 storage and TLS; the web host can write only the backups prefix and cannot read/delete backups. Recovery uses a separate authorized administration role. The example bucket lifecycle is 35 days for current backups plus 7 days for noncurrent versions; align this with every customer's approved policy before deployment. It is not a per-customer deletion mechanism.

Start database → apply migrations/policies → start web/Caddy → verify runtime/HTTPS/MFA/authorization → start collector with no unapproved roles → verify audit/backup → admit each approved customer. Existing users retain identities but receive no automatic customer grants. Create individual memberships using `security_admin grant`; grant portfolio access only to explicitly approved individual superusers using `security_admin portfolio`. Enroll MFA and verify recovery before retiring shared credentials from routine use. Keep a separately controlled emergency administrator identity.

Run `scripts/verify_pilot.py --stack ACTUAL_STACK --region APPROVED_REGION --output .deployment/pilot-checks.json` with authorized read-only review credentials. It checks configuration only and lists the remaining host/TLS/STS/backup checks. Also run `manage.py verify_runtime` as each actual database login and negative URL/export checks with two real test identities. Do not publish the private evidence file in Git.

## Patching, release and rollback

A release must use the final reviewed Git SHA and exact scanned image digest. CI has tests, migration/RLS checks, dependency/Bandit/secret scanning and critical/high image scanning. Pin the resolved deployment image digests in release evidence; the source image tags deliberately receive fresh patched images during CI. Rebuild and rescan for security updates; no critical/high exception is silently allowed. Branch-protection/required-review settings require repository-administrator configuration and were not changed by this build.

CI now builds all three deployment images and exercises the application against the patched PostgreSQL image before scanning. Download the successful push run's `billing-release-GIT_SHA` artifact, verify its SHA-256 manifest, and load the packaged images rather than rebuilding different images on the server. Set `BILLING_APP_IMAGE`, `BILLING_POSTGRES_IMAGE` and `BILLING_CADDY_IMAGE` to the recorded tags/digests and compare local image IDs with `images.json` before starting. The application uses patched Alpine packages; Caddy is rebuilt from the stable release with patched Go dependencies. The severity gate remains unchanged.

Docker's classic and containerd image stores can expose configuration and manifest digests as different image IDs. In that case, export the loaded images and verify that each raw configuration digest equals the CI image ID and every filesystem layer digest matches CI. Record both the verified configuration digest and the host manifest digest; never accept a tag match alone. Verify the GitHub artifact archive digest as well as its embedded image-bundle checksum.

The legacy `/admin/` entry point redirects approved portfolio administrators to Operations. Generic Django model administration, including third-party OTP device editing, is not exposed; individual provisioning and MFA recovery continue through the audited administration command.

Set `BILLING_DATABASE_SUBNET` to a private Docker subnet that does not overlap the VPC or existing Docker networks, and use the same subnet in PostgreSQL client rules. For an existing stack update, preserve the current web server's ImageId, subnet, volume mapping and original UserData unless a separate replacement/restart has been explicitly planned. Collector bootstrap converts Ubuntu package sources to HTTPS to match its restricted egress.

The database also joins a database-only ingress bridge: Docker does not publish ports for containers attached only to an internal network. The port still binds exclusively to the host's private interface, with AWS security-group ingress from the collector and exact TLS/SCRAM client rules. The web application's database path remains on the internal bridge. CI checks both the internal application path and a published database port.

For rollback, first stop the collector and freeze writes. Prefer rolling application code forward with a fix. If rolling back code, keep compatible additive schema and the separated IAM/database identities; do not restore a shared instance profile or bypass RLS/MFA to make old code run. If the previous image is incompatible, keep maintenance mode and restore the validated pre-release backup into a new isolated database/volume, verify manifests and point the application at it during an explicitly approved recovery. Preserve the failed database and account for data written since the backup. Never down-migrate security/history tables or overwrite the only production copy as a shortcut. Resume collection only after source configuration, ownership and reconciliation gates pass.
