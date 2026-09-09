# Build tracker — security and customer portfolio

Base inspected 2026-09-09: `origin/main` at `68e322ae2d4663a13fc2030bae7ffebca2ab6fb8`. Isolated branch: `codex/billing-security-portfolio`. No production changes authorized.

Baseline: `DEBUG=true DB_HOST= AWS_EC2_METADATA_DISABLED=true .venv/bin/python manage.py test billing.tests --noinput`: **107 tests passed, 1 PostgreSQL-only test skipped on SQLite**.

Statuses are separate: implementation, local/CI evidence, real environment evidence and external gates. No customer approval or independent review is implied by local tests.

| ID | Priority | Requirement | Dependencies | Implementation | Verification evidence | External inputs / gates |
|---|---|---|---|---|---|---|
| B01 | P0 | Manual IAM role onboarding | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B02 | P0 | Exact customer role allowlisting | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B03 | P0 | Identity and External ID verification | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B04 | P0 | Minimum permissions and capability controls | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B05 | P0 | Per-customer approval and onboarding records | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B06 | P1 | Generic customer/payer/account portfolio | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B07 | P0 | Discovery, assignment and historical ownership | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B08 | P1 | Customer, account and project budgets | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B09 | P1 | Customer rollups in Alliance reporting | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B10 | P1 | Bulk customer onboarding | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B11 | P0 | Individual authentication, SSO and MFA | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B12 | P0 | Customer authorization everywhere | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B13 | P0 | Security and isolation testing | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B14 | P0 | Collector identity separation | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B15 | P0 | Database privilege separation | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B16 | P0 | Network and administration controls | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B17 | P0 | Encryption, region and backup access | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B18 | P0 | Secrets and sensitive logging | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B19 | P0 | Protected audit logging | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B20 | P1 | Alerts and operational monitoring | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B21 | P0 | Secure CI and release gates | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B22 | P0 | Backup restore and recovery | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B23 | P0 | Offboarding and retention | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B24 | P0 | Reusable per-customer reconciliation | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B25 | P1 | Performance for 100 customers | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B26 | P1 | Generic staged customer rollout | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B27 | P2 | External customer portal | B28–B34 | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B28 | Preserve and verify | Daily/monthly reporting | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B29 | Preserve and verify | Six-hour collection | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B30 | Preserve and verify | Ownership model | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B31 | Preserve and verify | Alliance foundations | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B32 | Preserve and verify | HTTPS and web protections | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B33 | Preserve and verify | Temporary AWS credentials | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
| B34 | Preserve and verify | Freshness and coverage | Baseline | Auditing | Baseline only | Production verification pending; customer approvals and independent review where applicable |
