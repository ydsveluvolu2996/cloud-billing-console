# B01–B34 build report

All 34 requirements have implementation and verification work on `codex/billing-security-portfolio`. The original Django/PostgreSQL/Caddy application and effective ownership/fact/Alliance models were extended. This build is ready for **draft code review**, with explicit release blockers. It is not deployed or approved for a live pilot.

The latest remote main inspected was `68e322ae2d4663a13fc2030bae7ffebca2ab6fb8`; no newer release was replaced with that historical commit. Work uses an isolated worktree. No production data was copied, no real AWS connections were activated, no IAM policies were changed, no notifications/invitations were sent, and nothing was merged or deployed.

## Delivered changes

Manual role onboarding supports customer-approved names/paths, server-generated unique External IDs, exact policy JSON, administrator-approved exact ARN allowlisting, and strict successful/negative trust probes. Optional Organizations, tags/categories, forecasts, resources and Budget reads are independently approved; dashboard budgets need no AWS write permission. Customer consent records remain pending without real evidence.

Individual local MFA and configurable OIDC sit behind centralized customer/account authorization across pages, writes, bulk imports, reports, exports, history, background jobs and cache results. Actual PostgreSQL runtime roles enforce row/column privileges; guarded functions support invitations, offboarding and ownership restamping. An exclusion constraint rejects concurrent ownership conflicts. Existing histories and Alliance fields/notes/snapshots are retained, with append-only customer revision records added.

The generic expandable portfolio supports multiple payer/standalone/shared connections, account metadata, budgets and customer Alliance rollups. Monitoring, per-customer reconciliation, configuration-bound readiness, audited offboarding and retention tooling are included. External portal and live delivery flags default to disabled. Six-hour scheduling, retries, recent re-fetches and atomic data preservation remain, with additional late-lease and concurrent-source publication guards.

Prepared infrastructure separates the collector onto another EC2 instance/profile, removes AWS customer access from web, uses private verified-TLS database connectivity, SSM administration, restricted pilot HTTPS, encrypted storage/backups, separated secrets and protected central logging. Provider CloudFormation remains infrastructure-as-code; **customers do not need CloudFormation**.

## Validation commands and results

Commands ran from the isolated checkout using Python 3.12.11 and protected local test environment exports. PostgreSQL test admin/credentials were confined to `.deployment/` and are not in Git. `source .deployment/test.env` selected the loopback PostgreSQL 17 test database; it is a local file, not a distributable secret template.

| Command | Result / limits |
|---|---|
| `DEBUG=true DB_HOST= AWS_EC2_METADATA_DISABLED=true .venv/bin/python manage.py test billing.tests --noinput` (baseline) | 107 passed; one PostgreSQL-only skip on SQLite |
| `.venv/bin/python manage.py test billing.tests --noinput` (PostgreSQL final application suite) | **155 passed in 29.218 s**, no skips; actual restricted role, concurrency, isolation, MFA, OIDC, migration, reconciliation and retention tests included |
| `.venv/bin/python manage.py makemigrations --check --dry-run` | No model/migration drift |
| `.venv/bin/python manage.py migrate --noinput` | All migrations through 0019 applied to isolated synthetic database |
| `PYTHONPATH=. .venv/bin/python scripts/generate_database_roles.py` | Runtime policy SQL regenerated; CI verifies no committed diff |
| `DEBUG=false SECRET_KEY=<local-validation-value> .venv/bin/python manage.py check --deploy --fail-level WARNING` | Passed; HSTS preload warning W021 intentionally silenced because this domain is not submitted for preload |
| `.venv/bin/pip check` | No broken requirements |
| `.venv/bin/pip-audit -r requirements.txt --format json --output /tmp/billing-dependency-audit.json` | No known runtime Python dependency vulnerabilities |
| `.venv/bin/bandit -r billing config scripts -ll -ii -f json -o /tmp/billing-bandit.json` | No medium/high severity findings at medium/high confidence; not a proof of no defects |
| `.venv/bin/cfn-lint deploy/infrastructure.yaml` | Passed |
| `.deployment/tools/gitleaks git --redact` | No detected secrets; final-commit scan also runs in CI |
| `.venv/bin/python manage.py security_load --customers 100 --months 7 --services 3 --readers 8 --requests 80 --collect-sources 130 --output docs/evidence/security-load.json` and `--recheck` | 100 customers, 130 sources, 3,926 accounts, 2.26m initial rows; scoped p95 207.4 ms, zero isolation/HTTP failures; synthetic collection 100.3 s |
| `.venv/bin/python scripts/restore_drill.py --source billing_security_checks --target billing_security_restore_verified --bin-dir /opt/homebrew/opt/postgresql@17/bin --output docs/evidence/restore-drill.json` | 48 tables/547 synthetic records plus schema matched; 1.517 s, 182,306-byte dump; actual production backup/RTO not verified |
| `PYTHONPATH=. .venv/bin/python scripts/verify_ui.py` | 22 flow/viewport checks at 1440px and 390px; no HTTP/JavaScript errors or document overflow; synthetic screenshots include login/MFA, manual IAM, approvals, bulk, portfolio, budgets, Alliance history, monitoring and Cost Explorer |
| `.deployment/tools/trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 ...` | **Release blocked**: 40 high/critical Caddy package findings locally, 54 high/critical app-base package findings in initial CI; exact metadata below. Local PostgreSQL image analysis reached its five-minute timeout; final CI scans all three images. |
| `git diff --check` | Passed |

