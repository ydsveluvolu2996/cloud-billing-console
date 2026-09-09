# Cloud Billing Console

A private team dashboard for daily and monthly AWS costs across customer accounts. The entire application runs on one EC2 instance using Django, PostgreSQL, Docker Compose, Caddy HTTPS, and cron. A private S3 bucket stores releases, the onboarding template, and encrypted daily backups.

## Features

The dashboard now manages the whole customer estate, not only cost charts: business customers with several payer or standalone connections, an inventory of every linked account with effective-dated ownership, projects with versioned allocation rules, and budgets at customer, payer, account and project level. Collection runs through a durable PostgreSQL job queue serviced by a separately supervised worker container. See [docs/customer-account-project-budgets.md](docs/customer-account-project-budgets.md) for the model and rules, [docs/capacity.md](docs/capacity.md) for measured capacity and [docs/CODEX_DEPLOYMENT_HANDOFF.md](docs/CODEX_DEPLOYMENT_HANDOFF.md) for deployment.

- Portfolio overview, server-side searchable/sortable customer directory, customer detail with Accounts, Projects, Budgets, Reports and Sync tabs, payer/member account tree, account detail with service and daily/monthly breakdowns.
- Resumable six-step onboarding wizard with per-connection CloudFormation links, background role verification, Organizations discovery (billing-only fallback), initial import, rotation, pause/resume, offboarding with retained history and bulk CSV onboarding.
- Shared payers with explicit, disjoint account assignments and an unassigned review queue; duplicate payer/member detection; historical ownership on account transfers.
- Projects allocated by linked accounts, activated tag, cost category or account + tag/category with previews, reconciliation and a visible Unallocated / shared remainder.
- Dashboard budgets (history, month overrides, filters, thresholds, alerts, run-rate or AWS forecasts, honest data status) and read-only imported AWS budgets.
- Blue monochrome design system with token-based CSS, patterned monochrome charts and an accessible application shell.

- Cost Explorer-style home report: six complete months by default, stacked/bar/line charts, service/account/customer grouping, a right-side parameter panel, and a searchable monthly or daily cost matrix.
- Separate customer portfolio for budgets and month-end projections. Matrix CSV exports match the visible period columns; detailed CSV exports retain the original cost slices.

- Customer, linked-account, service, currency, date range, and cost-basis filters.
- Daily/monthly charts with keyboard-accessible values, date shortcuts, period comparisons, month-to-date totals, budget indicators, and a simple month-end run-rate projection.
- Searchable, sortable customer, AWS account, and service breakdowns; drill-downs preserve dates, currency and cost basis.
- Customer connection directory, visible import status, and an administrator-only refresh action for the selected customer or portfolio.
- Six-hour collection at 00:00, 06:00, 12:00, and 18:00 UTC, plus queued manual imports.
- Customer-specific read-only IAM onboarding with external IDs and verification.
- Authentication, login rate limiting, administrator-only connection management, CSV exports, and audit activity.
- Exact decimal storage, pagination, retry handling, and atomic replacement of revised AWS costs.

This is cost analytics, not invoicing or payment tracking. Cost Explorer data is delayed and current-period values can change. Initial imports cover six previous months plus the current month when AWS makes them available. Routine imports refresh the previous and current months; the first run on the second of each month reconciles the full history window. Different currencies are never summed. In chart data and customer rows, zero means an imported zero; a dash means no imported figure is available. Missing dates remain chart gaps. Nonzero charges smaller than 0.01 remain visible in detailed tables. Service shares are calculated against net spend, so credits can produce negative shares or shares above 100%. Projections and budget checks always cover the whole customer in the current month, even when a service/account filter is selected. AWS API requests incur charges, including failed/manual retries and pagination.

