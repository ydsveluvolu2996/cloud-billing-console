> Historical release handoff. Deployment and identity instructions in this document are superseded by [the security deployment runbook](deployment-runbook.md). Do not use the old shared-host worker, broad role policy or deployment shortcuts for this release.

# Codex deployment handoff — customer/account/project/budget dashboard and blue monochrome redesign

Status: **implemented, tested and pushed. Deployment has not been performed.** Codex reviews,
merges and deploys. Nothing in this branch touched the live EC2 instance, the production database,
customer IAM roles, the published S3 template or AWS billing settings.

## Branch, pull request, release commit

| Item | Value |
| --- | --- |
| Repository | `ydsveluvolu2996/cloud-billing-console` |
| Branch | `claude/billing-console-dashboard-expansion-pbohul` (contains both the dashboard expansion and the UI redesign) |
| Pull request | Draft PR targeting `main`; URL in the delivery message and in the GitHub PR list for the branch |
| Release commit | The head of the branch at merge time. The exact SHA is reported in the delivery message; verify with `git rev-parse origin/claude/billing-console-dashboard-expansion-pbohul` before building. |
| Base | `main` at `6118383` (canonical dashboard hostname and HTTPS alias redirects) |

## Implemented features

Dashboard expansion (see `docs/customer-account-project-budgets.md` for details):

* Customers separated from AWS credential connections (`BillingSource`): several payer or
  standalone connections per customer, shared payers, member budget-read connections.
* Account inventory with effective-dated assignments, transfers that preserve history, disappearing
  accounts retained, unassigned review queue, duplicate payer/member detection.
* Durable PostgreSQL job queue and supervised `worker` container: six-hour slots with jitter,
  coalesced manual refreshes, per-connection exclusion, leases and crash recovery, exponential
  backoff, resumable per-month collection, request metering and configurable limits.
* Per-month atomic publication with coverage records (`CollectionPeriod`), monthly/weekly
  reconciliation, connection-version checks.
* Six-step resumable onboarding wizard, background verification with capability probes,
  Organizations discovery with billing-only fallback, initial import, rotation, pause/resume,
  offboarding with retained history, bulk CSV onboarding with validation and preview.
* Projects with versioned, effective-dated, mutually exclusive allocation rules, previews,
  scoped cached AWS queries for tag/category rules, reconciliation with a visible remainder.
* Dashboard budgets (customer, payer, account, project; history, month overrides, filters,
  thresholds 80%/100%, deduplicated acknowledgeable alerts, AWS or run-rate forecasts, honest data
  status) and read-only imported AWS budgets.
* Portfolio overview, server-side customer table, customer tabs (Accounts, Projects, Budgets,
  Reports, Sync), account tree and account detail, central budget table, onboarding operations
  view, activity page with the job queue.
* Central scope authorization (`billing/scope.py`) covering reports, exports, metadata, saved
  reports and worker queries. Reader/operator/admin roles preserved.

UI redesign:

* Single token-based stylesheet (`static/app.css`) implementing the blue monochrome design
  (primary `#2563EB`, hover `#1D4ED8`, deep `#1E3A8A`, soft `#EFF6FF`, selected `#DBEAFE`, page
  `#F6F8FC`, text `#0F172A`/`#475569`/`#64748B`, border `#E2E8F0`), 4/8/12/16/24/32 spacing, 8 px
  controls and 12 px cards, system font stack (no external font service).
* Application shell: white sidebar with inline SVG icons and clear active item, compact top bar
  with page context and account controls, off-canvas navigation below 1024 px, right-hand report
  parameters panel that becomes a drawer below 1100 px with a sticky Apply button.
* Charts in blue shades with fill patterns, dash styles and marker shapes (legend swatches match);
  credits are hatched and labelled; missing values stay gaps.
* State badges combine glyph, text and border so meaning never depends on colour alone.
* Login, password change and error pages restyled; forms use persistent labels, required
  indicators and inline validation; tables use right-aligned tabular numerals, sticky headers and
  contained horizontal scroll.

Screenshots (synthetic development data only, no customer information):
`docs/screenshots/before/` (previous design, 1440 px and 390 px) and `docs/screenshots/after/`
(1440/1024/768/390 px for the main pages).

## Known limitations

* Verification, discovery and imports run in the worker; the wizard shows queued/verified states
  after a page reload (no live push). The worker polls every `WORKER_POLL_SECONDS` (5 s).
* Tag/category project allocations depend on additional Cost Explorer requests; they show
  "pending" until the worker has fetched them, and only cover accounts the customer owns.
* Cost Explorer only exposes account/service facts additively; region, tag or resource detail is
  never inferred from them.
* AWS forecasts for budgets are requested per scope and cached; when AWS returns no forecast the
  run-rate projection is used and labelled.
