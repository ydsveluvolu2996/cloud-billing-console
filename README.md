# Cloud Billing Console

A reusable AWS billing dashboard on Django 5.2, PostgreSQL 17, a durable collection queue and Caddy. The web application stays on EC2; customer AWS access moves to a separate EC2 collector with its own instance profile. This branch is a review build, not a production deployment.

The same customer → payer/connection → account model supports multiple payers, standalone accounts, shared payers, effective ownership, projects, budgets and Alliance handoffs. Reporting uses decimal amounts, explicit currencies and inclusive UI/exclusive AWS dates. Six-hour polling does not imply six-hour AWS data freshness.

Customers manually create one approved read-only IAM role. Arbitrary valid approved names/paths are supported. No customer stack, agent, long-lived key or billing export setup is required. Optional capabilities and metadata need explicit approval. Trust verification checks the actual role trust document, exact collector principal, account identity and both negative External ID cases. An administrator must separately approve and deploy the exact provider allowlist.

Individual login requires MFA. Configurable OIDC uses pre-provisioned issuer/subject identities. All customer access needs explicit membership; a superuser also needs an explicit portfolio grant. Web, collector and administration database identities are separate. External customer access and live notification delivery default to disabled.

## Review and operations

- [B01–B34 tracker](docs/build-tracker.md) and [final build report](docs/build-report.md).
- [Manual IAM onboarding](docs/manual-iam-onboarding.md) and [security architecture](docs/security-architecture.md).
- [Deployment runbook](docs/deployment-runbook.md), [recovery/offboarding runbook](docs/recovery-offboarding.md) and [independent review package](docs/independent-review.md).
- [Reconciliation guide](docs/reconciliation.md), [100-customer measurements and cost estimate](docs/performance-security.md), [UI evidence](docs/evidence/ui-results.json).
- Existing [budget/project rules](docs/customer-account-project-budgets.md), [Alliance definitions](docs/alliance-reporting.md) and [report parameters](docs/cost-explorer-parameters.md) remain applicable except where the new security/runbook documents explicitly supersede operational instructions.

## Local development

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
DEBUG=true .venv/bin/python manage.py migrate
DEBUG=true .venv/bin/python manage.py createsuperuser
```

Provision an individual portfolio administrator (the username is a placeholder, not a seeded account):

```sh
DEBUG=true RUNTIME_ROLE=admin .venv/bin/python manage.py security_admin portfolio --username YOUR_INDIVIDUAL_USERNAME --actor YOUR_INDIVIDUAL_USERNAME --evidence LOCAL_DEVELOPMENT_ONLY
DEBUG=true .venv/bin/python manage.py runserver 127.0.0.1:8000
```

Enroll MFA on first login. No customers or approvals are seeded automatically. For disposable browser review only, `scripts/seed_review.py` requires an empty database and stores generated credentials in ignored, restricted `.deployment/review.json`. Never run it against production.

Run the full tests on an isolated PostgreSQL database; `DatabaseRoleTests` creates restricted logins and must run as a test-cluster administrator. SQLite is supported for quick unit checks and explicitly skips PostgreSQL-only verification.

```sh
DEBUG=true .venv/bin/python manage.py test billing.tests --noinput
DEBUG=true .venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/pip-audit -r requirements.txt
.venv/bin/bandit -r billing config scripts -ll -ii
.venv/bin/cfn-lint deploy/infrastructure.yaml
```

The production web container cannot run migrations or collection. Use the administration runtime for migrations and `RUNTIME_ROLE=collector` on the isolated collector host for `manage.py run_worker`. The runbook describes private TLS database setup, identity grants and backup handling. Do not use the retired automatic account bootstrap or historical customer CloudFormation template for new onboarding.
