# Billing production releases

The **Application and security checks** workflow automatically releases pushes to `codex/billing-security-portfolio`. Application and PostgreSQL tests, the 100-customer synthetic workload, dependency/secret/image scans and release-executor tests must all pass before deployment. The web container and native collector come from the same tested commit and CI artifact. Manual dispatch remains available with an exact commit SHA.

Production: `582287676741`, `ap-south-1`. Combined web and collector: `i-0cae3cd32c80de891`.

## One-time setup

1. Create GitHub environment `billing-production` and restrict deployments to branch `codex/billing-security-portfolio` (branch, not tag). This repository's current GitHub plan does not support required environment reviewers. Pushing to this branch authorizes an automatic release after checks pass; restrict branch write access and repository write/admin membership accordingly. Manual releases require explicit dispatch with the exact SHA. Do not present either path as a two-person approval gate.
2. Dispatch `checks.yml` with `deploy=false`, an exact `expected_sha`, and a reason. The identity job prints only non-secret OIDC claims. Confirm the subject matches `bootstrap.py`. The identity step does not print the token or request AWS permissions.
3. With a temporary **hosting-account** administrative login, review `python deploy/github-release/bootstrap.py > release-plan.json`, then run `python deploy/github-release/bootstrap.py --apply --profile cloud-billing`. The administrative Python environment needs `boto3[crt]` when using `aws login`. Do not commit local plans, AWS credentials or command output containing environment values. Bootstrap requires the combined host online in SSM, Python 3.12, AWS CLI, sufficient free disk and the existing verified runtime/database baseline. Install missing AWS CLI using the AWS signed installer with signature verification before bootstrap. It creates a dedicated OIDC role and fixed SSM document, installs a root-owned release helper/config, and adds versioned artifact-read access to the hosting instance role. It does not modify customer roles, security groups, database policies or login users.
4. After successful bootstrap, set environment variable `BILLING_RELEASE_ROLE_ARN` to `arn:aws:iam::582287676741:role/CloudBillingGitHubRelease` in `billing-production`.
5. Verify the first automatic push release or dispatch an exact release with `deploy=true`, then check the final deployment receipt. Changes to the root-owned helper itself require this reviewed bootstrap step again; ordinary application releases do not update the helper or need personal AWS login.

The workflow path must be registered on the default branch before GitHub permits dispatch. This repository already has `checks.yml` on its default branch. Always verify dispatch works on the release branch before claiming setup is complete.

## Release

A push to the verified release branch starts checks and releases its exact tested SHA. Pushes to other branches, pull requests and scheduled checks cannot deploy. After checks and scans finish, the serialized deployment job verifies the current branch head through GitHub's exact-reference API before requesting AWS credentials. A superseded push records `skipped_superseded` and stops. An unavailable or invalid branch response fails closed. The coordinator repeats this check before making AWS calls. A release already activating is allowed to finish; a later push does not interrupt it.

For a manual release, dispatch the exact workflow commit:

```sh
gh workflow run checks.yml --repo ydsveluvolu2996/cloud-billing-console \
  --ref codex/billing-security-portfolio \
  -f deploy=true -f expected_sha=FULL_40_CHARACTER_APPROVED_SHA \
  -f reason='Describe the approved release'
```

The dispatched workflow's commit must equal the supplied SHA; a manual dispatch does not silently follow a newer branch head. Both paths check the exact repository name and ID, branch, event and full SHA. The job obtains a two-hour AWS session through the existing GitHub OIDC role; there are no stored AWS access keys. The role can upload encrypted objects under one artifact prefix, invoke one fixed SSM document on one specific instance, and read command results. It cannot invoke `AWS-RunShellScript`, modify IAM or read customer credentials. Environment branch restrictions are essential because the OIDC trust binds to the repository/environment identity.