## Local development

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
export DEBUG=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8765
```

Local development defaults to SQLite. Production requires PostgreSQL. Do not publish the development server or seed demo data into the production database. The production build contains no sample customer costs or default password.

## Deployment

1. Deploy `deploy/infrastructure.yaml` in your chosen account with an Ubuntu 24.04 AMI, a public subnet, and `CAPABILITY_NAMED_IAM`. It creates a t3.small, 30 GiB encrypted gp3 disk, stable public IP, SSM administration, and a private versioned S3 bucket. Only HTTP/HTTPS are inbound; the database and application port are not published.
2. Copy a release to `/opt/cloud-billing`. Generate a private `.env` based on `.env.example`. Point your hostname to the public IP. The temporary `sslip.io` hostname is replaceable and depends on that service's DNS availability.
3. Run `docker compose build`, `docker compose up -d db`, `docker compose run --rm app python manage.py migrate`, and `docker compose up -d` (this starts the `app`, `worker` and `proxy` services; the worker schedules and runs all collection jobs).
4. Create the administrator with `docker compose exec app python manage.py createsuperuser`. No public signup exists. All regular application users are internal team readers; staff can manage customers, and superusers can manage users. Customer logins are not supported by this version.
5. Install `deploy/cloud-billing.cron` into `/etc/cron.d/cloud-billing` (backups, session cleanup and an hourly safety-net `sync_costs --queued` in case the worker container is down), `deploy/cloud-billing.logrotate` into `/etc/logrotate.d/cloud-billing`, and make `deploy/backup.sh` executable. Server time zone must be UTC.
6. Upload `deploy/customer-role.yaml` to `templates/customer-role.yaml` in the private artifact bucket. The app generates one-hour presigned quick-create links. The download-template route remains available if link creation fails.

SSM is the administration channel. No SSH key or port is required. The instance role can assume only `/BillingConsole/CostReadOnly` roles. Each customer's trust policy must independently authorize the exact collector principal and external ID. The app never stores temporary AWS credentials.

To rename the dashboard, point the new hostname at the Elastic IP and update `DASHBOARD_HOST`, `ALLOWED_HOSTS`, and `CSRF_TRUSTED_ORIGINS` in the private deployment environment. Set `DASHBOARD_ALIASES` to a space-separated list of previous hostnames to keep their HTTPS links working as permanent redirects to the new hostname, preserving paths and query strings. Leave it empty when no aliases are needed. Keep alias DNS records pointed at this instance for certificate renewal. Validate the Caddy configuration and recreate both the app and proxy containers after changing their environment. Verify the new HTTPS certificate, login page, health endpoint, and old-link redirects. No database migration is needed for a hostname change.

The single instance is a deliberate availability tradeoff. Keep Ubuntu/Docker/container dependencies patched. EC2 CPU credits are in standard mode to avoid unexpected surplus-credit charges; monitor capacity as customers grow. Increasing the instance size or moving PostgreSQL to RDS are later options.

## Customer onboarding

1. Create the customer (name, internal reference, owner, optional monthly budget), then add each management/payer or standalone account as a connection. Duplicate accounts and members of already-connected payers are rejected.
2. Generate the connection's setup link or template. The customer's administrator enables Cost Explorer and creates the CloudFormation stack: one read-only role with the six Cost Explorer actions, plus optional `organizations:DescribeOrganization`/`ListAccounts` for a complete inventory and optional `budgets:ViewBudget` for read-only budget import.
3. Paste `RoleArn` from the stack Outputs. The worker assumes the role with the connection's unique external ID, confirms the account identity, records capabilities and discovers linked accounts (billing-only fallback when Organizations access is declined).
4. Assign accounts (shared payers use the review queue), create projects and budgets, and queue the initial import (six completed months plus the current month).
5. Connection states: Awaiting customer setup, Connection verified, Account discovery complete, Initial import running, Connected, Partial data, Permission problem, Stale data, Paused. Rotate the external ID to reconnect; pause or offboard to stop collection while retaining history.
6. Bulk onboarding: upload a CSV under Onboarding → Bulk CSV, review the validated preview, then apply. No customer e-mails are sent automatically.

## Backups and recovery

`deploy/backup.sh` makes a PostgreSQL custom-format dump and copies it plus the deployment configuration into the private S3 `backups/` prefix. Backups expire after 35 days; noncurrent versions after 7 days. S3 encryption, TLS enforcement, and public-access blocks are enabled. The backup configuration contains secrets and must stay restricted to the deployment operators.

To restore, stop the app/cron, restore `.env` securely, bring up PostgreSQL, and stream the chosen dump into `docker compose exec -T db pg_restore -U billing -d billing --clean --if-exists`. Back up the existing database before restoring. Run migrations, restart the app, verify totals, then re-enable cron. The instance and data volume are retained on stack deletion to protect records; clean them up explicitly only after confirming retention and backups.

## Validation

```bash
DEBUG=true .venv/bin/python manage.py test billing.tests
DEBUG=true .venv/bin/python manage.py makemigrations --check --dry-run
DEBUG=true .venv/bin/python manage.py collectstatic --noinput
DEBUG=true .venv/bin/python manage.py run_worker --once          # schedule and drain the job queue once
DEBUG=true .venv/bin/python manage.py synthetic_load --customers 5 --accounts 50 --months 2 --output /tmp/load.json  # isolated database only
```

`synthetic_load` refuses to run against a database containing real customers; use it only on an isolated database (see `docs/capacity.md`).

GitHub Actions runs the checks against PostgreSQL. `/health/` checks database availability and exposes only status. Application errors and scheduled-job results are retained in rotating local logs; inspect them through SSM. Budget alerts are visible inside the dashboard; outgoing email/Slack notification delivery is not configured.

## Complete report parameters

The Cost Explorer page includes all 19 console billing filters, five cost bases, hourly/daily/monthly reports, usage quantities, period comparisons, AWS forecasts, tag/category absence views, filter visibility preferences, saved reports and import of the supplied AWS report URL. See [the parameter map](docs/cost-explorer-parameters.md) for exact behavior and AWS prerequisites.

Advanced reports use a PostgreSQL-backed request cache and the existing cron worker. New queries run within the one-minute queue schedule; reports opened within seven days and saved reports refresh every six hours. Failed refreshes retain prior results and expose the error. Incomplete reports block CSV export. AWS Cost Explorer charges apply per API request/page, including metadata and forecast requests; cached chart changes make no additional AWS calls.

For upgrades, back up first, build the application image, run `docker compose run --rm app python manage.py migrate --noinput`, and recreate the app. Update the existing customer IAM stack with `deploy/customer-role.yaml` and replace the generic onboarding template in the private artifact bucket. Resource/hourly opt-ins and tag activation remain customer-controlled.
