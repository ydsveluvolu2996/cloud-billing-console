# Manual IAM role onboarding

Every customer uses the same workflow. Customer names never determine permissions. Customers create an IAM role manually; CloudFormation, agents, access keys and billing exports are not prerequisites. Existing customer stacks need not be deleted: their role can be retained if its customer-approved trust and permissions pass the new checks.

1. An operator creates/selects the business customer and adds a payer or standalone connection with its real 12-digit account ID. The server generates the unique External ID.
2. Record supplied approval contacts, authorized users, expected account IDs, approved fields/optional metadata, region and retention at **Approval and rollout**. Empty records are pending, never implicit approvals.
3. Choose a customer-approved IAM role name/path. **View and copy IAM policies** provides exact collector trust, minimum permissions and separate optional policies. If the role name changes, replace the self-role ARN in `iam:GetRole` with the chosen ARN. Enable Cost Explorer first.
4. The customer administrator creates the role and returns its ARN. The operator saves it. This prepares a versioned exact allowlist request; the web does not modify AWS IAM.
5. An authorized administrator reviews evidence in the separate admin runtime, approves the customer and role with `security_admin`, exports the exact ARN allowlist and matching provider policy, and separately applies the approved IAM change and read-only collector file. This build performs none of those live actions.
6. The isolated collector assumes the role for 15 minutes, checks its account identity and reads its own role trust. It requires the exact supported trust statement, plus explicit AccessDenied responses without and with an incorrect External ID. A timeout, throttle or unrelated error is inconclusive. Any alternative broad Allow trust is rejected and must be reviewed.
7. Discovery includes all Organizations pages when authorized. Explicit inventory is available without Organizations. Newly discovered accounts absent from approved inventory remain unassigned for review. Initial collection and activation remain gated on approval, allowlisting and current trust validation.

The External ID protects connection selection; it is not a customer password. No permanent customer credentials are stored. Provider allowlisting must not impose an ExternalId condition, because that could mask an insecure customer trust in negative probes. Only exact role resources are permitted on the provider side.

## Permission map checked against AWS documentation on 2026-09-09

| Capability | Actions | Resource scope / behavior |
|---|---|---|
| Core costs and account dimension | `ce:GetCostAndUsage`, `ce:GetDimensionValues` | Default billing scope uses `*`. Current AWS authorization tables also list optional `billingview` resource support; this implementation does not request custom billing views. |
| Trust verification | `iam:GetRole` | Exact customer role ARN only. Needed to inspect the actual trust rather than infer it from STS alone. |
| Inventory | `organizations:DescribeOrganization`, `organizations:ListAccounts` | `*`; these operations do not support per-account resource scoping. Optional, management account only in this workflow. |
| Tags | `ce:GetTags`, `ce:ListCostAllocationTags` | Optional; customer approval and activated allocation tags required. No activation writes. |
| Categories | `ce:GetCostCategories` | Optional, read only. |
| Forecast | `ce:GetCostForecast` | Optional; insufficient history/data is reported and run-rate remains labelled separately. |
| Resource detail | `ce:GetCostAndUsageWithResources` | Optional; AWS opt-in and lookback restrictions apply. |
| Imported AWS budgets | `budgets:ViewBudget` | `arn:aws:budgets::<account>:budget/*`. Optional member budget-reader roles are justified only for budgets unavailable to the approved payer role. |

Dashboard budgets require no customer AWS writes. The platform supports the commercial `aws` partition by default. Other partitions are rejected until a same-partition collector, endpoints and capabilities are explicitly configured and verified.

Sources: [Cost Explorer authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ce.html), [Organizations authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_organizations.html), [STS External ID guidance](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html), [GetRole](https://docs.aws.amazon.com/IAM/latest/APIReference/API_GetRole.html), [Organizations pagination and State](https://docs.aws.amazon.com/organizations/latest/APIReference/API_ListAccounts.html).

Real customer approvals, trust checks and reconciliation have not been performed during this build. The original customer CloudFormation file is retained for historical reference only; new onboarding does not require or link to it.