* Percentage-based imported AWS budgets (RI/Savings Plans) are shown read-only, never as currency.
* The portfolio "Cost overview" page aggregates across all customers for the selected range; at
  2.3 million cost rows several of its aggregate queries take seconds (see `docs/capacity.md`).
  A monthly rollup table is the recommended follow-up if wide date ranges are used routinely.
* Reverse migration below `0003` requires that every customer still has exactly one connection;
  otherwise restore from the pre-upgrade backup (see Rollback).
* Customer self-service login, external notifications and AWS budget actions are out of scope.

## Tests, CI and capacity evidence

* `python manage.py test billing.tests`: 90 tests, passing on SQLite and PostgreSQL 16
  (locally) — collection, pagination, transfers, shared payers, discovery fallback, bulk CSV,
  jobs (coalescing, leases, crash recovery, backoff, fairness, scheduling), allocation rules and
  reconciliation, budgets (history, overspend, forecasts, alert deduplication, stale data,
  portfolio totals, CSV), onboarding wizard/rotation/offboarding, scope isolation, existing
  dashboard/filter/export regressions.
* Migration rehearsal on PostgreSQL: legacy `0002` schema with customers, costs, cached queries and
  saved reports migrated forward (IDs, costs, budgets, external IDs preserved), backward to `0002`
  and forward again.
* GitHub Actions (`.github/workflows/checks.yml`): collectstatic, `makemigrations --check`, the test
  suite against PostgreSQL 17, `run_worker --once`, a small `synthetic_load` run, `check --deploy`.
* Capacity: `docs/capacity.md` records the isolated synthetic load run (100 customers, 2,000
  accounts, 7 months, 2.3 M rows) with dashboard latencies, query counts, memory, job fairness and
  a simulated collection cycle. These are simulated results on the CI-like sandbox, not live
  verification, and do not prove the current t3.small is sufficient.
* Browser verification: Playwright/Chromium at 1440, 1024, 768 and 390 px; no console errors and no
  page-wide horizontal overflow on the checked pages (dense tables scroll inside their containers).

## Configuration (names only; set values in the private `.env`)

Existing: `DEBUG`, `SECRET_KEY`, `DB_PASSWORD`, `DASHBOARD_HOST`, `ALLOWED_HOSTS`,
`CSRF_TRUSTED_ORIGINS`, `AWS_REGION`, `COLLECTOR_ROLE_ARN`, `ARTIFACT_BUCKET`, `HISTORY_MONTHS`.

New (all optional, defaults in `config/settings.py`): `WORKER_CONCURRENCY` (3),
`WORKER_POLL_SECONDS` (5), `WORKER_TICK_SECONDS` (30), `JOB_LEASE_SECONDS` (600),
`JOB_MAX_ATTEMPTS` (6), `JOB_BACKOFF_SECONDS` (60), `JOB_BACKOFF_CAP_SECONDS` (3600),
`SCHEDULE_JITTER_SECONDS` (900), `RECONCILE_MONTHS` (3), `MAX_REQUESTS_PER_JOB` (400),
`MAX_PAGES_PER_REQUEST` (1000), `AWS_REQUEST_INTERVAL_SECONDS` (0), `AWS_THROTTLE_RETRIES` (3),
`AWS_THROTTLE_SLEEP_FACTOR` (1.0), `BUDGET_AWS_FORECASTS` (true), `RUN_RATE_MIN_DAYS` (3).

No new secrets. The collector still uses the instance role to assume customer roles; nothing stores
access keys or session tokens.

## Database migrations and compatibility

Migrations `billing/migrations/0003` → `0006`:

* `0003_dashboard_expansion`: new tables and nullable columns; relaxes legacy `Customer.account_id`
  / `external_id` constraints.
* `0004_migrate_customer_connections` (data): one `BillingSource` per legacy customer (same account
  ID, role ARN, external ID, enabled/sync flags and timestamps), `AwsAccount` +
  `AccountAssignment` for every account seen in costs, `CollectionPeriod` rows per imported month,
  a customer-scope `Budget` from the legacy `budget` field, `Cost`/`SyncRun`/`ExplorerQuery`
  re-pointed to the source. Cached queries without a source are deleted (none expected).
* `0005_remove_legacy_customer_fields`: drops the legacy columns.
* `0006_cost_scope_index`: adds index `(customer, currency, day)` on `billing_cost`.

Compatibility: customer UUIDs, cost rows, saved reports and external IDs are unchanged, so existing
customer IAM stacks keep working without any change. Explorer cache fingerprints now include the
connection version, so cached AWS report data refreshes on the next worker cycle after the upgrade
(first requests after deploy show "pending" for a minute). Migration `0004` runs in one transaction
and iterates customers; on the current data volume it completes in seconds. Take a backup first.

## Worker / scheduler cutover

1. The cron file `deploy/cloud-billing.cron` replaces the two `sync_costs` lines with one hourly
   safety net (`sync_costs` at minute 15). Install it over `/etc/cron.d/cloud-billing`.
2. `compose.yaml` adds the `worker` service (`python manage.py run_worker`, same image,
   `stop_grace_period: 120s`). Start it with `docker compose up -d`.
