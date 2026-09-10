# Billing production releases

The existing **Application and security checks** workflow now supports an exact-commit production release. Application and PostgreSQL tests, the 100-customer synthetic workload, dependency/secret/image scans and release-executor tests must all pass before deployment. The web container and native collector come from the same commit and CI artifact.

Production: `582287676741`, `ap-south-1`. Web: `i-0cae3cd32c80de891`. Collector: `i-0bba5908e62ce509e`.

## One-time setup

1. Create GitHub environment `billing-production` and restrict deployments to branch `codex/billing-security-portfolio` (branch, not tag). This repository's current GitHub plan does not support required environment reviewers. The explicit workflow dispatch and exact SHA are the release approval; restrict repository write/admin membership accordingly. Do not present this as a two-person approval gate.
2. Dispatch `checks.yml` with `deploy=false`, an exact `expected_sha`, and a reason. The identity job prints only non-secret OIDC claims. Confirm the subject matches `bootstrap.py`. The identity step does not print the token or request AWS permissions.
3. With a temporary **hosting-account** administrative login, review `python deploy/github-release/bootstrap.py > release-plan.json`, then run `python deploy/github-release/bootstrap.py --apply --profile cloud-billing`. Do not commit local plans, AWS credentials or command output containing environment values. Bootstrap requires both hosts online in SSM, Python 3.12, AWS CLI, sufficient free disk and the existing verified runtime/database baseline. It creates a dedicated OIDC role and fixed SSM document, installs a root-owned release helper/config, and adds versioned artifact-read access to each hosting instance role. It does not modify customer roles, security groups, database policies or login users.
4. After successful bootstrap, set environment variable `BILLING_RELEASE_ROLE_ARN` to `arn:aws:iam::582287676741:role/CloudBillingGitHubRelease` in `billing-production`.
5. Dispatch the exact release with `deploy=true` and check the final deployment receipt. Changes to the root-owned helper itself require this reviewed bootstrap step again; ordinary application releases do not update the helper or need personal AWS login.

The workflow path must be registered on the default branch before GitHub permits dispatch. This repository already has `checks.yml` on its default branch. Always verify dispatch works on the release branch before claiming setup is complete.

## Release

```sh
gh workflow run checks.yml --repo ydsveluvolu2996/cloud-billing-console \
  --ref codex/billing-security-portfolio \
  -f deploy=true -f expected_sha=FULL_40_CHARACTER_APPROVED_SHA \
  -f reason='Describe the approved release'
```

The selected branch must still resolve to the supplied SHA. The release job checks repository ID, branch, event and SHA. It obtains a two-hour AWS session through GitHub OIDC; there are no stored AWS access keys. The role can upload encrypted objects under one artifact prefix, invoke one fixed SSM document on two specific instances, and read command results. It cannot invoke `AWS-RunShellScript`, modify IAM or read customer credentials. Environment branch restrictions are essential because the OIDC trust binds to the repository/environment identity.

CI packages tracked source, the scanned Docker images and offline Python wheels. The coordinator hashes the downloaded CI artifact, uploads versioned encrypted objects, and pins each object version/checksum in a manifest. Both hosts verify the manifest, target, checksums and compatibility before activation. The web host also verifies the image's CI configuration digest and layers before pinning its local image ID. The collector installs its dependencies into a new root-owned virtualenv using the offline wheelhouse.

The collector activates first, followed by the web app. Existing code, dependencies and web image settings are retained for rollback; obsolete runtime source files are removed. The web step runs the existing encrypted database backup before switching containers. Checks include the actual restricted PostgreSQL runtime, collector service, HTTPS health endpoint, anonymous login redirect, MFA login field and static assets. The public site's restricted ingress is preserved; HTTPS checks run locally through SSM with the real hostname and TLS verification.

A failed activation rolls back that host and the coordinator rolls back the other attempted host. A failed rollback marks the release `rollback_failed` and fails the run; it must be investigated before another release. The workflow's concurrency gate serializes releases and never cancels a running release for a newer push. Do not manually cancel a release while activation is running. An interrupted run's SSM command may continue; inspect its command IDs and host state before retrying. The host lock prevents concurrent mutation and an old release cannot roll back a newer one.

## Maintenance boundaries and recovery

Changes to migrations, generated database role SQL, user-administration SQL, compose configuration or collector service are rejected against the installed compatibility baseline. They require a separate reviewed maintenance deployment and baseline refresh. This pipeline never runs migrations, changes database roles, restarts PostgreSQL/Caddy, opens ingress or grants additional customer billing permissions.

Every attempt stores a `billing-deployment-SHA-ATTEMPT` GitHub artifact for 90 days with manifest coordinates, SSM command IDs and status. On each host, `/opt/cloud-billing/.deployment/github-release-current.json` identifies the active release. Root-only state and rollback copies live under `/opt/cloud-billing-releases/RELEASE_ID/`. Database backups continue through the existing backup process.

For an interrupted or failed release, use the exact manifest version and checksum in its receipt with the fixed SSM document's `status` or `rollback` action. Rollback never undoes database changes. Keep the active release and its previous runtime/venv until the next release is verified. This initial version deliberately does not prune rollback directories or S3 versions automatically; monitor free disk and remove reviewed, obsolete releases during maintenance. Staging refuses insufficient disk before any activation. CI artifacts expire after 3 days, but versioned S3 deployment artifacts remain available under the bucket's lifecycle policy.

## Validation

```sh
python -m unittest discover -s deploy/github-release/tests -v
```

Tests cover archive traversal/links/size, checksum rejection, schema compatibility, exact release identity, restoration after failed health checks, obsolete source removal, prevention of stale rollback, two-host rollback and the IAM/SSM permission boundary. GitHub additionally runs the full application tests and security scans. End-to-end AWS deployment is only verified when the release receipt reports both runtimes active and live functional checks pass.
