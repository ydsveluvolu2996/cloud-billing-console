# AWS Cost Explorer live control inventory

Observed directly in the user's signed-in Chrome Cost Explorer tab on 22 September 2026. This document records UI controls and behavior, not customer billing amounts, account identifiers, credentials, or AWS implementation code. Scope: the Cost Explorer report workspace and its report-management flows. AWS global navigation, payments, infrastructure administration, and account settings are not Cost Explorer reporting features.

Evidence status: **Observed** means opened/read in the live interface; **Conditional** describes a visible prerequisite/disabled state; **Pending inspection** is explicitly not a verified option. Runtime implementation coverage is tracked separately. Dynamic service/account/tag/resource values must come from each authorized customer's data rather than a copied list.

## Workspace and report actions (Observed)

- W01 Report title: New cost and usage report.
- W02 Report parameters button opens a right-side panel; panel has a Close button and resize handle.
- W03 Recent reports dropdown; observed empty state: No recent reports.
- W04 Save to report library opens a modal with name input, Cancel, Close modal, and Save report (disabled until a name is entered). No AWS report was saved during inspection.
- W05 Analyze with Amazon Q opens a resizable Amazon Q side panel with Close, prompt library, Ask me anything about AWS input, and Send message. No prompt was sent. This requires AWS-hosted Q access; it is not a public Cost Explorer chat API.
- W06 Explore your costs with AI prompt carousel with previous/next scrolling and Ask question.
- W07 Prompt: Which services were my top spenders last month?
- W08 Prompt: What is my projected cost for next month?
- W09 Prompt: Show my monthly spend for past 3 months.
- W10 Prompt: How much did I spend on database services this month?
- W11 Prompt: Which services had the biggest cost increase this month?
- W12 Prompt: What was my compute cost last month?
- W13 Prompt: Compare my compute costs for last 2 months.
- W14 Cost and usage overview: total cost, average cost per selected granularity, selected grouping count; Info help control.

## Time and ranges (Observed)

- T01 Time section expands/collapses; Standard and Compare report-mode controls.
- T02 Date Range opens two adjacent calendars; previous/next month navigation; editable Start date/End date; Cancel and Apply.
- T03 Past presets: last 1 day, last 7 days, month to date, last 1 month, last 3 months.
- T04 More reveals last 6 months, last 1 year, last 3 years, year to date, current month; Hide collapses the extra shortcuts.
- T05 Future presets: next 1 month, next 3 months, next 12 months, next 18 months.
- T06 Historical and future presets can combine. Relative ranges remain rolling when a report is reopened.
- T07 Live boundary evidence on 2026-09-22: last 1 day starts 2026-09-21; next 1 month extends end through 2026-10-31; current month is 2026-09-01 through 2026-09-30; year to date is 2026-01-01 through 2026-09-22. Dates displayed in the console are inclusive.
- T08 Granularity menu: Hourly, Daily, Monthly.

## Group by (Observed)

- G01 Expand/collapse, Clear, searchable dimension menu (Filter groupings).
- G02 Dimensions in observed order: None, Service, Linked account, Region, Instance type, Usage type, Resource, Cost category, Tag, API operation, Availability zone, Platform, Purchase option, Tenancy, Database engine, Billing entity, Legal entity, Charge type, Payer account.
- G03 Tag selection reveals a searchable key selector; keys are customer data. Selected tag grouping URL uses `groupBy=["TagKeyValue:Environment"]` (illustrative observed generic key).
- G04 Cost category reveals key selection, but this account has no available category keys (0 of 0), so category value behavior and URL encoding cannot be verified. Resource first presents a dynamic service picker; the live account listed 59 services. Choosing EC2 Instances revealed 55 resource choices with search, Includes/Excludes, Select all, Cancel and Apply. Public resource API restrictions are documented separately.

## Filters (Observed)

- F01 Expanded Filters section: Info, Applied filters count, Clear all, Filter preference.
- F02 Default visible filters: Service, Linked account, Region, Instance type, Usage type, Usage type group, Resource, Cost category, Tag.
- F03 More filters reveals Charge type, Availability zone, Platform, Purchase option, Tenancy, Database engine, Legal entity, Billing entity, API operation, Payer account; Show less collapses them.
- F04 Every filter has a dedicated Clear action.
- F05 Opened all 19 filter families individually. Ordinary dimension filters expose the same controls; Service example: searchable choices, Includes/Excludes radios, Select all with result count, multiple checkboxes, visible result count, Cancel, Apply, and loading state.
- F06 Filter preference modal: visibility switches for all 19 filters; Cancel, Save, Close. No AWS preferences were saved during inspection.
- F07 Resource selector first asks for a service; Cost category and Tag first ask for keys.
- F08 Opened Tag key and value selectors; multiple tag keys can be selected (each gets its own value filter): key search, dynamically loaded keys, value search, Includes/Excludes, Select all, multiple values, Cancel, Apply, and a distinct `No tag key: <key>` choice.
- F09 Observed tag URL schema: `{"dimension":{"id":"TagKey","displayValue":"Tag"},"operator":"INCLUDES","values":[{"value":"DEV","displayValue":"DEV"}],"growableValue":{"value":"Environment","displayValue":"Environment"}}`. Selecting the missing key gives `values:[{"value":"","displayValue":"No tag key: Environment"}]`.

