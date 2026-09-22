# AWS Cost Explorer public API capabilities

Documentation audit: 2026-09-22. This document records current primary AWS documentation and implementation implications. It does **not** record live console observations, successful customer API calls, current IAM grants, or enabled account preferences. Earlier repository references are historical evidence and do not establish current customer capability.

## Standard cost and usage queries

`GetCostAndUsage` supports `DAILY`, `MONTHLY`, and `HOURLY`; up to two grouping definitions of type `DIMENSION`, `TAG`, or `COST_CATEGORY`; optional filters; pagination; and these exact metrics:

| UI measure | API metric |
| --- | --- |
| Unblended cost | `UnblendedCost` |
| Amortized cost | `AmortizedCost` |
| Blended cost | `BlendedCost` |
| Net unblended cost | `NetUnblendedCost` |
| Net amortized cost | `NetAmortizedCost` |
| Usage quantity | `UsageQuantity` |
| Normalized usage | `NormalizedUsageAmount` |

The documented ordinary grouping list is `AZ`, `INSTANCE_TYPE`, `LEGAL_ENTITY_NAME`, `INVOICING_ENTITY`, `LINKED_ACCOUNT`, `OPERATION`, `PLATFORM`, `PURCHASE_TYPE`, `SERVICE`, `TENANCY`, `RECORD_TYPE`, and `USAGE_TYPE`. Dates use an inclusive start and exclusive end. Responses preserve amount/unit pairs and an `Estimated` flag. Follow every `NextPageToken` without changing the request. Usage totals can mix incompatible units; restrict usage-type or usage-type-group scope. [GetCostAndUsage](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html)

**Implementation implication:** the API's two-group limit is not evidence that the console offers two simultaneous groups. Its prose grouping list is narrower than the repository's historical live-probe list. Keep the live-validated inventory separately; do not equate the shared dimension enum with universal grouping support.

### Filter inventory and metadata

Map the console labels to exact request identifiers:

| Label | API identifier |
| --- | --- |
| Service | `SERVICE` |
| Linked account | `LINKED_ACCOUNT` |
| Region | `REGION` |
| Instance type | `INSTANCE_TYPE` |
| Usage type | `USAGE_TYPE` |
| Usage type group | `USAGE_TYPE_GROUP` |
| Resource | `RESOURCE_ID` |
| Charge type | `RECORD_TYPE` |
| Availability zone | `AZ` |
| Platform | `PLATFORM` |
| Purchase option | `PURCHASE_TYPE` |
| Tenancy | `TENANCY` |
| Database engine | `DATABASE_ENGINE` |
| Legal entity | `LEGAL_ENTITY_NAME` |
| Billing entity | `BILLING_ENTITY` |
| API operation | `OPERATION` |
| Payer account | `PAYER_ACCOUNT` |
| Tag | `Tags` expression / `TAG` group type |
| Cost category | `CostCategories` expression / `COST_CATEGORY` group type |

`GetDimensionValues` supplies date-scoped, searchable, filtered choices in `COST_AND_USAGE` context. Linked-account attributes contain display names; values contain IDs. Usage types/groups expose unit attributes. `PAYER_ACCOUNT` appears in the general enum but is omitted from that page's context-specific prose list; require operation/account verification. `RESOURCE_ID` is documented there as opt-in EC2 Compute data within 14 days. The shared enum includes identifiers for other contexts and is not a menu specification. [GetDimensionValues](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetDimensionValues.html)

Tag and category choices use separate APIs. `GetTags` enumerates keys or values for a supplied `TagKey`, date range, and filter. `GetCostCategories` enumerates names, or values for `CostCategoryName`; names/values without associated cost are omitted. Both support pagination, but their metric-sorted mode forbids `NextPageToken` and `SearchString`. Do not combine those modes. [GetTags](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetTags.html), [GetCostCategories](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostCategories.html)

### Include, exclude, untagged, uncategorized

Values inside one leaf expression are OR alternatives. Combine separate filter leaves with `And`; exclusion wraps a leaf with `Not`. Each expression object must have one root operator. This original example means either listed region, excluding credits:

```json
{
  "And": [
    {"Dimensions": {"Key": "REGION", "Values": ["eu-west-1", "ap-south-1"]}},
    {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"]}}}
  ]
}
```

These are expression semantics, not a license to use every generic match option in every operation. [Expression](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_Expression.html)

