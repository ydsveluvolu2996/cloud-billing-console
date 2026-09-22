# Current Cost Explorer implementation audit

Date: 2026-09-22. Baseline requested for this audit: deployed `c22c1d4`, working branch `codex/cost-explorer-parity`.

This is a source audit, not a fresh AWS console verification. The live AWS inventory is being collected separately. Evidence below uses repository-relative files and lines and contains no customer billing values or identifiers. Existing references describe observations from an earlier session; they do not establish current live parity (`docs/cost-explorer-reference.md:3`, `docs/cost-explorer-reference.md:50`). No production code was edited during this audit.

## Overall finding

The core report contract is already broad: 19 filters, include/exclude matching, 20 grouping choices including None and the local Customer extension, five cost metrics, usage and normalized usage, Standard/Compare, three granularities, custom dates and seven relative historical ranges. The main visible omissions are breakdown preferences/pagination, recent reports and a report management page, relative forecast controls, and assistant prompt actions. Several frontend state transitions and metadata validation paths need correction before calling the existing controls complete.

The query path is split deliberately. Simple standard unblended/amortized daily/monthly service/account/customer reports use imported local billing rows; other selections use durable AWS requests (`billing/advanced_explorer.py:14`, `billing/advanced_explorer.py:79`). Advanced data is queued and collected by the worker, with cached results retained on refresh failure (`billing/query_cache.py:45`, `billing/query_cache.py:163`).

## Implemented controls and exact behavior

