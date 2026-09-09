# Capacity evidence — synthetic load test

**These are simulated results.** No live AWS account was called; the collector ran against an
in-process fake Cost Explorer that returns paginated pages instantly. They show how the application
and database behave at the target data volume, not what the production EC2 instance can sustain.
They do **not** demonstrate that the current t3.small (2 vCPU, 2 GiB) is sufficient.

## Workload

`python manage.py synthetic_load --customers 100 --accounts 2000 --months 7 --services 6 --readers 8 --requests 160 --collect-sources 100`
(raw output: `docs/capacity/synthetic-load-results.json`, run 2026-09-08).

| Dimension | Value |
| --- | --- |
| Customers | 100, one payer connection each |
| Linked accounts | 2,000 (20 per payer, management account included) |
| History | 7 months, 192 days, 6 services per account |
| Cost rows | 2,304,000 (`billing_cost` 958 MB including indexes) |
| Budgets | 100 customer budgets, evaluated for two months (200 evaluations) |

## Hardware and software assumptions

| Item | Value |
| --- | --- |
| Host | Sandbox VM: 4 vCPU Intel Xeon 2.80 GHz, 16 GiB RAM, local disk |
| Database | PostgreSQL 16.13 on the same host, default configuration |
| Application | Django test client in-process (no gunicorn, Caddy or TLS); Python 3.12 |
| Concurrency | 8 reader threads sharing the GIL, one PostgreSQL connection each |

Production runs gunicorn with 2 workers × 2 threads behind Caddy on a t3.small with PostgreSQL in a
container on the same instance and CPU credits in standard mode. Expect lower throughput there.

## Results

### Simulated collection cycle (100 payers, current + previous month)

| Metric | Value |
| --- | --- |
| Wall time, single worker thread | 167.5 s |
| Per payer (mean / p95 / max) | 1.67 s / 1.83 s / 1.92 s |
| Requests per payer per cycle | 5 (1,000-row pages) |
| Rows published | 468,000 |
| Peak Python memory | 77.5 MB |
| Estimated Cost Explorer request cost | 100 payers × 5 requests × USD 0.01 = USD 5.00 per cycle, USD 20 per day at four cycles; the monthly 7-month reconciliation costs about USD 17.50 once |

Live AWS adds network latency (0.5–2 s per request) and per-account throttling, so a live cycle for
100 payers is expected to take 15–40 minutes with `WORKER_CONCURRENCY=3`; well within the six-hour
slot, and the jitter (`SCHEDULE_JITTER_SECONDS`, default 900) spreads the start times. Page sizes
from AWS are smaller than the fake client's, so request counts per payer will be higher in
practice (roughly 2–6 requests per month per payer); `MAX_REQUESTS_PER_JOB` caps the exposure.

### Job queue fairness

202 jobs scheduled for one slot in 1.1 s; leasing took 5.6 ms per job; all 100 payers were leased
once before any payer's second job (per-connection exclusion). Budget evaluation for 100 budgets ×
2 months took 27.5 s.

### Dashboard latency (single reader, warm cache)

| Page | p50 | p95 | SQL queries (before N+1 fix → after) |
| --- | --- | --- | --- |
| `/` Cost Explorer, last 6 months, all customers | 346 ms | 8.4 s | 42 → 25 |
| `/portfolio/` Cost overview, current month, all customers | 4.3 s | 5.9 s | 226 → 27 |
| `/overview/` Portfolio overview | 555 ms | — | 216 → 18 |
| `/customers/<id>/` Accounts tab | 25 ms | 272 ms | 19 → 10 |
| `/customers/<id>/?tab=budgets` | 23 ms | 26 ms | 13 |
| `/budgets/` | 73 ms | — | 9 |
| `/onboarding/` | 308 ms | — | 305 → 7 |

Query counts after the fix were measured on a 100-customer / 2,000-account data set with one month of
history; latencies in the table are from the full 2.3 M-row run before the fix (the N+1 removal
changes query count, not the heavy aggregate queries). The Cost overview's cost comes from about
ten aggregate scans over the selected range across all customers (totals, previous period, daily
series, service and account breakdowns, month-to-date and completed-day sums). Customer-scoped pages
use the `(customer, currency, day)` index and stay fast.

### Concurrent readers

8 readers, 160 requests over a mix of the pages above: 1.9 requests/s, p50 761 ms, p95 23.8 s,
max 29.4 s, peak Python memory 30 MB. The tail is dominated by concurrent portfolio-wide aggregate
queries competing for 4 vCPUs plus the Python GIL in one process; gunicorn's separate worker
processes would remove the GIL contention but not the database CPU contention.

## Conclusions and recommendations

* Collection, scheduling, budget evaluation and all customer-scoped pages are comfortably within
  budget for 100 customers and 2,000 accounts.
* Portfolio-wide pages over the full fact table are the bottleneck at 2.3 M rows: several seconds
  per request and poor tail latency under concurrency. Options, in order of effort: keep the default
  Cost overview range at the current month (already the default), add PostgreSQL parallel query
  tuning (`max_parallel_workers_per_gather`), or add a maintained monthly rollup table per
  customer/account/service that the portfolio pages read instead of daily facts (recommended
  follow-up before the estate reaches this volume).
* Storage: about 1 GB per 2.3 M rows. Seven months for 2,000 accounts × 6 services fits the 30 GiB
  volume; the same accounts with 20 distinct services would triple that.
* The current t3.small has 2 vCPU and 2 GiB; PostgreSQL alone needs shared buffers sized for a
  1 GB table to keep the portfolio queries in memory. Measure on the target instance with
  `synthetic_load` against an isolated database before relying on it, or move PostgreSQL to RDS /
  a larger instance.

## Reproducing

```bash
createdb loadtest   # isolated database; the command refuses databases with real customers
DEBUG=true DB_HOST=... DB_NAME=loadtest DB_PASSWORD=... python manage.py migrate
DEBUG=true DB_HOST=... DB_NAME=loadtest DB_PASSWORD=... python manage.py synthetic_load --customers 100 --accounts 2000 --months 7 --services 6
```

Generated data is deleted at the end unless `--keep` is given. Adjust `--services`, `--months`,
`--readers` and `--requests` to model your estate; the JSON output lists every measured figure.
