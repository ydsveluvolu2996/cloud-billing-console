# Dashboard activation worker

Deploy this only after the reviewed application migration, generated database policies,
and the constrained IAM broker. It is a separately privileged runtime, never an option
inside `billing_web` or `billing_collector`.

1. Back up the database and existing host configuration. Apply migration 0021 using
   the administration DB identity and reinstall `deploy/database-roles.sql` as that owner.
2. Install the independently managed collector permissions boundary and broker using
   `../onboarding-broker/README.md`. Verify existing collection/SSM/backup permissions
   still work and that the collector cannot mutate IAM.
3. On the existing single EC2, run `python3 deploy/onboarding-worker/install.py
   --broker-arn arn:aws:lambda:REGION:HOSTING_ACCOUNT:function:CloudBillingOnboardingBroker`.
   The installer preserves the current exact allowlist, creates a root-only admin
   environment, and allows the native host's private address through PostgreSQL HBA
   using TLS and SCRAM. Credentials remain on disk and are never printed.
4. Add `ONBOARDING_BROKER_FUNCTION` with that same ARN to the production `.env` and
   recreate the web container. This is a function identifier, not AWS credentials.
5. Run the service once with the administration environment to check connectivity,
   then `systemctl enable --now cloud-billing-activation` and restart the collector
   to load the relocated read-only allowlist. Configure retained CloudWatch shipping
   for `/var/log/cloud-billing/activation/worker.log` with stream `activation`.
6. Verify real web/collector database identities and metadata denial. Submit a
   previously approved single-account connection from the MFA dashboard and confirm
   authorization, exact trust tests, discovery, cost/budget imports and completion.

The worker is a restricted root service because it must read the existing root-only
administration credential. Code, environment and secrets are read-only under systemd;
only the dedicated allowlist state and runtime/log directories are writable. The
collector has group read access to the allowlist, not write access. No web mount or
metadata rule is widened. Root-level host compromise remains outside these boundaries.

Ordinary releases stop/restart the activation worker alongside the native collector,
including rollback and health checks. Changes to the unit, installer, migration or
privileged SQL trigger the release maintenance guard and require a separate review.

For rollback, disable/stop `cloud-billing-activation`, remove the web function setting,
restore the prior application release, and keep the additive request table/audit records.
Do not revert the allowlist to an older copy after onboarding new customers: preserve
all approved entries. Retain the collector permissions boundary. Dedicated broker
policy entries and DynamoDB state are durable authorization history; removing customer
access requires the normal offboarding/revocation review, not deleting the whole policy.