| Area | Current implementation | Evidence |
| --- | --- | --- |
| Report title | Editable `report_name`, 120-character limit; shown as page heading. Default is “Cost report”. | `billing/parameters.py:46`, `billing/parameters.py:73`, `templates/billing/parameters.html:47`, `templates/billing/explorer.html:3` |
| Standard/Compare | Select in parameter sidebar. Switching into Compare in JS chooses month-over-month. Direct server parameters without comparison dates use preceding period. | `templates/billing/parameters.html:9`, `static/app.js:393`, `static/app.js:407`, `billing/parameters.py:48` |
| Historical dates | Custom inclusive UTC start/end; This month, Last month, Last 7 days, Last 14 days, Last 3/6/12 complete months. Default is Last 6 months. Editing either date changes the range to Custom. | `templates/billing/parameters.html:10`, `billing/parameters.py:53`, `static/app.js:384`, `static/app.js:405` |
| Date validation | Start must not be future, selected duration at most 731 days, end at most 18 months ahead. API requests convert inclusive end to exclusive end. | `billing/parameters.py:62`, `billing/parameters.py:134` |
| Forecast dates | A future custom end requests forecast, Standard cost only; daily forecasts limited to three months. No independent `future_range`/`futureRelativeRange` field or forecast preset picker exists. | `billing/parameters.py:46`, `billing/parameters.py:112`, `templates/billing/parameters.html:10` |
| Comparison dates | Month-over-month rolling, preceding-period rolling, or custom dates. Whole-month preceding periods stay calendar aligned; other ranges use equal day counts. Custom ranges need not have equal duration. | `templates/billing/parameters.html:12`, `billing/parameters.py:48`, `billing/parameters.py:98` |
| Granularity | Monthly, Daily, Hourly. Hourly requests use UTC timestamps and require both selected and comparison dates within 14 days. | `templates/billing/parameters.html:13`, `billing/parameters.py:96`, `billing/parameters.py:111`, `billing/parameters.py:138` |
| Group by | One selected dimension. Tag/category grouping has a key input, datalist and explicit Load available keys action. Resource grouping uses resource API. | `templates/billing/parameters.html:15`, `billing/parameters.py:22`, `billing/parameters.py:83`, `billing/parameters.py:142` |
| Customer/source scope | Authorized Customer selector is a local extension. Existing `source` is carried as a hidden field; there is no source picker in this sidebar. | `templates/billing/parameters.html:5`, `templates/billing/parameters.html:17`, `billing/advanced_explorer.py:97` |
| Filter values | All 19 filters have independent Include/Exclude, search, multi-checkbox values, Load available values, exact manual value entry and Clear selection. Values within one filter are OR; dimensions are AND; exclusion wraps the dimension expression in Not. | `templates/billing/parameters.html:20`, `billing/parameters.py:34`, `billing/parameters.py:118` |
| Filter preferences | Browser-local visibility preferences. Value-selected filters stay visible. No preference dialog with Apply/Cancel; changes apply immediately. | `templates/billing/parameters.html:18`, `static/app.js:463` |
| Clear/reset | Clear all clears the 19 values and the two absence switches while retaining applied dates/group/metric/scope. Reset report navigates to `/`. | `billing/advanced_explorer.py:233`, `templates/billing/parameters.html:16`, `templates/billing/parameters.html:49` |
| Metadata | Authenticated, asynchronous dimension/tag/category endpoints. Choices exclude their own filter and include other selections and ownership windows. Account metadata can display descriptions beside IDs. Returned selections are retained when reloading available values. | `billing/web.py:254`, `billing/web.py:268`, `billing/web.py:299`, `static/app.js:415`, `static/app.js:440` |
| Cost metric | Unblended, Amortized, Blended, Net unblended, Net amortized; each maps to the matching AWS metric. | `billing/parameters.py:23`, `billing/parameters.py:136`, `templates/billing/parameters.html:39` |
| Usage | Separate Costs/Usage quantity select. Usage requires exactly one included usage type or usage type group; mixed returned units are rejected. Cost metric control remains visible while usage is selected. | `templates/billing/parameters.html:38`, `billing/parameters.py:89`, `billing/advanced_explorer.py:52` |
| Normalized units | Checkbox chooses `NormalizedUsageAmount`; only valid with Usage. There is no frontend eligibility-dependent disabling. | `templates/billing/parameters.html:43`, `billing/parameters.py:91`, `billing/parameters.py:136` |
| Untagged/uncategorized | Independent checkboxes emit `ABSENT`, either for a selected key or all keys. Cannot combine with explicit values in the same filter. Optional approval gates apply. | `templates/billing/parameters.html:41`, `billing/parameters.py:85`, `billing/parameters.py:121`, `billing/query_cache.py:66` |
| Show forecast | Checkbox defaults enabled. Only has a data effect when end is future. Actuals stop before today and forecast begins today, with 80% intervals retained per customer. Forecast appears in a separate table, not the main graph. | `billing/parameters.py:75`, `billing/advanced_explorer.py:118`, `billing/advanced_explorer.py:132`, `billing/advanced_explorer.py:168`, `templates/billing/comparison.html:10` |
| Overview | Total, average per period, group count and selected metric/unit. Explicit incomplete/pending/availability messaging; returned empty advanced reports display known zero. | `templates/billing/explorer.html:4`, `templates/billing/explorer.html:10`, `billing/advanced_explorer.py:177` |
| Chart | Bar, Line and Stacked; top nine groups plus Others by absolute magnitude. Negative credits supported. Tooltip handles pointer, focus and keyboard navigation. Responsive SVG uses patterns/markers as well as color. | `billing/advanced_explorer.py:63`, `billing/explorer.py:78`, `templates/billing/explorer.html:11`, `static/app.js:235` |
| Legend | Per-series show/hide, without changing table or report totals. Standard tooltip labels a partial visible total. Visibility lives only in memory and is lost on navigation. No show/hide-all or legend-wide collapse control. | `templates/billing/explorer.html:11`, `static/app.js:240`, `static/app.js:320`, `static/app.js:368` |
| Standard breakdown | Every row rendered in one table, group total and one column per period, pinned total row markup, search all row text, per-column ascending/descending sort, row link to add the group as a filter. | `templates/billing/explorer.html:14`, `static/app.js:75`, `static/app.js:131`, `billing/advanced_explorer.py:193` |
| Comparison | Previous/selected/change KPIs, percent when baseline nonzero, side-by-side graph, searchable/sortable union-of-groups table and View costs drilldown. The separate comparison chart is always bar regardless of selected standard chart style. Missing groups become zero only after complete responses. | `billing/advanced_explorer.py:146`, `billing/advanced_explorer.py:164`, `billing/advanced_explorer.py:208`, `templates/billing/comparison.html:2` |
| AWS comparison drivers | Separate optional API and presentation. Requires two whole calendar months and stable ownership. Absence of driver approval does not block core comparison/export. | `billing/comparison_drivers.py:9`, `billing/advanced_explorer.py:183`, `templates/billing/explorer.html:13` |
| CSV | Complete normalized report exported with exact decimal text, all rows/periods, comparison, available drivers, forecast, basis/snapshot/scope/warnings. Incomplete advanced reports return 409 and UI disables export. Search and legend changes do not limit export. | `billing/web.py:154`, `templates/billing/explorer.html:3`, `templates/billing/explorer.html:14` |
| Save/open report | Sidebar library links, staff-visible Save current report, server permission check, normalized current form state copied on save, new record each save. Stored relative ranges resolve again when opened. | `templates/billing/parameters.html:47`, `static/app.js:470`, `billing/web.py:336`, `billing/models.py:573` |
| AWS URL import | Validates HTTPS AWS console URL without fetching it; imports known dates/metrics/chart/switches, ordinary dimension include/exclude filters and at most one group. Tag/category encoded filters are explicitly rejected. Unknown relative-date values can fall back to absolute input dates. | `billing/parameters.py:157`, `billing/parameters.py:174`, `billing/parameters.py:185`, `templates/billing/explorer.html:15` |
| Progress/refresh | Worker queue status polling every five seconds, auto-reload only when form unchanged, manual refresh for staff, six-hour cache scheduling. Metadata has its own polling and generation checks. | `static/app.js:415`, `static/app.js:476`, `billing/query_cache.py:94`, `billing/web.py:100` |