3. On first start the worker schedules the current slot for every verified, enabled connection
   (idempotent keys); migrated connections keep `initial_import_done=true` so only current and
   previous months are refreshed.
4. `sync_costs` remains as a compatibility command that schedules and drains the queue once under
   the previous process lock; running both is safe because job keys are unique.

## Customer IAM capability changes

`deploy/customer-role.yaml` adds parameters `EnableOrganizationsDiscovery` (default `false`;
grants `organizations:DescribeOrganization` and `organizations:ListAccounts`) and
`EnableBudgetImport` (default `false`; grants `budgets:ViewBudget` on
`arn:aws:budgets::<account>:budget/*`). The six Cost Explorer actions and the trust policy (exact
collector role ARN + external ID) are unchanged. The `ExternalId` pattern now accepts 36–64 hex
characters so rotated IDs work; existing UUID external IDs remain valid.

Upgrade path for an existing customer: update the stack with the new template (parameters can stay
`false`, nothing else changes), or set the parameters to `true` to unlock complete member
inventory and budget import, then click "Re-run verification" on the connection page.

## Onboarding template publishing

After merging, replace the generic template in the private artifact bucket so quick-create links
render the new parameters:

```bash
aws s3 cp deploy/customer-role.yaml s3://"$ARTIFACT_BUCKET"/templates/customer-role.yaml --region "$AWS_REGION"
```

The dashboard passes `param_EnableOrganizationsDiscovery` and `param_EnableBudgetImport` in the
quick-create URL; the download-template route embeds the same defaults. Do not publish until the
application is upgraded, otherwise links from the old release would still work but ignore the new
parameters.

## Backup, build, migrate, restart, health check

```bash
cd /opt/cloud-billing
/opt/cloud-billing/deploy/backup.sh                                      # pre-upgrade backup to S3 backups/
git fetch origin && git checkout <release-sha>                            # or copy the release archive
docker compose build
docker compose run --rm app python manage.py migrate --noinput
docker compose run --rm app python manage.py check --deploy
docker compose up -d                                                      # recreates app, starts worker, keeps db/proxy
install -m 0644 deploy/cloud-billing.cron /etc/cron.d/cloud-billing
docker compose ps
curl -fsS https://"$DASHBOARD_HOST"/health/                               # {"status": "ok"}
docker compose logs --since 5m worker                                     # "worker starting concurrency=3"
docker compose exec -T app python manage.py run_worker --once             # optional: drain immediately
```

Static assets are collected in the image build (`collectstatic` in `deploy/Dockerfile`) and served
by WhiteNoise with hashed names; no CDN or external font is required.

## Rollback and data recovery

1. `docker compose stop app worker`.
2. Check out the previous release commit (`6118383`) and `docker compose build`.
3. Preferred: restore the pre-upgrade dump (`pg_restore --clean --if-exists`, see README) so the
   schema matches the old release exactly. Alternative when no data changed since the upgrade:
   `docker compose run --rm app python manage.py migrate billing 0002` on the *new* image, then
   switch images. The reverse of `0004` re-creates legacy columns from each customer's first
   connection and requires one connection per customer.
4. Reinstall the previous cron file, `docker compose up -d app`, verify `/health/` and totals.
5. Data recovery of individual periods: re-queue an import from the connection page ("Re-import
   history") or `manage.py sync_costs --customer <uuid> --full`; publication is atomic per month so a
   failed retry never removes the previous snapshot.

## Post-deployment billing reconciliation checks

1. Sync & activity: every connection has a `collect` job `done` for the current slot; no `failed`
   jobs other than connections awaiting setup.
2. Customer directory: status for each migrated customer is Connected (or Stale data until the
   first worker cycle completes); "Refresh queued" clears within the cycle.
3. For two or three customers, compare Cost overview month-to-date totals with AWS Cost Explorer
   (same dates, USD, unblended). Current-month values are estimates.
4. Customer → Accounts tab: payer total equals the sum of member rows including the management
   account's own row; no account appears under two customers.
5. Customer → Sync tab: `CollectionPeriod` rows show `complete` for the current and previous month
   with non-zero request counts.
6. Budgets: customer budgets migrated from the legacy field appear with the same amount; statuses
   are evaluated (worker `evaluate_budgets` job) and no "Within budget" appears with stale data.
7. Accounts → Unassigned queue is empty for non-shared payers.
8. Explorer: saved reports open and refresh; CSV exports include the "Scope" footer row.

## Codex deployment review fixes

* Preserve `DASHBOARD_ALIASES` in Compose, Caddy and the environment example so the old HTTPS address continues redirecting to the canonical hostname.
* Lock the connection row when leasing work, and recheck active leases after locking. Separate job rows for one connection cannot be claimed concurrently; PostgreSQL concurrency regression coverage is included.
* The hourly safety net now schedules due work before draining it, so it also works when the supervised worker is down.
