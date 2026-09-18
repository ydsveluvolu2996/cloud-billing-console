# Single EC2 billing operations

The user requested consolidation onto the existing dashboard EC2 `i-0cae3cd32c80de891` in `ap-south-1`. Keep its Elastic IP `13.207.30.208`, PostgreSQL volume, HTTPS hostname, accounts, MFA, customer source IDs and `CloudBillingCollector` IAM principal. Web/Caddy/PostgreSQL stay in Docker. `cloud-billing-collector` is a separate, unprivileged native systemd service. This replaces the historical two-host topology; it is not equivalent host-level isolation.

## Boundaries

The single instance uses the existing collector instance profile. Its customer AssumeRole access remains an exact approved allowlist and customer External IDs are unchanged. The role also appends audit logs, reads versioned release artifacts and writes encrypted backups. Database administration credentials remain root-only; no runtime database role gains privileges. Existing web runtime files are preserved; materializing web secrets remains a separate administrative operation.

`deploy/single-ec2/metadata_guard.py` installs dedicated IPv4 and IPv6 filter chains without flushing unrelated firewall rules. The first OUTPUT jump allows metadata only to root and the numeric `billing-collector` UID. The first FORWARD jump blocks metadata for all forwarded traffic, including Docker containers. IMDSv2 and hop limit 1 remain mandatory. Install the root-owned executable at `/usr/local/sbin/cloud-billing-metadata-guard`; install the provided systemd unit and Docker/collector overrides. Docker reapplies the guard after restarting; collector startup and release health verify it. Do not grant the collector user sudo, Docker membership or interactive login.

The web container remains UID 10001, read-only, with all capabilities dropped, no host network/socket mounts and metadata SDK access disabled. Collector files under `/etc/cloud-billing` are not mounted into it. Direct metadata token and role-list requests must fail from the actual app container even when the SDK disable setting is bypassed. Root can access the host role and both runtimes: a privileged host compromise affects customer collection as well as the dashboard. Independent hosts provide stronger isolation if that becomes a requirement again.

Web and collector still authenticate as separate `billing_web` and `billing_collector` PostgreSQL roles over verified TLS with RLS. The database remains bound to the host's private interface. Remove the former collector's security-group ingress; permit only the local host source required by the native collector in `pg_hba.conf`. Confirm the actual collector client address in PostgreSQL before removing the old rule.

## Resources and collection

Use one worker thread initially (`WORKER_CONCURRENCY=1`). The service override gives the collector a 384 MB soft memory target, 512 MB hard cap, 75% of one CPU and lower scheduling priority. Keep the durable six-hour collection schedule; concurrency limits execution parallelism, not customer permissions. Observe runtime/queue age before adding customers. Existing synthetic 100-customer tests do not prove that this t3.small can serve the eventual complete production workload.

Collector logs use `/var/log/cloud-billing/collector/worker.log` in a collector-only directory and are appended to the existing retained CloudWatch group. Preserve web and backup log shipping. Keep root-owned files out of world-readable runtime paths.

## Migration and retirement

1. Inventory both hosts and save the live CloudFormation template, parameter values, role policies, source revision and runtime health in private deployment evidence. Prepare the collector and metadata guard on the existing web host while collection continues on the old host.
2. Review a CloudFormation change set that only updates the existing Server instance profile and adds the encrypted backup-write permission to the existing collector role. Reject replacement of the Server, address, volumes or role principal. Leave the old collector running until the new host passes database/STS checks.
3. Stage the existing collector secret directly from its exact Secrets Manager ARN into restricted files; never transmit values in SSM commands or logs. Reuse the existing CA, approved-role list and source configuration. Add the exact local PostgreSQL client address and reload HBA.
4. Verify web metadata denial, collector short-lived STS access to the approved customer, wrong External ID denial and actual web/collector database identities. Stop and disable the old worker, wait for its jobs to finish, then start the new worker. Verify a newly requested source sync finishes successfully on the new host and audit logs arrive.
5. Install the reviewed combined release helper/configuration and narrow GitHub's fixed SSM target to the remaining instance. Deploy an exact tested commit through GitHub OIDC. Confirm both runtime health checks, backup and rollback tests, TLS/MFA and a real collection before retiring the old host.
6. Remove CollectorServer and CollectorDatabaseIngress from the stack. The historical resource has Retain policies: removal alone does not stop it. After the cutover gates, explicitly terminate only the recorded old instance `i-0bba5908e62ce509e`. Its retained encrypted root volume is a recovery copy; account for its storage separately. Never delete the active dashboard volume. The unused old web IAM role/profile and collector security group may remain for recovery; they run no compute.

## Future releases

`deploy/github-release/release.py` has one `combined` target. The installed helper stages source, scanned images and native wheels once; backs up the database; stops the collector; updates source, web image and collector venv; and checks both runtime identities and metadata rules. Failure restores image, source and venv together. Old per-host release journals are rejected by the combined helper; recovering the earlier two-host topology is a separate maintenance operation. Infrastructure, database policy, metadata guard and service changes still require explicit maintenance; ordinary releases never change those controls.

Before any host restart, ensure both service enablement and Docker's metadata guard override are installed. After restart verify metadata denial, HTTPS, database TLS/RLS, collector scheduling, CloudWatch delivery and backup health. Keep the previous source/venv/image and root-only configuration evidence until recovery has been validated.

## Python runtime compatibility

The web image and the native collector have independent Python runtimes. CI runs the complete application, release-helper, database-policy, migration, worker and synthetic workload checks on both Python 3.12 and 3.14. Both matrix entries must pass before the exact-SHA production deployment can run. Each version publishes separate synthetic workload evidence; security scanning also tests the actual application image before packaging it.

The native Ubuntu collector intentionally remains on the host's Python 3.12. The security/package job must use Python 3.12: `package.py` downloads wheels for its running interpreter and verifies installation offline before creating the collector wheelhouse. Selecting Python 3.14 for that job would produce incompatible native wheels even if the container is healthy. Changing the container Python version does not upgrade `/usr/bin/python3`, the collector virtualenv, metadata guard or root-owned release helper. A native collector interpreter change requires a separate host maintenance plan, matching wheel packaging and rollback validation; do not upgrade the system interpreter as part of an application release.

## Activation service

Dashboard single-account onboarding now uses `cloud-billing-activation`, a separate
root-supervised administrative process with a protected environment. Its only writable
configuration is `/var/lib/cloud-billing-onboarding/approved-roles.json`; the collector
reads this file through its group. The native host has an exact PostgreSQL HBA entry
for TLS/SCRAM administration access. No administration credentials are mounted into
web/collector, and metadata rules remain unchanged. The private Lambda broker and
collector permissions boundary are installed once; routine onboarding never requires
a hosting account sign-in. See `deploy/onboarding-worker/README.md`.
