# Cloud Billing Console

A private team dashboard for daily and monthly AWS costs across customer accounts. The entire application runs on one EC2 instance using Django, PostgreSQL, Docker Compose, Caddy HTTPS, and cron. A private S3 bucket stores releases, the onboarding template, and encrypted daily backups.

## Features

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
3. Run `docker compose build`, `docker compose up -d db`, `docker compose run --rm app python manage.py migrate`, and `docker compose up -d`.
4. Create the administrator with `docker compose exec app python manage.py createsuperuser`. No public signup exists. All regular application users are internal team readers; staff can manage customers, and superusers can manage users. Customer logins are not supported by this version.
5. Install `deploy/cloud-billing.cron` into `/etc/cron.d/cloud-billing`, `deploy/cloud-billing.logrotate` into `/etc/logrotate.d/cloud-billing`, and make `deploy/backup.sh` executable. Server time zone must be UTC.
6. Upload `deploy/customer-role.yaml` to `templates/customer-role.yaml` in the private artifact bucket. The app generates one-hour presigned quick-create links. The download-template route remains available if link creation fails.

SSM is the administration channel. No SSH key or port is required. The instance role can assume only `/BillingConsole/CostReadOnly` roles. Each customer's trust policy must independently authorize the exact collector principal and external ID. The app never stores temporary AWS credentials.

The single instance is a deliberate availability tradeoff. Keep Ubuntu/Docker/container dependencies patched. EC2 CPU credits are in standard mode to avoid unexpected surplus-credit charges; monitor capacity as customers grow. Increasing the instance size or moving PostgreSQL to RDS are later options.

## Customer onboarding

1. Add the customer name, payer/standalone account ID, and optional budget in the dashboard.
2. Generate their setup link. Their administrator enables Cost Explorer and creates the CloudFormation stack, which creates one IAM role allowing only `ce:GetCostAndUsage`.
3. Paste `RoleArn` from CloudFormation Outputs and select **Save & verify connection**. Initial collection is queued and usually starts within one minute.
4. Review imported totals using the same date range, currency, and cost basis in Cost Explorer. Current dates are estimates. Connect the management/payer account to include its linked accounts.
5. To disconnect, pause collection and have the customer delete the onboarding stack. Imported data is retained. Permanent data removal should follow your agreed customer retention process.

## Backups and recovery

`deploy/backup.sh` makes a PostgreSQL custom-format dump and copies it plus the deployment configuration into the private S3 `backups/` prefix. Backups expire after 35 days; noncurrent versions after 7 days. S3 encryption, TLS enforcement, and public-access blocks are enabled. The backup configuration contains secrets and must stay restricted to the deployment operators.

To restore, stop the app/cron, restore `.env` securely, bring up PostgreSQL, and stream the chosen dump into `docker compose exec -T db pg_restore -U billing -d billing --clean --if-exists`. Back up the existing database before restoring. Run migrations, restart the app, verify totals, then re-enable cron. The instance and data volume are retained on stack deletion to protect records; clean them up explicitly only after confirming retention and backups.

## Validation

```bash
DEBUG=true .venv/bin/python manage.py test billing.tests
DEBUG=true .venv/bin/python manage.py makemigrations --check --dry-run
DEBUG=true .venv/bin/python manage.py collectstatic --noinput
```

GitHub Actions runs the checks against PostgreSQL. `/health/` checks database availability and exposes only status. Application errors and scheduled-job results are retained in rotating local logs; inspect them through SSM. Budget alerts are visible inside the dashboard; outgoing email/Slack notification delivery is not configured.