## Full filter and grouping inventory

Every filter below is rendered through the same filter template. Ordinary values are capped at 100 values per filter and 1,024 characters each (`billing/parameters.py:34`). There is one selected tag key and one selected category key, so multiple distinct tag keys or category names cannot be composed simultaneously through this form (`billing/parameters.py:46`, `templates/billing/parameters.html:24`).

| Filter | AWS key/type | Grouping available |
| --- | --- | --- |
| Service | `SERVICE` | Yes |
| Linked account | `LINKED_ACCOUNT` | Yes |
| Region | `REGION` | Yes |
| Instance type | `INSTANCE_TYPE` | Yes |
| Usage type | `USAGE_TYPE` | Yes |
| Usage type group | `USAGE_TYPE_GROUP` | No |
| Resource | `RESOURCE_ID` | Yes; EC2 service and resource prerequisites |
| Cost category | `CostCategories` with selected Key | Yes; category key |
| Tag | `Tags` with selected Key | Yes; tag key |
| Charge type | `RECORD_TYPE` | Yes |
| Availability zone | `AZ` | Yes |
| Platform | `PLATFORM` | Yes |
| Purchase option | `PURCHASE_TYPE` | Yes |
| Tenancy | `TENANCY` | Yes |
| Database engine | `DATABASE_ENGINE` | Yes |
| Legal entity | `LEGAL_ENTITY_NAME` | Yes |
| Billing entity | `BILLING_ENTITY` | Yes |
| API operation | `OPERATION` | Yes |
| Payer account | `PAYER_ACCOUNT` | Yes |

Inventory evidence: `billing/parameters.py:10`, `billing/parameters.py:21`, `billing/parameters.py:22`, `billing/parameters.py:118`, `billing/parameters.py:142`. None and Customer are additional group choices. The existing reference explicitly distinguishes usage-type-group filtering from grouping (`docs/cost-explorer-reference.md:23`).

## Gaps against the supplied live-inspection checklist

These are source-confirmed missing affordances. Their precise AWS labels, option values, defaults and disabled states should be supplied by the live inspection before implementation.

