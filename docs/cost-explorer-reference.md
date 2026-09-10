# AWS Cost Explorer behavior reference

Observed in the signed-in AWS Cost Explorer UI on 2026-09-10. This is a functional mapping, not a copy of AWS implementation code. Live billing data and identifiers are excluded from this document.

## Standard reports

The console supplies date presets and absolute dates, daily/monthly/hourly granularity, a single grouping selection in the observed UI, bar/line/stacked charts, summary totals and averages, a searchable breakdown table, CSV download, and a report library.

## Compare reports

The reference console defaults to month-over-month: previous complete month is selected and the preceding complete month is the baseline. Custom comparison selects two months. The overview shows baseline cost, selected cost, absolute difference and percentage change. The graph places the periods side by side, grouped by the selected dimension. The breakdown shows every group in either period, missing groups as zero only after both responses complete, difference, percentage and a driver drilldown. Zero baseline has no percentage.

The observed URL uses reportMode=COMPARE, comparisonStartDate/comparisonEndDate for the selected period, baselineStartDate/baselineEndDate for the previous period, and compareRelativeRange=MONTH_OVER_MONTH for the relative preset. Standard report URLs use startDate/endDate and historicalRelativeRange. The UI's end dates are inclusive; CE API TimePeriod.End is exclusive.

The dashboard preserves rolling month-over-month and preceding-period presets when reports are saved and reopened. Custom dates stay fixed. Its comparison table supports search, sorting and a cost drilldown that preserves filters. AWS-native driver facts are available through the separately approved GetCostComparisonDrivers API; they require two complete months and stable customer account ownership. Missing driver access does not block the cost comparison or its CSV.

## Filters and grouping

The 19 observed filters are Service, Linked account, Region, Instance type, Usage type, Usage type group, Resource, Cost category, Tag, Charge type, Availability zone, Platform, Purchase option, Tenancy, Database engine, Legal entity, Billing entity, API operation and Payer account. Each supports inclusion or exclusion and multiple values. Values in one filter are OR; distinct filters are AND. Empty selection is a no-op, including exclude mode. AWS lists linked-account names alongside IDs. Choices follow the date range and other filters. Clear selection, clear all, filter visibility preferences and search must not alter unrelated selections.

Standard dimension filter URLs encode an array of objects with dimension.id, operator INCLUDES or EXCLUDES, and values containing value and optional displayValue. An observed LinkedAccount exclusion maps to Not(Dimensions(Key=LINKED_ACCOUNT, Values=[selected ID])). It must never also narrow the authorized scope to that same excluded account. Display labels are not identifiers.

The standard group inventory maps to current GetCostAndUsage dimensions, plus tags and cost categories. UsageTypeGroup is a filter, not an AWS-supported grouping. Resource grouping requires GetCostAndUsageWithResources. Customer grouping is a dashboard extension based on authorized customer ownership.

## Metrics and optional data

Unblended, amortized, blended, net unblended and net amortized are separate cost metrics. Usage quantities require a usage-type/unit scope; normalized usage is a distinct metric. Forecasts cover future dates and are separate from actual charges. Untagged and uncategorized filters use ABSENT. Tags/categories/resource/forecast capabilities remain subject to customer approval, least-privilege IAM and AWS data availability; hourly/resource data needs AWS opt-in. Missing permissions must have actionable messages and never produce an HTTP 500 or fake zero cost.

The current customer role has ce:GetCostAndUsage and ce:GetDimensionValues. Live probes accepted all 15 ordinary grouping dimensions offered by this dashboard, all five cost metrics, and 16 ordinary metadata dimensions. Optional APIs have not been granted. The AWS GetCostAndUsage documentation's prose list of group dimensions is narrower than the live API validation list; retain this distinction in evidence.

## Implementation and verification map

- billing/parameters.py: normalizes and validates all report controls; constructs AWS requests; imports console URLs.
- billing/scope.py and billing/access.py: customer/source containment, effective-dated ownership and user authorization.
- billing/advanced_explorer.py: query selection, consistent actual/comparison filters, totals, charts, drilldown URLs, capability feedback.
- billing/query_cache.py: durable six-hour query cache, optional capability approval, paginated worker requests, error/cooldown behavior.
- billing/web.py: authenticated report/metadata/status/CSV/save/import endpoints.
- templates/billing/parameters.html: all parameter fields and feature prerequisites.
- templates/billing/comparison.html: baseline/selected/difference summary, paired chart and comparison breakdown.
- billing/comparison_drivers.py and templates/billing/comparison_drivers.html: optional paginated AWS driver facts, exact month/account scope, permission feedback and export.
- static/app.js: charts, legend/keyboard interaction, date presets, linked metadata, state preservation and current-form saving.
- billing/tests/test_explorer_controls.py: include/exclude conservation for every filter, all group/metric combinations, exact exclusion regression, ownership restrictions, export consistency, unavailable capabilities, usage, dates, import and comparison graphs.

## Acceptance rules

For identical authorized scope, dates and metric, include(X) + exclude(X) equals the unfiltered total within precision tolerance. Both comparison periods must carry the same user filter expression and the correct ownership restriction for their own dates. Charts, summary cards, detail rows, saved reports and CSV must use the same normalized contract. Incomplete sources cannot masquerade as complete totals; pending onboarding is explicitly excluded from connected-customer exports. Web requests must never acquire customer AWS credentials.

Historical queries and metadata use the union of assignments overlapping the report periods, then split requests at ownership boundaries. A completed empty result is explicitly zero; missing or unavailable responses remain incomplete.

## Verification limits

Automated tests use independent synthetic ledgers and mocked AWS responses for the full filter/group/metric matrix, pagination, optional APIs, ownership transitions and browser interactions. Live AWS acceptance probes cover ordinary dimensions/metrics; optional customer permissions and granular-data opt-ins must be verified separately before claiming live parity. Graphify documents code relationships and is not a substitute for runtime tests or production reconciliation.

AWS references: [Cost and usage](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html), [metadata dimensions](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetDimensionValues.html), [cost comparison drivers](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostComparisonDrivers.html).
