# Security policy

The supported branch is `codex/billing-security-portfolio`. Report suspected vulnerabilities privately to the repository owner; do not put credentials or customer data in issues or pull requests.

Pull requests and pushes run application tests, dependency auditing, static analysis, full-history secret scanning, and container vulnerability scans. Scheduled checks run weekly. Dependabot proposes dependency updates; updates require review and passing checks. Actions are restricted at repository level to explicitly approved full commit SHAs. When reviewing an action update, update the repository allowlist to the reviewed SHA before running it.

Production releases require an explicit workflow dispatch, the exact tested commit, the designated release branch and environment, temporary AWS OIDC credentials, and successful tests and security scans. Checkout credentials are not persisted. Production deployment includes backup and health checks.

## Remaining platform limitations

The current private repository plan does not provide branch protection/rulesets or repository secret scanning/push protection. CI scans detect issues after a push; they do not prevent an administrator bypassing checks. Upgrade to a plan supporting private branch protections to require pull requests and successful test/security checks, block force pushes/deletions, and prohibit bypass. Confirm secret scanning availability separately. Private fork restrictions require an organization-owned repository. Account MFA must be verified in the owner's GitHub security settings; repository API access does not establish its status.

Repository privacy cannot revoke copies made while the repository was public. Rotate any credentials known or suspected to have been exposed, even after removing them from history.