CI packages tracked source, the scanned Docker images and offline Python wheels. The coordinator hashes the downloaded CI artifact, uploads versioned encrypted objects, and pins each object version/checksum in a manifest. The host verifies the manifest, target, checksums and compatibility before activation. It also verifies the image's CI configuration digest and layers before pinning its local image ID. The collector installs its dependencies into a new root-owned virtualenv using the offline wheelhouse.

One combined activation pauses collection, switches the web image and collector virtualenv, restarts the collector, and verifies both runtimes. Existing code, dependencies and web image settings are retained for rollback; obsolete runtime source files are removed. The web step runs the existing encrypted database backup before switching containers. Checks include the actual restricted PostgreSQL runtime, collector service, HTTPS health endpoint, anonymous login redirect, MFA login field and static assets. The public site's restricted ingress is preserved; HTTPS checks run locally through SSM with the real hostname and TLS verification.

A failed activation restores both runtimes on the combined host; the coordinator also requests an idempotent rollback. A failed rollback marks the release `rollback_failed` and fails the run; it must be investigated before another release. The workflow's concurrency gate serializes releases and never cancels a running release for a newer push. Do not manually cancel a release while activation is running. An interrupted run's SSM command may continue; inspect its command IDs and host state before retrying. The host lock prevents concurrent mutation and an old release cannot roll back a newer one.

## Maintenance boundaries and recovery

Automatic schema policy v1 permits exactly one canonical billing migration adding one nullable `DateTimeField` to the existing `SavedReport` model, without a default, index, constraint or custom code. All previous migration and infrastructure hashes must remain unchanged. The installed helper uses fixed SQL, the existing local database-admin secret, an encrypted backup, preservation checks and a recoverable baseline journal; it never imports candidate migration Python. See [additive migration boundaries and recovery](additive-migrations.md).

Other schema changes, generated database role SQL, user-administration SQL, compose configuration, collector services and single-EC2 metadata/service controls remain separate reviewed maintenance changes. This pipeline does not change database roles, restart PostgreSQL/Caddy, open ingress or grant additional customer billing permissions.

Once the coordinator has uploaded the manifest and entered its deployment block, it writes a `billing-deployment-SHA-ATTEMPT` GitHub artifact retained for 90 days with manifest coordinates, SSM command IDs and status. A staging failure records `stage_failed`. Failed commands retain only their status and an allowlisted helper error code (`validation_failed`, `command_failed`, `timeout`, or `internal_error`); unknown or older helper errors become `helper_failed`. Invalid success receipts are rejected. Receipts never copy arbitrary helper error text, stderr or exception messages. Superseded pushes also produce a receipt, without AWS activity. Other failures before the deployment block (including context, OIDC or upload failures) are recorded in the failed GitHub job and may have no receipt artifact. On the host, `/opt/cloud-billing/.deployment/github-release-current.json` identifies the active release. Root-only state and rollback copies live under `/opt/cloud-billing-releases/RELEASE_ID/`. Database backups continue through the existing backup process.

For an interrupted or failed release, use the exact manifest version and checksum in its receipt with the fixed SSM document's `status` or `rollback` action. Rollback never undoes database changes. Keep the active release and its previous runtime/venv until the next release is verified. This initial version deliberately does not prune rollback directories or S3 versions automatically; monitor free disk and remove reviewed, obsolete releases during maintenance. Staging refuses insufficient disk before any activation. CI artifacts expire after 3 days, but versioned S3 deployment artifacts remain available under the bucket's lifecycle policy.

## Validation

```sh
python -m unittest discover -s deploy/github-release/tests -v
```

Tests cover archive traversal/links/size, checksum rejection, schema compatibility, exact release identity, superseded-push rejection before AWS, safe workflow concurrency, sanitized failure receipts, restoration after failed health checks, obsolete source removal, prevention of stale rollback, combined web/collector rollback and the IAM/SSM permission boundary. GitHub additionally runs the full application tests and security scans. End-to-end AWS deployment is only verified when the release receipt reports the combined runtime active and live functional checks pass.
