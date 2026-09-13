# Administrator report performance

On 10 September 2026, profiling the deployed EC2 web runtime with 31,491 billing
rows measured 29.022 seconds for the default Cost Explorer and 13.580 seconds for
Cost overview. Database execution accounted for 28.888 and 13.509 seconds,
respectively. The host had approximately 1.1 GiB available memory and about 575
CPU credits; resource exhaustion was not the measured bottleneck.

The row-security predicate repeated the same administrator lookup for every cost
row, on every aggregate scan. One account-count query scanned the cost table twice
under a nested loop and took 3,316.200 ms. A transactional trial on the same data
reduced it to 34.811 ms with identical results. Anonymous access was still denied,
and the trial policy changes were rolled back after measurement. These are
single-reader measurements on this data set, not capacity claims for 100 live
customers.

The generated customer and cost read policies now evaluate
`(SELECT billing_can_access(NULL,NULL,false))` as an uncorrelated initialization
plan. A NULL customer cannot match a customer membership, so this expression is
true only for the existing active, internal portfolio-administrator branch. CASE
skips repeated per-row checks only when that authorization succeeds. The original
scoped predicates remain the fallback; write checks, user-owned report/cache
policies, MFA and database-role restrictions are unchanged.

This is a per-statement authorization check, not a shared cache. Tests use the
actual restricted PostgreSQL login and reuse a prepared statement while switching
identities, revoking grants, expiring memberships, disabling users, removing
superuser status and toggling external-portal access. An execution-plan regression
checks that each relation's global authorization initialization runs once, without
using a hardware-dependent timing assertion.

For an existing deployment, apply only the two updated `web_scope` USING
expressions from `deploy/database-roles.sql` as the database administration owner,
inside a transaction with bounded lock/statement timeouts. Retain the original
policy expressions for rollback. Recheck administrator and scoped visibility,
billing totals and actual web-runtime page timings. No schema migration, instance
resize, application-image change, data cache or collector change is required.
