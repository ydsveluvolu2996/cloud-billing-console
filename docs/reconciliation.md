# Per-customer reconciliation

The same command supports every customer, payer, standalone account and historically assigned shared-payer account. It never calls AWS or invents a reference total. Only an authorized administration/collector runtime may record a result. Synthetic tests are distinct from actual customer reconciliation.

Obtain an approved AWS Cost Explorer reference for the exact UTC dates, unblended/amortized metric, one currency, linked accounts and optional service filter. Include credits/refunds. Record estimated/final status and evidence provenance. If ownership changed during the interval, select only that customer's effective dates for each account; the output lists inclusive starts and exclusive ends. A whole-payer export containing another customer's accounts is rejected rather than silently accepted. Use one row per authorized account total, including explicit zero rows when the reference supports zero.

The CSV contract is:

```csv
account_id,amount,currency,metric,start,end,timezone,estimated,services,credits_refunds
000000000001,0.0000000000,USD,unblended,2026-08-01,2026-08-31,UTC,false,,included
```

This zero-prefixed account is a development example only. Required columns are account ID, amount, currency, metric, start, end, timezone and estimated. `services` is a pipe-separated set matching `--services` (comma-separated CLI); omission means no service filter. Credits/refunds default to included and any excluded reference is rejected. Reject duplicate accounts, non-finite decimal values, unlike currencies, mismatched dates/metrics/timezones and out-of-scope accounts.

```sh
RUNTIME_ROLE=admin python manage.py reconcile_customer \
  --customer APPROVED_CUSTOMER_UUID \
  --reference-csv /restricted/approved-reference.csv \
  --reference-evidence APPROVED_EVIDENCE_REFERENCE \
  --start 2026-08-01 --end 2026-08-31 --metric unblended --currency USD \
  --actor APPROVED_OPERATOR --output /restricted/reconciliation.json
```

Add `--source SOURCE_UUID` for a particular approved connection, `--services 'Amazon EC2,Amazon S3'` for those exact services, and **always** `--synthetic` for development fixtures. A source-only or synthetic result cannot satisfy whole-customer rollout readiness. No files containing actual customer costs belong in Git.

The result preserves decimal strings, a default absolute tolerance of USD/currency 0.00000001, reference/difference amounts, coverage, estimates and ownership windows. Missing data is not zero: published monthly periods must cover the entire requested range and there must be evidence of the selected currency. Positive/negative values and zero-spend inventory are checked independently. A mismatched result is stored for investigation and printed as such; inspect `passed` in the output rather than assuming process completion means a match.

Readiness requires a passed, non-synthetic whole-customer result whose connection and ownership version map still matches every current connection. Connection changes or ownership transfers invalidate that evidence. Reconcile current and representative closed months after deployment, including payer-own spend, members, standalone accounts, shared scopes, credits/adjustments, zero and missing data. Record real reconciliation independently for each new customer; no customer in this build is claimed reconciled with AWS.
