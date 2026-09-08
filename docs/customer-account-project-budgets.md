# Customer, account, project and budget management

This document describes the data model, collection pipeline, allocation and budget rules
introduced with the dashboard expansion, and how they map to the AWS permissions each
customer grants. Read it together with `docs/CODEX_DEPLOYMENT_HANDOFF.md` (deployment) and
`docs/capacity.md` (measured capacity).

## Data model

| Model | Purpose |
| --- | --- |
| `Customer` | Business customer (UUID primary key preserved from earlier releases): name, internal reference, owner, reporting currency, active/offboarded. |
| `BillingSource` | One IAM role connection: management/payer, standalone, or member-budget reader. Holds account ID, role ARN, unique 160-bit hex external ID, `connection_version`, capabilities detected during verification, discovery mode, onboarding step and freshness fields. Cost-collecting sources are unique per AWS account. |
| `AwsAccount` | Inventory of every account seen through Organizations or billing data: name, e-mail, `State`, payer account, environment label, first/last seen and `missing_since`. Never deleted. |
| `AccountAssignment` | Effective-dated ownership (`start` inclusive, `end` exclusive, blank = current). Non-overlapping per account. Transfers end the previous assignment instead of rewriting history. |
| `Cost` | Additive daily fact: source, owning customer on that day (nullable = unassigned), day, linked account, service, currency, unblended, amortized, estimated. Unique per source/day/account/service/currency. |
| `CollectionPeriod` | Per source and month: status, revision, attempts, rows, request/page counts, duration, first/last day, estimated flag, last success/error. |
| `Project` / `AllocationRule` | Logical allocation with versioned, prioritised, effective-dated rules: linked accounts, activated tag, cost category, or accounts combined with a tag/category. |
| `ProjectCost` | Allocated daily amounts per rule/source/account/currency. |
| `Budget` / `BudgetAmount` | Dashboard budgets at customer, payer, account or project scope; recurring effective-dated amounts plus specific-month overrides; currency, cost basis, service filters, thresholds. |
| `BudgetEvaluation` | Snapshot per budget/month: amount, actual, forecast and method, status, data status. |
| `ImportedBudget` | Read-only AWS Budgets snapshot for the owning account (type, period, limit, unit, filters, AWS actual/forecast, timestamps, raw). |
| `Alert` | Deduplicated threshold alert per budget/month/kind/threshold with acknowledgement. |
| `Job` | Durable queue row: kind, coalescing key, source, payload, progress, lease, attempts, backoff. |
| `AuditEvent`, `SyncRun`, `ExplorerQuery`, `SavedReport`, `BulkImport` | Audit trail, per-run history, cached AWS report requests (now per connection and customer scope), saved reports, CSV previews. |

### Ownership rules

* A payer connection is collected once. Each cost row is stamped with the customer that owned the
  linked account on that day. Non-shared connections auto-assign new accounts to their owner;
  shared payers leave new accounts unassigned and they appear in the review queue
  (`/accounts/unassigned/`). Unassigned spend is excluded from customer views and shown separately.
* Transfers (`ensure_assignment`) end the old assignment at the transfer date, create the new one,
  and re-stamp stored facts from that date only. Older spend keeps its previous owner.
* Accounts that disappear from an organization are marked `missing_since`; their rows and
  assignments stay.
* Publishing rejects a month when another connection already holds rows for the same account and
  day (`OverlappingBillingScope`), so payer + member duplicates are impossible.
* The management account's own charges appear once, as a LINKED_ACCOUNT row equal to its ID. Payer
  totals are sums of member rows; nothing adds a payer total on top.

## Collection

* Baseline: `GetCostAndUsage`, DAILY, grouped by `LINKED_ACCOUNT` and `SERVICE`, metrics
  `UnblendedCost` and `AmortizedCost`. Monthly views aggregate stored facts.
* Each month is fetched with pagination validation (repeated tokens, dates outside the window,
  non-finite amounts, unit mismatches) and published atomically. Failures keep the previous
  snapshot and mark the period `failed`/`partial`.
* Initial import: `HISTORY_MONTHS` (6) completed months plus the current month. Scheduled runs
  refresh the current and previous month; Sunday 00:00 UTC refreshes `RECONCILE_MONTHS` (3); the
  2nd of each month at 00:00 UTC refreshes the full window.
