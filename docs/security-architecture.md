> Current production topology: web, database and collector share one EC2. The earlier separate-host threat model below is retained as the stronger alternative; it is not a claim about current host-level isolation. See [single-EC2 controls and limits](single-ec2-operations.md). Database identities, RLS, MFA and customer access controls still apply.

# Security architecture and authorization

The application extends the existing Django, PostgreSQL, durable worker and Caddy design. It preserves the Customer, BillingSource, AwsAccount, effective AccountAssignment, Cost and Alliance foundations. Additive migrations introduce governance and identity records without deleting existing billing facts, users, comments, service notes or snapshots.

## Runtime boundaries

- Existing EC2: Caddy and Django web. Its new instance role explicitly denies customer AssumeRole and IAM operations. IMDSv2 hop limit 1, disabled SDK metadata access, no shared collector environment/files, and separate collector hardware keep customer credentials out of the web container.
- Separate small EC2: the collector runs as a restricted systemd service. It has the retained collector IAM principal with exact approved customer role resources. It reads its own secret and mounted allowlist, communicates with PostgreSQL on the web host's private address over verified TLS, and has no inbound application endpoint or SSH rule.
- Administration: separately authorized operators use migration/administration database credentials. The web and collector cannot approve their own roles or grant portfolio access. `security_admin` prepares policies but never invokes IAM mutation APIs.
- PostgreSQL: non-owner, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOBYPASSRLS runtime identities. `deploy/database-roles.sql` grants table/column privileges and customer row policies to the web. The collector has cross-customer operational access, not identity/approval administration. Only the admin owns tables. RLS request identity is set transaction-locally, never from request parameters.

## Application authorization

Every HTTP request resolves current memberships into a ContextVar. Default billing model managers filter customers and scoped objects; mutations validate scope and related objects. There is no authenticated-user or staff-wide fallback. Portfolio administration requires both an individual superuser and an explicit UserSecurity portfolio grant. Operators edit assigned customers; viewers read assigned customers. Optional account restrictions narrow facts and account records; precomputed whole-customer budgets/project/cache data is hidden from restricted identities.

Security enforcement covers lists, search, object URLs, exports, saved reports, Alliance snapshots, bulk preview ownership and jobs. Cached requests include the customer, requester, parameters, connection and ownership version. Transfers invalidate cached ownership assumptions; AWS advanced reports split historical ownership intervals. Background execution rechecks active connection configuration and requesting user access. Browser-supplied customer/source IDs never confer authorization.

Commands are trusted service boundaries outside the HTTP context, with database privileges providing the independent runtime boundary. Adding new models/routes requires a scope mapping, RLS policy and negative tests. Raw SQL, alternate managers, relation traversal and background service entry points must be included in independent review. RLS limits missed application predicates; it does not make a fully compromised web process or SQL injection safe to impersonate arbitrary transaction context. Parameterized SQL and the web identity's inability to change grants remain required.

## Identity and recovery

Local MFA uses maintained `django-otp` TOTP devices with throttling and replay prevention. Enrollment requires a primary login and successful TOTP confirmation; confirmed device seeds are not displayed again. High-entropy single-use recovery codes are stored only as password hashes. MFA failures are throttled. Session versions revoke existing sessions after grants/revocation or audited administrator recovery; membership and expiry are re-read on every request.

OIDC uses `mozilla-django-oidc`, PKCE, nonce, HTTPS, signature validation and explicit issuer/audience checks. Only administrator-provisioned issuer/subject bindings can log in. There is no automatic email linking or just-in-time signup. Local MFA remains required after SSO, so an unknown IdP MFA claim cannot bypass enforcement. IdP tenant/client details and real federation testing remain external inputs.

External invitations use one-use hashed tokens and an immutable pre-provisioned user ID plus matching email. Invitations, acceptance and customer access default to disabled, with readiness/reconciliation/independent-review gates. Support access has an evidence reference and a bounded expiry. This build sends no invitations.

## Logs and secrets

