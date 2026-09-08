# Cost Explorer parameter implementation

Captured from the authenticated AWS Cost Explorer console on 8 September 2026. The supplied reference selects March–August 2026, monthly granularity, Service grouping, stacked bars, unblended costs, and forecast visibility. Its historical total was independently reconciled with AWS at USD 44.2233000625.

## Parameter contract

| Console parameter | Dashboard behavior |
| --- | --- |
| chartStyle | Stacked, bar and line charts; all use the same report totals |
| startDate / endDate | Inclusive UTC dates; converted to AWS's exclusive end date |
| historicalRelativeRange | This/last month, last 7/14 days, last 3/6/12 months, custom dates |
| futureRelativeRange | Future end date in a custom report; daily forecasts up to 3 months, monthly up to 18 |
| granularity | Monthly, daily, hourly; hourly requires customer opt-in and last 14 days |
| reportMode | Standard or Compare, with explicit comparison dates or preceding period |
| groupBy | None, customer, service, linked account, region, instance type, usage type, resource, cost category, tag, charge type, availability zone, platform, purchase option, tenancy, database engine, legal entity, billing entity, API operation, payer account |
| filter | All 19 console filters; multiple values within a filter use OR, different filters use AND, with include/exclude matching |
| costAggregate | Unblended, amortized, blended, net unblended, net amortized |
| usageAggregate | Usage quantity; requires one usage type to avoid adding incompatible units |
| useNormalizedUnits | NormalizedUsageAmount for eligible instance usage |
| excludeForecasting | Show/hide AWS forecasts; forecasts appear separately from actual charges |
| showOnlyUntagged | ABSENT expression on the chosen billing tag key |
| showOnlyUncategorized | ABSENT expression on the chosen cost category key |
| reportName | Editable title and shared saved-report library |
| isDefault | Console presentation metadata; no impact on AWS billing queries |
| Filter preference | Browser-local visibility settings; applied filters stay visible |

The dashboard's additional customer selector scopes independent payer/standalone connections. Currency stays USD for AWS reports; imported currencies remain separated in Customer portfolio. The console URL importer supports the supplied unfiltered report link. It rejects unsupported encoded filter formats explicitly; users select those filters through the dashboard rather than silently losing them.

## Data flow and six-hour collection

`parameters.normalize` validates HTML, saved reports and exports. `advanced_explorer.build_report` uses the existing daily account/service imports for simple reports. Other dimensions and advanced metrics produce parameter-specific `ExplorerQuery` records per customer. The existing cron worker calls the allowed read-only AWS APIs, follows pagination, and atomically updates cached responses. Web requests never wait on AWS. First requests start within the worker's one-minute queue schedule; recently used reports and saved reports refresh on the six-hour schedule. AWS's billing publication delay still applies.

Cache identity includes customer, role/external-ID fingerprint, operation and canonical parameters. Changing the chart style reuses the same data query. Active cache means opened within seven days; unused queries expire after 30 days. Saved report definitions persist and are resolved every six hours, including relative date ranges. Failed refreshes retain prior results with a visible warning. Missing customer responses produce an incomplete-report banner and block CSV export. Zero and missing amounts differ; negative credits and exact decimal amounts are preserved. Requests are processed in batches of 100 so large queues can span multiple minutes.

Comparison uses identical filters and metrics in both periods, with calendar comparisons for whole months. Forecast requests start today; actuals stop before today in a future report, so actual and forecast dates do not overlap. Prediction intervals remain per customer; adding separate confidence intervals would not form a valid portfolio interval.

## Minimal customer setup

One CloudFormation IAM role; exact collector principal plus a unique external ID. Six read-only actions:

- ce:GetCostAndUsage
- ce:GetDimensionValues
- ce:GetTags
- ce:GetCostCategories
- ce:GetCostForecast
- ce:GetCostAndUsageWithResources

No customer software, access keys, administrator policy, infrastructure inventory or workload permissions. Existing customers update their existing stack with the current template. Resource reports through the documented resource API require EC2-Instances, enabled resource data and the last 14 days. Hourly data also needs opt-in. Tags must be activated as cost allocation tags, categories must exist, and forecasts need sufficient AWS history. The dashboard reports AWS prerequisite errors; it does not enable paid granular features or modify customer billing preferences.

## Sources

- [GetCostAndUsage](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html)
- [GetDimensionValues](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetDimensionValues.html)
- [GetCostAndUsageWithResources](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsageWithResources.html)
- [GetCostForecast](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostForecast.html)
- [Cost Explorer filters](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-filtering.html)

## Graphify audit

Graphify mapped the original report controls in `templates/billing/explorer.html` to `billing/explorer.py`, `billing/reporting.py`, `billing/models.py`, `billing/collector.py`, `sync_costs.py` and `deploy/customer-role.yaml`. This showed why richer dimensions need parameter-specific AWS queries rather than deriving region/tag/resource data from the existing account/service rows. Query terms selected from the graph vocabulary were explorer, parameters, report, cost, collector, permission and sync. The initial graph contained 199 nodes and 387 edges. Initial diagnostics reported 85 unresolved references and two collapsed relation pairs; these are graph limitations, not verified application dependencies. The local interactive graph and audit report live in `graphify-out/` and are excluded from production images.