## Advanced options (Observed)

- A01 Expand/collapse and Info help.
- A02 Aggregate costs by: Unblended costs, Amortized costs, Blended costs, Net unblended costs, Net amortized costs.
- A03 Show forecasted values (checked in the original standard report).
- A04 Show only untagged resources.
- A05 Show only uncategorized resources.
- A06 Show usage as normalized units is visible and disabled both in the original unfiltered report and after selecting one EC2 instance usage type in hours with Linked account grouping. Selecting that usage type automatically sets usageAggregate=usageQuantity and displays cost and usage together in the overview, paired charts, and paired table rows. The enabled normalized-units prerequisite was not available in the inspected state.

## Chart and breakdown (Observed)

- V01 Cost and usage graph with Bar, Line, Stacked bar controls.
- V02 Interactive chart; accessible descriptions for periods/series; chart legend has individual show/hide checkboxes; includes Others for grouped overflow.
- V03 Cost and usage breakdown: group column, group total, one column per selected period, total row and group rows.
- V04 Download as CSV.
- V05 Find cost and usage data search.
- V06 Previous page, numbered page selection, Next page; disabled when unavailable.
- V07 Preferences modal: five Page size radio choices and Wrap lines toggle; Cancel/Confirm/Close. **Source UI defect:** all five page-size labels literally render `_Plural_Items_`, confirmed visually and in accessibility state. Numerical page sizes were not readable and must not be claimed as observed.
- V08 Wrap lines helper: wrap table cell content when enabled, truncate when disabled.
- V09 Footnotes distinguish estimated current charges and forecasts; currency is USD and billing dates use UTC.

## Compare mode (Observed)

- C01 Overview displays baseline month cost, comparison month cost, absolute difference and percentage difference.
- C02 Date Range: Relative → Month over month; Absolute → Custom (Choose two months to compare against each other).
- C03 In this live session Month over month resolved to July versus August 2026, the two previous complete months. This is direct evidence, distinct from any conflicting documentation wording.
- C04 Custom reveals Compare and Versus month fields, calendar buttons, and `Use YYYY/MM format` help.
- C05 Granularity, Resource filter, and Show forecasted values are disabled in Compare; Save to report library also observed disabled in Compare.
- C06 Cost comparison drivers card: top driver narratives grouped by service/usage/account and View all; figures can link to scoped comparison reports. View all opens the resizable side panel containing driver narratives and Close/Dismiss controls; each breakdown row has View for its scoped drivers.
- C07 Comparison chart uses paired bars for baseline/comparison across groups and two legend toggles.
- C08 Comparison breakdown: grouping label, baseline month, comparison month, difference, difference (%), Cost comparison drivers View per group. Numeric header buttons sort; search, CSV, pagination and Preferences remain available.
- C09 Zero-baseline percentage is shown as a dash, not a fabricated infinity. Large percentage changes can be abbreviated as `999%+` in the AWS presentation.

## Saved-report library (Observed)

- L01 Cost Explorer Saved Reports opens All reports with search (Filter) and Create new report.
- L02 Table columns: Report name, Type, Time range, Time granularity, Grouped by, Filtered by; all six headings are sortable.
- L03 Select-all and individual report checkboxes; Delete and Duplicate actions are disabled until a report is selected. No AWS report was deleted or duplicated.
- L04 Observed built-in Cost Explorer examples: Monthly costs by service, Monthly costs by linked account, Monthly EC2 running hours costs and usage, Daily costs, AWS Marketplace.
- L05 Library also links to Reservation utilization/coverage and Savings Plans utilization/coverage reports. These are separate report products, not Cost Explorer parameter dimensions.
- L06 Create new report offers five radio types: Cost and usage (recommended), Savings Plans utilization, Savings Plans coverage, Reservation utilization, Reservation coverage; Create Report continues to the selected product.
- L07 Those utilization/coverage choices describe customizable utilization/coverage targets. This audit records their presence; their separate reporting workspaces have not been inspected under this Cost Explorer inventory.

## Verified URL and forecast behavior

Historical enums observed after Apply: LAST_1_DAY, LAST_MONTH, LAST_6_MONTHS, LAST_12_MONTHS, LAST_3_YEARS, YEAR_TO_DATE, CURRENT_MONTH. Future enums: NEXT_MONTH, NEXT_3_MONTHS, NEXT_12_MONTHS, NEXT_18_MONTHS. Future dates end on the last day of the chosen future month. Requesting a forecast while grouped opens an Enable/Cancel modal explaining forecasting requires no Group By; Enable clears the grouping. This is a report-query change, not an AWS billing-preference opt-in.

## Inspection boundaries

Opening and changing report controls only reads billing data. No IAM policy, granular data opt-in, infrastructure, AWS report, or saved preference was changed. Incomplete or disabled capabilities must have a visible explanation in the billing console; they must not be implemented as invented data or inert buttons. Amazon Q is an AWS-hosted feature; the implementation must distinguish an AWS handoff from a local AI service.