| Missing or partial feature | Current boundary | Suggested implementation location |
| --- | --- | --- |
| Breakdown pagination and page size | Table renders all rows; generic `paginate()` is not used by Explorer. | `templates/billing/explorer.html:14`, `templates/billing/comparison.html:9`, `static/app.js:75`, `billing/web.py:35` |
| Breakdown display preferences | No visible-columns, page-size, wrap-lines, density, striped-row or sticky-column controls/state. | `templates/billing/explorer.html:14`, `templates/billing/comparison.html:9`, `static/app.js:75` |
| Breakdown date/metric transpose or alternative table arrangement | Fixed group rows and period columns. Confirm whether offered by current AWS UI. | `templates/billing/explorer.html:14` |
| Recent reports | No report history model, last-opened timestamp, recent list or recent-report selection control. | `billing/models.py:573`, `billing/web.py:350`, `templates/billing/parameters.html:47` |
| Reports landing/library management | Library is an unpaginated sidebar list. No report list route, search, rename, delete, overwrite, duplicate or selection actions. | `config/urls.py:33`, `billing/web.py:338`, `billing/models.py:580`, `templates/billing/parameters.html:47` |
| Relative forecast picker | Future end date is a custom absolute date; no forecast “None / N months” field. Importer never reads `futureRelativeRange`. | `billing/parameters.py:46`, `billing/parameters.py:157`, `templates/billing/parameters.html:10` |
| Forecast graph/overview integration | Forecast is a separate per-customer table. The graph and overview totals only use actual rows. | `billing/advanced_explorer.py:139`, `billing/advanced_explorer.py:168`, `templates/billing/comparison.html:10` |
| Amazon Q suggested prompts/actions | No assistant panel, suggested prompt UI, prompt action endpoint or assistant integration in the Explorer page. Native comparison drivers are the closest existing explanation feature. | `templates/billing/explorer.html:1`, `templates/billing/parameters.html:1`, `config/urls.py:31`, `billing/comparison_drivers.py:9` |
| Enhanced filter selection affordances | Search and individual checkboxes exist; no select-all matching values, explicit Apply/Cancel inside a filter, or selected-value chips outside expanded filters. | `templates/billing/parameters.html:21`, `static/app.js:431` |
| Multiple tag/category filters | Contract stores only one key and value list for each family. Need live confirmation before expanding the data model. | `billing/parameters.py:46`, `billing/parameters.py:121` |
| Current-form chart changes | Chart styles navigate to precomputed links. They preserve applied parameters but not unapplied sidebar edits. | `billing/advanced_explorer.py:230`, `templates/billing/explorer.html:11` |
| Complete URL roundtrip | Tag/category filter import and keyed grouping cannot be reconstructed; unknown future-relative selection is ignored. | `billing/parameters.py:165`, `billing/parameters.py:185` |

## Concrete defects and edge cases found

1. **Valid long monthly forecasts cannot load metadata.** Metadata overwrites granularity with daily, normalizes the still-future end date, and only afterward clamps end to today. A monthly report ending more than three months ahead is valid in the report contract but rejected as a daily forecast during metadata loading. Normalize only the actual metadata date range, or preserve an appropriate granularity while validating. Evidence: `billing/web.py:270`, `billing/web.py:274`, `billing/web.py:275`, `billing/parameters.py:114`.

2. **Tag/category grouping changes reuse stale keys and outstanding responses.** The group selector change handler only hides/shows the key area; it retains `group_key`, the datalist and its in-flight metadata generation. Switching Tag to Cost category can retain a tag key or allow a late tag-key response to populate category choices. Clear or validate the key when its grouping family changes and invalidate that request. Evidence: `static/app.js:408`, `static/app.js:455`, `static/app.js:458`.

3. **Absence switches do not invalidate metadata requests.** The invalidation listener handles date/customer/source and descendants of filter boxes. Untagged/uncategorized checkboxes live outside those boxes. Values loaded under one absence scope can remain or arrive after that scope changes. Include these expression-changing fields in invalidation. Evidence: `templates/billing/parameters.html:41`, `static/app.js:458`.

4. **Absence-only filters can be hidden despite the preference promise.** “Applied filters always stay visible” is implemented using checked inputs inside each filter box. Absence checkboxes are in the separate Advanced section, so an applied untagged/category-absence filter has no selected value inside the filter box and can stay hidden by preference. The server likewise expands only nonempty values. Evidence: `templates/billing/parameters.html:18`, `templates/billing/parameters.html:41`, `static/app.js:466`, `billing/advanced_explorer.py:216`.

5. **Clear all loses unrelated unapplied edits.** It is a server-rendered anchor built from the last normalized request. Changing dates/group/metric and then selecting Clear all navigates to the old selections, contrary to the existing reference requirement to preserve unrelated selections. Implement against the current form. Chart-style links have the same pending-edit behavior. Evidence: `billing/advanced_explorer.py:230`, `billing/advanced_explorer.py:233`, `templates/billing/parameters.html:16`, `docs/cost-explorer-reference.md:19`.

6. **Real `(Unassigned)` tag/category values collide with absence.** Unpacking converts missing keyed values to the display string `(Unassigned)` and uses that as the bucket key. A real value with that exact text merges into the same bucket and receives an ABSENT drilldown. Preserve a separate raw key/absence flag; use the placeholder only as display text. Evidence: `billing/advanced_explorer.py:47`, `billing/advanced_explorer.py:49`, `billing/advanced_explorer.py:197`, `billing/advanced_explorer.py:203`.

