# Backend parity notes — 2026-09-22

## Implemented contract

`billing/parameters.py` is the common report contract for HTML, CSV, saved definitions and worker requests.

- Historical selection remains `date_range`. New values are `last_1_day`, `last_36_months`, `year_to_date` and `current_month`; existing values remain accepted. `this_month` means month to date. Last 1 day is yesterday. Current month includes its final calendar day.
- `future_range` is `none`, `next_1_month`, `next_3_months`, `next_12_months` or `next_18_months`. The future end is the last calendar day of the selected future month. For example, Next 1 month selected during September ends October 31.
- Normalized `start` and `end` are the combined report interval. `historical_end` preserves the historical picker endpoint, including a custom endpoint, while future presets roll when saved definitions reopen. The AWS URL format carries effective start/end rather than this extra local custom-date anchor.
- `keyed_filters` is a JSON string containing up to 20 additional tag/category filters. Each object has exactly `type` (`tag` or `cost_category`), `key`, `values`, `mode` (`include` or `exclude`) and optional boolean `absent`. Values within a key are OR; keyed filters are AND with the existing primary filters and ordinary dimensions. Each key appears once, with 100 values at most, key length 120, value length 1,024 and total additional JSON size 200,000 characters. Existing primary `tag_key`/`tag` and category fields remain supported. Normalized definitions use a stable sorted order.
- Metadata for a specific key excludes that key's selection while retaining other selected keys. Keyed drilldowns retain constraints on other keys. Existing server-side approval inspection validates every additional key in the resulting AWS expression.
- Three-year reports require Monthly granularity and use AWS data rather than the local imported-history shortcut. The UI receives an explanation of AWS's existing multi-year-data prerequisite. No AWS settings are enabled or changed. AWS failures remain incomplete results, not fabricated zero totals.
- The chart payload's `forecast_rows` contains numeric mean/lower/upper values, start and exclusive end dates, customer label and a query-specific `series_id`. Intervals remain separate; bounds from different customer/source requests must not be summed into a portfolio confidence interval.

## Verified native behavior and explicit limits

Live console inspection by the parent agent found that choosing a usage type with Hours units sets `usageAggregate=usageQuantity` while retaining `costAggregate=unBlendedCost`. AWS then presents cost and usage together in overview, graph and breakdown. The dashboard now supports `measure=cost_usage`: a cost result and a simultaneous secondary usage result using identical authorized scope, dates, filters and grouping. Each retains its own units, totals, chart and breakdown. The normalized-units switch affects only the usage result. Pending requests, incomplete status and query IDs combine across both results, so export remains blocked until both are available. The implementation uses two bounded internal report builds without recursion; it does not add USD and usage quantities together. Existing cost-only and usage-only modes remain available. The same observed usage selection left the normalized-units control disabled; the dashboard's normalized-usage API choice remains subject to supported usage and AWS data availability.

Native usage selections can retain forecast dates and `usageAggregate=usageQuantity`. This application currently implements cost forecasting only and rejects future Usage or combined cost-and-usage reports explicitly; it does not call `GetUsageForecast` or claim to show usage forecasts.

The native console prompts to clear Group By before enabling forecasts. This dashboard can retain grouped historical actuals alongside a separate whole-scope customer forecast; this is an application extension, not AWS-native grouped forecasting. The AWS handoff helper rejects grouped future forecasts instead of silently dropping grouping.

Observed tag URL encoding is `dimension.id=TagKey`, with its key in `growableValue.value`, including multiple filter objects for distinct keys. Tag grouping uses `TagKeyValue:<key>`. A selected missing-key value is the empty string, distinct from an ordinary literal `(Unassigned)` value. Backend group unpacking now preserves raw empty keys, and missing rows have a separate display label and `is_absent` flag.

Cost category keys were unavailable in the inspected account, so their native URL encoding could not be verified. The application still supports category keys and values through the documented Cost Explorer API. Category URL import/export returns an explicit unsupported-format message rather than claiming a guessed encoding is verified. Category group values remain opaque; only the documented tag-key prefix is stripped from returned group keys.

Verified live URL enums are `NEXT_MONTH`, `NEXT_3_MONTHS`, `NEXT_12_MONTHS`, `NEXT_18_MONTHS`, `LAST_1_DAY`, `LAST_MONTH`, `LAST_6_MONTHS`, `LAST_12_MONTHS`, `LAST_3_YEARS`, `YEAR_TO_DATE` and `CURRENT_MONTH`. Older aliases such as `NEXT_1_MONTH` remain accepted on import; export uses the observed forms.

The AWS handoff helper carries report filters and effective dates, but local Customer/source selectors are not AWS URL fields. The caller must restrict handoff to a suitable AWS account/view and preserve authorized linked-account containment. A portfolio URL is not evidence that AWS has adopted this application's local customer scope.