For actual-cost queries, dimension matching permits `EQUALS` and `CASE_SENSITIVE`; tags/categories additionally permit `ABSENT`. Broader shared enums such as `CONTAINS` do not override the operation-specific contract. [GetCostAndUsage filter contract](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html#API_GetCostAndUsage_RequestParameters)

For tags, absent key and values mean no tags; a supplied key with absent values means missing that key. Use `ABSENT` explicitly for these UI choices rather than treating an empty string as missing. [TagValues](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_TagValues.html)

For categories, omitting key and values means unmapped to any category. The current documentation's second sentence reverses the supplied/omitted key wording while describing absence of a particular key. A keyed `ABSENT` expression is the implementation interpretation; verify against live data before treating it as a reconciled category result. [CostCategoryValues](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_CostCategoryValues.html)

**Application requirements:** omit empty ordinary filters; preserve selected identifiers independently of display labels; apply authorization scope outside user-controlled filters. For the same authorized population, include/exclude should partition the unfiltered result. A category/tag metadata permission failure is not an empty result. AWS-generated and user-defined tags must be activated as cost allocation tags before appearing in Cost Explorer; IAM alone cannot create that history. [Cost allocation tags](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/cost-alloc-tags.html)

## Resource detail and granular-data prerequisites

`GetCostAndUsageWithResources` is opt-in and limited to the last 14 days. Its current API contract requires a resource-ID filter or grouping and an EC2 Compute service filter. Hourly resource detail is EC2-only. Its introductory prose also says other resource data is daily, creating a documented tension with its mandatory EC2 filter. The operation supports the same seven metrics and up to two groups. [GetCostAndUsageWithResources](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsageWithResources.html)

The console preferences separately offer all-service hourly data without resource IDs, hourly EC2 instance detail, and daily resource detail for selected services. Daily/hourly granular data covers 14 days; management-account preferences and data-volume limits apply. Changes can take 48 hours to appear, with further preference changes blocked during that window. Granular visibility requires chargeable billing data, excluding the documented Billing Conductor billing-group cases. [Configuring multi-year and granular data](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-configuring-data.html)

**Implementation consequence:** do not promise non-EC2 resource API parity solely because the AWS console offers daily resources. Treat broader service support as unverified until the public API accepts the exact request and reconciles with that account's console. Do not silently force an EC2 service filter into a report that claims to show another service. Resource absence, unsupported range/service, pending settings, and IAM denial need distinct explanations.

Default daily/monthly history covers the current month plus 13 prior months. Multi-year monthly history can extend to 38 months; it is an organization preference and can be disabled after three months without use. [Hourly data overview](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-services-hourly.html), [Multi-year data](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-multi-year-data.html)

## Actuals and forecasts

`GetCostForecast` is a separate request, with one uppercase metric: `UNBLENDED_COST`, `AMORTIZED_COST`, `BLENDED_COST`, `NET_UNBLENDED_COST`, or `NET_AMORTIZED_COST`. It supports daily forecasts through three months and monthly forecasts through 18 months, despite the shared enum containing `HOURLY`. It has filters but no `GroupBy`. The response supplies mean values and optional lower/upper prediction bounds; `PredictionIntervalLevel` accepts 51–99. Its supported filter list omits `RESOURCE_ID` and `PAYER_ACCOUNT`. [GetCostForecast](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostForecast.html)

`GetUsageForecast` supports `USAGE_QUANTITY` and `NORMALIZED_USAGE_AMOUNT`, the same daily/monthly horizons, and prediction intervals. It explicitly requires a start date on or after today. `UnresolvableUsageUnitException` means the usage-type/group selection did not resolve compatible units. [GetUsageForecast](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetUsageForecast.html)

The cost-forecast page currently says start must be today or earlier, conflicting with usage-forecast wording. Starting today satisfies both documented statements. **Recommended implementation:** split a combined report at today: historical actuals end today exclusive, forecasts start today. For a future-only display, request from today then trim the displayed forecast. Validate alternate start-date behavior before changing this rule; never double-count today.

The console guide describes a 12-month future horizon, while the API references above describe 18. Console parity should follow the verified live picker; an 18-month option is an API-based extension unless observed. [Cost Explorer chart](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-chart.html)

Console forecasts use an 80% prediction interval and can be unavailable when history is insufficient. Forecasts are estimates, not accrued charges. Consolidated forecasts may lag changes in organization membership. [Forecasting with Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-forecast.html)

**Application requirements:** preserve actual/forecast labels and uncertainty bounds; do not synthesize zero forecasts on failure. Separate per-customer confidence intervals cannot be summed into a statistically valid portfolio confidence interval. Grouped forecasts require separate scoped requests and must not be presented as an API-native grouped forecast response.

## Compare and native cost drivers

`GetCostAndUsageComparisons` is public. It accepts a baseline and comparison interval, each exactly one calendar month with first-of-month boundaries; up to 13 months' history, or 38 with multi-year enabled. It supports expressions, dimension/tag/category grouping, and the same seven metric names as actuals. `MaxResults` is 1–2000. Results include baseline, comparison, difference, and unit; `TotalCostAndUsage` already covers all pages. Do not sum that total once per page. [GetCostAndUsageComparisons](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsageComparisons.html)

`GetCostComparisonDrivers` is a separate public operation with the same month-boundary/history rules. It includes service and usage type automatically, accepts grouping and filters, and returns typed driver facts with amounts/units. `MaxResults` is 1–10; follow pagination. The documented seven comparison metrics also apply here. [GetCostComparisonDrivers](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostComparisonDrivers.html)

The console comparison guide explicitly excludes resource filtering and resource grouping. It describes month-over-month and custom month selections, a top-three driver display, and a View all expansion. Its relative-range prose says current month versus previous month; use current live observation to resolve any difference from the repository's earlier previous-complete-month default. [Performing a cost comparison](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-perform-cost-comparison.html)

AWS distinguishes returned driver data from console-only explanatory wording. Driver facts can explain changes in usage, credits, fees, and reservation/Savings Plans coverage; ranked differences alone do not establish these causes. [How cost comparison works](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-understand-cost-comparison.html)

**Application requirements:** keep arithmetic two-query comparisons available where supported, but identify arbitrary-period comparisons as application behavior. Enable AWS-native driver analysis only for qualifying month intervals and permissions. Require complete period data and a stable authorized account population; do not query a broader customer scope to make an optional API work. Treat zero-baseline percentage change as undefined, not 0% or infinity.

## Saved reports, recent reports, bookmarks, CSV

AWS documents saved configurations, default reports, bookmarks, and CSV downloads. The recent-report list is console navigation history with access time and a link. [Cost Explorer reports](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-reports.html), [Recent Cost Explorer reports](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-exploring-data.html)

Crucially, `ce:CreateReport`, `ce:DescribeReport`, `ce:UpdateReport`, and `ce:DeleteReport` are **permission-only actions**, not directly callable public API operations. `ce:GetPreferences` and `ce:UpdatePreferences` are also permission-only. Adding these permissions does not create an SDK report/preference API. [Cost Explorer service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ce.html)

**Implementation consequence:** maintain this dashboard's saved definitions and recent views in its own authorized persistence. Import/export supported URL parameters and export its reconciled result data to CSV. Do not claim synchronization with AWS-native saved reports or AWS browsing history. Do not confuse Cost Explorer reports with the separate Cost and Usage Report export service.

## Analyze with Amazon Q

AWS documents three distinct interactions: suggested prompts and Ask question can update Cost Explorer's report parameters and visualization; Analyze with Amazon Q explains the configured view in Q's chat while leaving that visualization unchanged. Analysis receives filters, dimensions, granularity, and date range. Q permissions include `q:StartConversation`, `q:SendMessage`, and `q:PassRequest`, alongside the underlying cost-data permissions. [Cost questions and Analyze with Amazon Q](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-nlq.html)

AWS's current authorization reference lists Amazon Q's `q:` actions, including those three, as **permission-only**, without directly invocable public API operations. These IAM names are not an embeddable Cost Explorer analysis endpoint. [Amazon Q service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_q.html)

Q's analysis uses multiple Billing and Cost Management services, calculations, and API transparency. Its sources can include forecasts, comparisons/drivers, anomalies, optimization recommendations, budgets, and pricing. A summary of one current report cannot claim equivalent evidence coverage. [How Q cost management works](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-q-how-it-works.html)

**Implementation consequence:** an authentic native Q experience requires a user handoff to the AWS console with the correct account/view and permissions. In-dashboard AI analysis requires a separately designed model integration, customer data handling, tool authorization, and evidence-bearing output. Label that integration honestly; do not put a working-looking Amazon Q button over canned text or local arithmetic. No AI integration or permissions were configured during this audit.

## Account capability and delivery checklist

Cost Explorer must already be enabled in AWS; the public API cannot enable it. Initial current-month data can take about 24 hours, with historical/forecast preparation taking longer. Data refresh is at least daily. [Enabling Cost Explorer](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-enable.html)

Every public operation above needs its corresponding `ce:` permission in the customer's effective policy. Data visibility also depends on the source account, organization access, billing view, and settings; a console login is not evidence that the collector role has equivalent access. Several API contracts accept `BillingViewArn`, so preserve the selected view rather than silently querying a different cost population. [Cost Explorer permissions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ce.html), [Controlling Cost Explorer access](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-access.html)

API use is chargeable: primary-view requests cost $0.01; custom-view requests cost $0.01 per source. Hourly granularity additionally charges for hosted usage records. Cache and paginate deliberately; exposing a control must not automatically enable granular billing preferences. [Cost Explorer pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)

Before claiming live parity, verify the exact customer role, view, date range, metric, group/filter combination, pagination, returned units, and totals against that customer's AWS view. Record permission denial, unsupported combinations, data-not-ready, and complete-empty results separately. This audit changed documentation only; it did not change production code, policies, customer settings, or paid features.