Secret-file settings support restricted Secrets Manager materialization on each runtime. Customer STS credentials remain in memory for 15-minute sessions. Dedicated structured audit events record actor, scope, target, outcome and timestamp. The prepared CloudWatch log group retains the central copy; runtime roles only create streams/append events and cannot delete it. Deployment must configure shipping before admitting customers. Logs do not include request bodies, cookies, authorization headers, IdP tokens or AWS response credentials; the logging filter redacts common secret formats and strips exception payloads.

These are implementation controls and review targets, not a compliance certification or independent security review. Live network, IAM, RLS, secret storage and central-log evidence must be collected after an authorized deployment.

## Narrow database administration functions

The web has no general Cost update privilege. `billing_restamp_ownership` permits effective-dated customer restamping only after it checks write access to every affected old/new owner under an account row lock. A PostgreSQL exclusion constraint independently rejects overlapping assignment intervals. The function cannot modify cost amounts. `billing_accept_invitation`, `billing_revoke_customer_access` and the boolean readiness functions implement bounded operations that cannot be accomplished by granting broad identity-table writes. All use a fixed search path, revoke PUBLIC execution, and are tested with real web logins. Review these functions as privileged code.

Approved aliases, owners and environments are stored on ownership intervals so a transfer does not disclose the new customer's metadata to the old owner. Existing account fields are retained. Alliance revisions preserve prior tracking fields, comments, service notes and snapshots in append-only customer-scoped records; old audit records are not deleted. Central security logs omit business note contents.

Bulk previews bind to an immutable requesting user ID and an authorization fingerprint. A grant, revocation, role/account-scope change, expired support membership or reused username cannot reopen an older preview under different permissions. PostgreSQL policies require the server-computed current request fingerprint for both bulk previews and cached query results. Historical previews remain stored but must be uploaded again before applying under the new binding; there is no automatic grant/backfill.

## Dashboard connection activation

For single AWS accounts (including organization members) and member budget readers,
MFA-verified internal portfolio administrators can submit **Connect account**.
`billing_request_activation` is a request-only SECURITY DEFINER function. It locks
and checks the live administrator, session version, source, consent and customer,
and writes an immutable snapshot. Neither web nor collector can directly insert,
change or approve activation requests. Shared/consolidated payer onboarding retains
the reviewed administration path until explicit inventory approval is automated.

A separate root-supervised activation process uses the administration database
identity, verified TLS and protected files. It rechecks the saved request, current
access, connection version, External ID hash and approvals before authorizing the
exact role. It invokes one private Lambda, which maintains a separate, exact-ARN
AssumeRole policy from durable, strongly consistent DynamoDB state. The collector
has no IAM mutation permission. Its independently managed permissions boundary
preserves existing operations, limits future customer roles to `BillingConsole/`,
and explicitly denies IAM administration; the boundary alone grants no role access.
The original exact-role IAM policy and its existing customer permissions are retained.

The coordinator atomically updates the collector's root-owned allowlist, approves
only the submitted account and selected capabilities, and queues trust verification.
Missing/wrong External ID tests remain mandatory. It waits for fresh verification
and discovery, then displays progress or an actionable failure in the dashboard.
After connection checks pass, the background engine automatically queues the first
billing and approved budget imports, then continues collection every six hours.
Current connection version, verification, customer activity and approval gates apply
to initial imports and scheduled refreshes alike. Paused or unapproved sources are
skipped. Optional dashboard pull controls request an extra refresh within the user's
authorized scope; they are not required to start collection. Single-account collection filters AWS requests to that ID
and rejects results outside it before replacing any saved costs. No customer access
keys, passwords, or hosting AWS sign-ins are part of ordinary account activation.

This adds a privileged service to the existing single EC2, not a new host-isolation
claim. Root and the approved release process remain trusted. A compromised native
collector could invoke the broker for another `BillingConsole/` role; customer trust,
External IDs and the exact local/database approval gates still apply. The web cannot
reach instance metadata, the broker, admin credentials or the writable allowlist.
See the broker and worker deployment READMEs for bootstrap, monitoring and rollback.
