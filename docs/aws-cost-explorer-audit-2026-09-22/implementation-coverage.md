# Cost Explorer implementation coverage

The [live inventory](live-ui-inventory.md) records 66 control IDs observed in Chrome. These IDs describe controls and flows, not a claim that every AWS-only service has a public API equivalent. No customer amounts, identifiers, credentials or screenshots are included in this audit.

| Inventory | Billing console implementation | Difference or prerequisite |
| --- | --- | --- |
| W01–W04 | Report title, closable/resizable parameters panel, scoped recent history, Save dialog and report library | Recent history is stored in the user's session and invalidated when authorization changes. |
| W05–W13 | Seven prepared report shortcuts and an explained Amazon Q handoff | Q runs in the user's separately authorized AWS session. Prepared reports are deterministic shortcuts, not AI responses. Database shortcuts name RDS/DynamoDB; compute shortcuts name EC2. Comparison shortcuts use complete months. |
| W14, V01–V09 | Overview, bar/line/stacked charts, series toggles, full totals, CSV, search, sorting, pagination, preferences, wrapping | Cost and usage can appear together on separate scales. Page sizes are usable numeric choices; the AWS session rendered broken `_Plural_Items_` labels. |
| T01–T08 | Standard/Compare, two-calendar draft date picker, all observed past/future ranges, rolling saved ranges, hourly/daily/monthly | Hourly/resource data and extended history require existing AWS opt-ins. Cost forecasts support the documented daily/monthly horizons. Usage forecasts are not implemented and future usage requests show an explicit error. |
| G01–G04 | Every observed group dimension, plus the existing Customer grouping; keyed tag/category selection | Resource data through the public API is limited to EC2 and the supported short window. The inspected AWS console exposed more service choices. |
| F01–F09 | All 19 filter families, searchable include/exclude drafts, selection counts, chips, clear/apply/cancel, visibility preferences, multiple tag/category keys | Keys and values are loaded from authorized customer data. Category keys were absent in the inspected account; unverified category URL encodings are rejected, while category API filtering remains supported. |
| A01–A06 | Five cost measures, cost/usage/both, normalized usage, forecasts, missing-tag/category filters | Different usage units cannot be combined. Missing keys are distinct from literal text values. Additional keyed filters obey the same approval rules. |
| C01–C09 | Relative/custom comparison, summary/difference/percent, paired charts, sortable table, existing AWS driver narratives and per-group drilldown | Driver APIs retain their complete-calendar-month prerequisite; generalized previous-period comparisons remain an application extension. |
| L01–L04 | Library search, six sortable metadata columns, create/open, row selection, duplicate, rename, archive/restore | Archive is recoverable. Every mutation enforces write scope and CSRF protection. AWS's five built-in examples are not copied as customer-owned saved records. |
| L05–L07 | Create report lists all five observed report types. Cost and usage opens locally; four Reservation/Savings Plans types open their verified AWS console workspaces. | Separate AWS sign-in and permissions apply, and local customer filters are not transferred. Their utilization/coverage workspaces and target-setting flows are not replicated locally. |

## Correctness and access fixes

- Metadata uses the historical portion of a forecast report instead of converting a long Monthly forecast into an invalid Daily request.
- Changing keyed dimensions clears stale selections and cancels outdated metadata responses. Absence selections invalidate metadata and remain visible even if preferences hide that filter.
- Chart changes, Clear all and Save preserve the current form draft rather than reverting to the last rendered URL.
- Cost and usage CSV sections keep separate units and formula-safe labels. Exports wait until every required actual, comparison and forecast result is available.
- Forecast means are plotted with distinct series; independent customer/source confidence intervals are never added into a false portfolio interval.
- AWS console links require a single connection, intersect linked-account restrictions, and reject date ranges with changing ownership that an AWS URL cannot represent.

## Verification

The local full suite passed 405 tests (18 PostgreSQL-only skips); 47 final parity, library and workspace regressions passed after that run. CI runs PostgreSQL 17/18 with Python 3.12/3.14 and tests the deployable images. The workflow also checks browser-script syntax and date/forecast transformations.

Chrome checks on synthetic data covered desktop layout, paging (20 + 5 rows), search without changing totals, preference cancellation, applying historical dates, filter cancellation, report duplication/archive/restore, and a 390-pixel responsive parameters panel with no document overflow. Application browser logs showed no application-script errors; unrelated browser-extension errors were excluded. The AWS reference report was restored after inspection.