7. **Saved-report UI permission gate differs from endpoint authorization.** Save buttons use `user.is_staff`; the endpoint uses `scoping.can_edit`, which under authorization can allow an editable customer assignment without Django staff. A legitimately authorized editor may have no visible save button. Use the same capability context as the endpoint. This is an authorization/UI consistency issue, not evidence of data disclosure. Evidence: `templates/billing/parameters.html:47`, `templates/billing/parameters.html:52`, `billing/web.py:336`, `billing/web.py:23`, `billing/scope.py:24`.

8. **Comparison cost wording persists in usage mode.** Compare accepts usage, but its heading/description/chart labels and View costs action say costs, and the standard empty graph still says “No cost data”. Correct labels for the active measure during UI work. Evidence: `billing/parameters.py:68`, `templates/billing/comparison.html:2`, `templates/billing/comparison.html:5`, `templates/billing/comparison.html:9`, `templates/billing/explorer.html:11`.

Items above are established by source paths but were not reproduced in a running browser by this audit agent. No browser access or customer data was used.

## Existing verification coverage and limits

- `billing/tests/test_explorer_parameters.py:31` covers expression combinations, metrics, unsupported combinations, cache reuse/cooldown, role rotation, AWS pagination, incomplete exports, calendar comparisons, actual/forecast separation, metadata authentication, URL/save roundtrip, empty values and hourly timestamps.
- `billing/tests/test_explorer_controls.py:64` covers every filter include/exclude against an independent synthetic ledger; `billing/tests/test_explorer_controls.py:79` checks every group/metric against chart and CSV totals.
- `billing/tests/test_explorer_controls.py:113` checks chart-style query reuse and preservation of **applied** parameters; it does not exercise unsaved form edits in a browser.
- `billing/tests/test_explorer_controls.py:132` covers metadata filter/scoping behavior, optional capability messaging, usage scope, absent drilldowns, comparison charts and ownership transitions.
- `billing/tests/test_explorer_controls.py:214` covers relative/custom comparison reopening; `billing/tests/test_explorer_controls.py:224` covers native drivers; `billing/tests/test_explorer_controls.py:261` covers forecast ownership scope.
- `billing/tests/test_report_exports.py:93` covers pending, failed, cached and capability-blocked export affordances.
- `scripts/verify_ui.py:26` includes an Explorer page in viewport/screenshot checks; it does not exercise the detailed filter/report/table state transitions listed above.
- Existing docs acknowledge that optional live API permissions and granular-data opt-ins have not all been verified (`docs/cost-explorer-reference.md:50`). The source audit must not be reported as renewed live validation.
- Tests were inspected but not executed by this agent: the repository has no local virtual environment and the default Python lacks Django. The implementation/testing owner should run the focused suites in the established project runtime.

## Suggested file ownership for implementation

| Work package | Primary files | Coordination boundary |
| --- | --- | --- |
| Parameter contract and metadata correctness | `billing/parameters.py`, `billing/advanced_explorer.py`, `billing/web.py`, `billing/tests/test_explorer_parameters.py`, `billing/tests/test_explorer_controls.py` | One backend owner should control normalization and shared query/export/saved semantics. Keep AWS request scope restrictions unchanged except for explicit tested fixes. |
| Explorer presentation and interaction | `templates/billing/parameters.html`, `templates/billing/explorer.html`, `templates/billing/comparison.html`, `static/app.js`, `static/app.css` | One frontend owner should own shared table/search/sort code while adding pagination/preferences, date/forecast controls and current-form actions. Backend should supply stable raw data and normalized keys. |
| Report library/recent history | New report template/module as needed, `billing/models.py`, a migration, `config/urls.py`, narrow `billing/web.py` changes | Agree route/model contract with backend owner before edits. Avoid putting customer-bearing recent definitions in browser storage unless authorization and isolation are explicitly accounted for. |
| Assistant actions | New isolated template/module as needed, then include from Explorer | Define what each prompt does using available data and approved capabilities; do not label existing native driver data as an Amazon Q connection. |
| End-to-end validation | Dedicated synthetic browser tests plus current focused Python suites | Cover pending metadata cancellation, keyed grouping changes, absence filters, current-form clear/style/save, relative forecast resolution, table preferences/pagination, and report open/save/recent behavior. |
| Audit/docs | This directory plus existing Explorer reference docs | Record observed AWS controls separately from implemented behavior and runtime evidence. Update claims only after current testing. |
