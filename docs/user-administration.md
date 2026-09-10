# Users, customer access and sign-in

Portfolio administrators use **Users & access** to create and manage internal users. Administrators see all current and future customers. Operators can manage selected customers; viewers can read selected customers. Optional AWS account IDs restrict access within the chosen customers. Every listed ID must be currently assigned to a selected customer, and every selected customer must have at least one ID when account restrictions are used.

Create each user's username, initial password and role, then share credentials through your approved secure channel. New users enroll their own authenticator at sign-in. Password changes and access changes revoke their existing sessions. Disabling a user blocks sign-in. Deletion removes the login, MFA device and memberships while preserving billing facts and billing audit events. A user with legacy Django administration log entries must be disabled rather than deleted to retain that history. Administrators cannot delete themselves or remove their own administrator access.

The sign-in screen accepts username, password, and an authenticator/recovery code. Password-only validation creates a five-minute pending challenge, never a dashboard-authenticated session. MFA setup and recovery codes also appear on the sign-in screen without dashboard navigation. Pending challenges are bound to the user's password hash and access version; expiration, password changes, disablement or revocation invalidate them. TOTP verification and recovery consumption use row locks, replay detection and persisted throttling. Old `/mfa/` links return to `/login/`.

The top-left **Hide sidebar / Show sidebar** button collapses navigation on desktop and opens/closes it on mobile. Its desktop preference is stored only in the browser. Hiding navigation is a layout preference; use **Sign out** to end a session.

## Deployment

After schema migrations, apply `deploy/database-roles.sql` as the separate database administration owner. Its generated policy includes `deploy/user-administration.sql`. The web runtime receives only the guarded `billing_admin_user(jsonb)` capability, not general identity insertion/deletion or privilege-management grants. It checks the current active portfolio administrator and transaction-local MFA verification. Non-administrators and the collector are denied. Customer/account ownership checks and self-removal protection are repeated inside the function, and changes create an audit event without passwords or MFA secrets.

As elsewhere in this application, PostgreSQL request identity comes from the trusted web process. These database checks defend against accidental missing application authorization; they do not defend against arbitrary code/SQL execution that can forge that process's request context. Keep the separate runtime identities, network restrictions, MFA and security review controls in place.

The existing administration command remains available for initial portfolio grants and emergency recovery. A grant revokes sessions, so the administrator must sign in again with the existing password and authenticator. Do not enroll or reset a user's authenticator as part of a normal deployment.
