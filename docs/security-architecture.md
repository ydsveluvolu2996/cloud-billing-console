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

External invitations use one-use hashed tokens and exact matching pre-provisioned identity email. Invitations, acceptance and customer access default to disabled, with readiness/reconciliation/independent-review gates. Support access has an evidence reference and a bounded expiry. This build sends no invitations.

## Logs and secrets

Secret-file settings support restricted Secrets Manager materialization on each runtime. Customer STS credentials remain in memory for 15-minute sessions. Dedicated structured audit events record actor, scope, target, outcome and timestamp. The prepared CloudWatch log group retains the central copy; runtime roles only create streams/append events and cannot delete it. Deployment must configure shipping before admitting customers. Logs do not include request bodies, cookies, authorization headers, IdP tokens or AWS response credentials; the logging filter redacts common secret formats and strips exception payloads.

These are implementation controls and review targets, not a compliance certification or independent security review. Live network, IAM, RLS, secret storage and central-log evidence must be collected after an authorized deployment.