The GitHub workflow builds the application image successfully and runs tests against PostgreSQL 17, a 100-customer/130-source smoke workload, migration drift checks, runtime policy generation, infrastructure lint, dependency/secret/Bandit scans and critical/high container gates. The initial pushed application commit `e56c9b3` passed the full test job, including scale; its security job failed on the image gate. [Initial CI evidence](https://github.com/ydsveluvolu2996/cloud-billing-console/actions/runs/34313164770) is deliberately identified as intermediate. **The draft PR records the final branch SHA, exact CI links and conclusions after documentation/UI follow-up commits.** It does not substitute an earlier green check for final-commit evidence.

## Release blockers and external inputs

1. **Patched images:** [Caddy findings](evidence/container-findings.json) include Alpine curl/OpenSSL and embedded Go/dependency advisories; [application image findings](evidence/app-container-findings.json) include Debian packages for which the scanner reports no fixed version. No findings are ignored and no scanner gate is bypassed. Rebuild against supported patched upstream releases, rescan all application/database/proxy images, and require zero critical/high findings before release. Final CI artifacts provide the current image IDs and exact vulnerability database result. A completed scan is not a clean scan.
2. **Independent security review:** no independent assessment has occurred. Assign a reviewer, remediate/retest findings and record approval tied to the release SHA. See [review package](independent-review.md). No compliance certification is claimed.
3. **Per-customer inputs:** real approved contacts and individual users, payer/standalone/shared inventory, roles, optional fields/tag/category keys, storage region, retention and evidence; actual trust checks; whole-customer AWS references and reconciliation; rollout owner/date and rollback readiness. No fixed named-customer sequence or invented approvals exists.
4. **Identity/infrastructure verification:** actual IdP issuer/client/subject mappings and federation tests; approved pilot IP/VPN access; read-only live network/IMDS/STS/TLS/renewal evidence; schema-owner/runtime role checks; encrypted backup access/retention; central audit delivery/denial tests; production backup restore and agreed RPO/RTO. Prepared scripts are not live evidence.
5. **Capacity:** broad administrator portfolio p95 was about 9.9 seconds in the larger synthetic run. Measure/tune on actual EC2 before broad rollout. Fake AWS pagination cannot prove live quotas, latency, source freshness or six-hour capacity. See [measured workload and pricing assumptions](performance-security.md).
6. **Repository release controls:** an administrator must configure required checks/reviews/branch protection for the actual release process. This build did not alter protection settings, approve production actions or enable the portal.

## Proposed cost and deployment order

The simplest proposed separation adds a t3.small collector: approximately **$27–34/month incremental hosting**, or **$50–65/month for two t3.small hosts plus estimated storage/log/backup allowances**. The measured fake-pagination model implies about **$787/month for four daily Cost Explorer cycles**, before optional queries/history; budget a preliminary $800–1,100 API allowance and validate it during an authorized pilot. These are estimates, not incurred charges. Current official EC2/IPv4/API prices, assumptions and exclusions are documented in [performance-security.md](performance-security.md).

After a separate deployment authorization: resolve image/review gates → preserve validated backup and inventory → stop old worker/freeze writes → review infrastructure change set preserving data-bearing resources/collector principal → establish separate profiles/network/TLS/secrets → apply additive migrations as schema owner and runtime policies → verify web/MFA/scopes → start the isolated collector with approved exact roles only → verify audit/backup → reconcile and admit ready customer batches. Keep external portal/live notifications off until separately authorized.

Rollback stops collector publication and freezes writes first. Prefer a forward fix; retain compatible additive schema and the new security boundaries. Do not down-migrate history/security tables, restore broad IAM or bypass MFA/RLS. If incompatible, restore the validated pre-release backup to a new isolated database/volume, verify it, preserve the failed copy and account for later writes before approved cutover. Full procedures: [deployment](deployment-runbook.md), [recovery/offboarding](recovery-offboarding.md), [reconciliation](reconciliation.md).

The [tracker](build-tracker.md) lists priority, dependencies, implementation, local/CI evidence, real-environment status and outstanding gates separately for **every B01–B34 requirement**.
