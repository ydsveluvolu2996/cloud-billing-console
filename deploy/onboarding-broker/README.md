# Private onboarding IAM broker

This broker lets the native onboarding coordinator register a customer role without asking an operator to sign into the hosting AWS account for every customer. The customer still authorizes access once in **their own AWS account**, using the dashboard's role instructions or stack. The dashboard records the account, role, consent and verification request. Its isolated coordinator invokes this function and handles local approval, trust verification and import scheduling.

The Lambda accepts only this payload:

```json
{"role_arn":"arn:aws:iam::111111111111:role/BillingConsole/CostReadOnly"}
```

Success is exactly `{"role_arn":"…","status":"allowed"}`. A Lambda `FunctionError`, throttled invocation, unexpected response, or failed subsequent trust check must leave activation pending or failed. `allowed` confirms the IAM registration, **not** customer consent, trust correctness, Cost Explorer availability or completed imports. The coordinator must validate those separately before collection.

## Security boundary

The handler rejects wildcards, noncommercial partitions, other principal types, extra request fields and roles outside `BillingConsole/`. It writes only the dedicated `DashboardOnboardedCustomerRoles` inline policy, whose sole action is `sts:AssumeRole` on exact role ARNs. Existing `ExactCustomerRoleAllowlist` and other collector policies are untouched. Unexpected or broadened contents in the dedicated policy cause a failure instead of being copied forward.

The function execution role has only:

- `iam:GetRolePolicy` and `iam:PutRolePolicy` on `CloudBillingCollector`.
- `dynamodb:GetItem` and `dynamodb:UpdateItem` on its one table, restricted to the collector's key.
- Log-stream creation and event writes in its own log group.

IAM does **not** constrain `PutRolePolicy` to a policy's contents or this inline-policy name. Handler validation is therefore not the complete security boundary. Before deployment, a hosting administrator must attach an independently reviewed permissions boundary to `CloudBillingCollector`. It must cap the role at its required existing runtime actions/resources, `sts:AssumeRole` for permitted customer-role paths and exact existing exceptions, and invocation of this exact broker. It must not allow IAM administration, arbitrary Lambda invocation, or new hosting-account privileges. The broker cannot change or remove that boundary. See [AWS permissions-boundary evaluation](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html).

`deploy.py --apply` checks that the specified boundary is already attached and belongs to the hosting account. It does not author, inspect the full semantics of, or replace that independently managed policy. Keep boundary changes in the hosting administrator's maintenance process.

The template adds no Lambda URL, API Gateway or resource-based invocation permission. Only the exact Lambda ARN is added to the collector's identity permissions. Existing hosting administrators may invoke it according to their IAM policies. The web container must remain unable to access instance-role credentials or invoke the function. Neither the web nor native collector receives IAM write permissions. The coordinator's request validation, admin/MFA authorization, immutable request scope, and root-owned configuration remain essential controls.

## Durable state and retries

Reserved concurrency is one. A single DynamoDB item stores the desired exact role set and a version. Reads are strongly consistent, and updates use a version condition. IAM is rendered from that authoritative set, so an eventually consistent IAM read cannot accidentally drop a previously registered account. IAM grants absent from the authoritative set fail closed for administrator review. The table uses on-demand billing, encryption, point-in-time recovery and retain-on-delete/replace policies. These AWS resources have normal service charges.

An IAM failure after state persistence keeps the desired request for an idempotent retry. The handler reads back the written IAM policy before reporting success. IAM/STS permission propagation can still delay the coordinator's subsequent trust verification, which should retry with backoff. Synchronous invocation is required; check `FunctionError` as well as the payload.

The dedicated policy is capped at 4,096 compact characters before saving additions. AWS also imposes an aggregate 10,240-character inline-policy limit per role. A `LimitExceeded` error leaves the request saved and provides an actionable capacity message; it does not remove existing roles. Review/migrate policy capacity before raising the application limit. See [AWS IAM character limits](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html#reference_iam-limits).

This broker is add-only. Normal revocation must immediately disable collection through the approval/source and local allowlist controls. An administrator can then reconcile removal from both the dedicated IAM policy and DynamoDB state during maintenance. Never delete or restore only the state table: that can reintroduce stale grants or trigger a state mismatch. Disable onboarding first, retain a snapshot, reconcile both representations, and verify the collector boundary before resuming.

## One-time installation

1. Install the reviewed collector permissions boundary, preserving existing runtime permissions. Its invocation allowance must name the planned function ARN: `arn:aws:lambda:REGION:HOST_ACCOUNT:function:CloudBillingOnboardingBroker`.
2. Review the generated template and deploy with temporary hosting administrative access:

   ```sh
   python3 deploy/onboarding-broker/deploy.py \
     --profile HOSTING_PROFILE --region HOSTING_REGION \
     --boundary-arn arn:aws:iam::HOST_ACCOUNT:policy/REVIEWED_BOUNDARY \
     --output /tmp/onboarding-broker.json

   python3 deploy/onboarding-broker/deploy.py \
     --profile HOSTING_PROFILE --region HOSTING_REGION \
     --boundary-arn arn:aws:iam::HOST_ACCOUNT:policy/REVIEWED_BOUNDARY \
     --apply
   ```

   The first command only renders source; the second checks the attached boundary and creates or updates the stack. It creates an execution role, private Lambda, retained DynamoDB table, retained 90-day log group and an exact invocation policy on the existing collector role. The stack has termination protection. The dedicated customer IAM policy and state item are created only on the first valid invocation. New Lambda accounts may require a concurrency quota increase before AWS permits reserving one execution; deployment must fail rather than omit serialization.

3. Save the `BrokerArn` output in the root-owned native coordinator configuration. Install its service, database permissions and dashboard activation flow using the separate onboarding-worker maintenance instructions. Do not expose hosting credentials or broker invocation to the web runtime.
4. Invoke through the coordinator for one explicitly approved existing customer role, confirm the returned ARN, audit/state/IAM alignment, trust probes, and successful account-scoped import. Run negative tests for non-admin and wrong-account submissions. Confirm existing collector jobs and web health still pass.

For later broker code changes, rerun `deploy.py --apply`; it uses the reviewed local `broker.py` as the Lambda source. An application-only release does not update this privileged function or its IAM boundary. Run the tests below before updating. The raw `template.json` has a deliberately nonfunctional placeholder handler; deploy the rendered template through `deploy.py`.

## Validation

```sh
python3 -m unittest discover -s deploy/onboarding-broker/tests -v
cfn-lint deploy/onboarding-broker/template.json
```

The tests cover input and policy injection, exact resource constraints, separate IAM/state failure handling, idempotency, conditional writes, stale IAM reads, quota failures, source rendering, private invocation and the required boundary preflight. They do not substitute for live IAM/STS and customer-consent verification.