* Six-hour slots (00/06/12/18 UTC) with `SCHEDULE_JITTER_SECONDS` jitter. Job keys include the slot,
  so scheduling is idempotent; manual refreshes coalesce into one job per connection.
* The worker (`manage.py run_worker`) leases jobs with `SELECT … FOR UPDATE SKIP LOCKED`, runs
  `WORKER_CONCURRENCY` threads, excludes connections that already have a leased job, extends
  leases while paging, recovers expired leases and retries with exponential backoff and jitter.
  Progress (completed months) is stored on the job so retries resume.
* Request limits: `MAX_REQUESTS_PER_JOB`, `AWS_REQUEST_INTERVAL_SECONDS`, throttle retries. Each
  period records request and page counts; Cost Explorer bills per request (USD 0.01 in the
  published price list), so the capacity document estimates request cost per cycle.
* Web requests never call AWS. Verification, discovery, import, budget import and cached report
  refreshes are jobs.

## Discovery and permissions

* Verification assumes the role with the connection's external ID, checks
  `sts:GetCallerIdentity` returns the expected account, runs a two-day Cost Explorer probe, then
  tests `organizations:DescribeOrganization` and `budgets:DescribeBudgets` (IAM action
  `budgets:ViewBudget`). Results are stored in `capabilities`.
* Discovery uses `organizations:ListAccounts` (all pages, including empty pages that still carry
  `NextToken`; the `State` field is read before the retired `Status`). Without the permission it
  falls back to `GetDimensionValues LINKED_ACCOUNT` over the history window and labels the
  inventory billing-only: zero-cost accounts cannot be listed that way.
* `deploy/customer-role.yaml` keeps the six Cost Explorer actions and adds two optional
  parameters: `EnableOrganizationsDiscovery` and `EnableBudgetImport` (scoped to
  `arn:aws:budgets::<account>:budget/*`). No administrator, ReadOnlyAccess, workload or IAM write
  permissions are granted. Hourly/resource opt-ins and tag activation stay with the customer.
* Existing customer stacks keep working (the parameters default to `false`); updating the stack
  with the new template and enabling the parameters unlocks discovery and budget import.

## Project allocation

* Account rules allocate exactly from stored facts.
* Tag and cost-category rules need additional scoped AWS queries (daily, grouped by
  `LINKED_ACCOUNT`, filtered by the tag/category values and the customer's accounts). Whole-account
  claims by other projects are excluded from the query, so a dollar is never allocated twice.
* Validation rejects ambiguous rules: overlapping account sets, the same key with overlapping
  values, or different keys over overlapping scopes. Different keys are allowed only over disjoint
  account scopes.
* The remainder is shown as "Unallocated / shared" and reconciles with customer spend for the same
  dates, currency and metric. No proportional splitting is invented.

## Budgets

* Dashboard budgets are evaluated against stored facts with matching scope, month, metric and
  currency. `Not configured` is reported when no amount is effective; never zero.
* Forecast: cached AWS `GetCostForecast` for the scope when available, otherwise a labelled
  completed-day run-rate projection; `Unavailable` when fewer than `RUN_RATE_MIN_DAYS` completed
  days exist or coverage is missing.
* Status order: Over budget, Forecast over budget, At risk (actual ≥ actual threshold), Unverified
  (partial or stale data), No data, Within budget (complete data only), Not configured.
* Portfolio totals only sum customer-level budgets; payer/account/project budgets are compared to
  the parent separately.
* Alerts are in-app only, deduplicated per budget/month/kind/threshold, acknowledgeable.
* Imported AWS budgets are read-only snapshots. Percentage-based (RI/Savings Plans) budgets are
  never shown as currency. Payer roles expose only payer-owned budgets; a member budget-read
  connection imports member-owned budgets without collecting member costs again.

## Access control

Internal roles: readers (authenticated users), operators (`is_staff`), administrators
(`is_superuser`). Readers can view and export; only operators edit connections, assignments,
budgets and rules. `billing/scope.py` resolves every customer/source/account/project selection and
rejects identifiers that do not belong together, for HTML reports, CSV exports, metadata lookups,
saved reports and worker queries. Shared-payer AWS queries always carry a `LINKED_ACCOUNT`
restriction derived from assignments.
