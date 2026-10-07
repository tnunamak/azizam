# People and built-in accounts

Owners manage People, Invites and Access plans at `/ui/people/`. Existing
profiles remain the policy model and use the existing profile CRUD and
`POST /admin/api/policy/simulate` APIs. The Home, Settings and Status pages
are unchanged by this feature.

## Account flow

1. The owner creates an invitation with a stored profile, expiry and optional
   email restriction. A pending invitation can be copied again or revoked.
2. The invitee opens `/ui/invite/#token=...`. They create an email/password
   account, or sign in with the configured OIDC provider and accept using
   their signed-in identity. Targeted OIDC invites require a verified email.
3. Acceptance atomically assigns the access plan, creates a personal App and
   API key, consumes the invitation and records an audit event. All existing
   Apps and keys of an OIDC invitee receive the invitation's plan as well.
4. The browser shows the new key once. After saving it, the member continues
   to Home. Keys use the existing runtime authentication and policy paths.

New passwords require 12–1,024 characters and use scrypt. Existing local-owner
PBKDF2 passwords still work. Login attempts have persistent per-email and
per-IP limits. Browser sessions reuse the existing signed cookie and CSRF
mechanism. No provider token is stored in a browser cookie.

Invitation tokens are derived from random link IDs with a domain-separated
HMAC of the persistent browser signing key. Only their SHA-256 hashes are
stored in the database. This lets an authorized owner copy a pending link
again without storing its bearer token. Changing the signing key prevents
copying existing links; links already shared retain their original expiry
and one-use checks. Password-reset tokens are independent random values and
are shown only when the owner creates the reset link.

## JSON interface

All account responses use `Cache-Control: no-store`. Owner mutations require
the existing `x-azizam-csrf` token from `/admin/api/session` for browser
sessions. Bearer credentials follow existing admin authentication. Ordinary
members and administrators cannot use the owner-only People/invitation APIs.

| Endpoint | Result / request |
| --- | --- |
| `GET /auth/api/options` | Available built-in/OIDC methods; signed-in OIDC identity and its CSRF token when present |
| `POST /auth/api/login` | `{email,password}`; sets browser session, returns Home destination |
| `GET /auth/api/invite?token=...` | Invitation email, expiry and existing access plan |
| `POST /auth/api/invite/accept` | `{token,email,name,password}` for built-in; omit password and send session CSRF for OIDC |
| `POST /auth/api/password-reset` | `{token,password}`; consumes reset link and invalidates earlier sessions |
| `GET /admin/api/people` | People, assigned plan, last active, builtin/protected/role flags, profiles |
| `PATCH /admin/api/people/{id}` | `{profile_id?,disabled?}`; updates member policy or availability |
| `DELETE /admin/api/people/{id}` | Removes member from People and revokes access |
| `POST /admin/api/people/{id}/reset` | `{expires_in:3600}`; generates a built-in password reset link |
| `GET /admin/api/invites` | Invitation metadata and profiles, never raw tokens |
| `POST /admin/api/invites` | `{profile_id,email?,expires_in}`; invitation metadata and private link |
| `GET /admin/api/invites/{id}/link` | Recover the same pending invitation link; expired/used/revoked links fail |
| `DELETE /admin/api/invites/{id}` | Revoke pending invitation |
| `GET /admin/api/operator/sign-in` | Current effective sign-in choices |
| `PUT /admin/api/operator/sign-in` | `{mode:"built-in"}`, `{mode:"oidc"}` or `{mode:"both"}` |

The sign-in choice lives in `operator_settings.sign_in` in admin.db and
applies without restart. OIDC must be configured before selecting `oidc` or
`both` explicitly. The Settings lane can use these APIs without changing the
account implementation. The default permits built-in sign-in and exposes
OIDC when its existing configuration is enabled.

## Revocation and ownership

Changing a member's plan updates every owned App and key, including explicit
key policy overrides. Disabling an account revokes its keys and browser
sessions and disables its Apps. Enabling it permits fresh sign-in and new
keys; old keys and sessions remain revoked. Owner/admin accounts are
protected from member-management actions.

Removal retains a tombstone so an old OIDC identity cannot return and recover
access. It deletes the password verifier and revokes Apps, keys, reset links
and sessions while preserving historical attribution. It is not physical
personal-data erasure. Password reset invalidates earlier browser sessions,
but does not rotate runtime API keys.

Receipt/job ownership is independent of profile visibility. New media jobs
carry the authenticated App ID and key hash through job updates. Legacy
runtime keys also receive trusted key-hash attribution, so new queued jobs
remain accessible before assets exist. Store ownership filters run before
list limits, and get/events/cancel/content check the same ownership rule.
Client and admin views do not expose owner key hashes.

Owner Library, request and usage views are instance views by default. They
include personal Apps, service Apps and local-network requests that arrived
without an API key. These no-key records are labeled `Local network · no key`;
when the request path records a client hint, the UI can show that hint as
supporting source detail. Historical receipt rows with `app_id = NULL` are
interpreted the same way at read time. This is non-destructive; no receipt
backfill is required.

Members do not inherit owner instance scope. Their Home, Usage and Library
views remain limited to their own Apps and key-attributed assets. A member
cannot filter to `local-network` or another App source. A member never sees
service-app receipts or no-key local-network receipts unless a future
explicit sharing feature grants that access.

Owner source filters use stable source IDs:

| Source ID | Meaning |
| --- | --- |
| `all` or omitted | All instance receipts visible to the owner |
| `local-network` | No-key local-network receipts (`app_id = NULL`) |
| `app:<app_id>` | Receipts for one App, including service Apps |

Library uses the same source IDs. Job-backed assets take their source from the
job receipt, or the job's trusted App/key identity if the receipt is absent.
Cache-only assets first use embedded provenance `request_id` to
find a receipt; if no receipt exists, their internal owner key hashes are
matched to historical App credentials, including revoked credentials, to
recover the App label. If neither path can recover a source, owner views show
the asset as `Local network · no key`.
The hashes are never returned by the API.

**Upgrade behavior:** historical records without trusted App/key attribution
are excluded from member-scoped lists and return 404 on scoped member detail
access. Owner instance views label historical `NULL` App receipts as
local-network/no-key source records. Old client-supplied parent receipt IDs,
labels and asset references cannot establish which member submitted a job.
They are not used to manufacture member ownership. Already-issued complete
signed asset URLs retain their existing capability semantics and expiry.
Explicit auth-disabled gateways retain unscoped access.

## Deployment and limits

No new environment variable, dependency or registry entry is required.
Account tables are created additively in the existing admin database during
boot. New job/receipt attribution uses their existing JSON storage. Build
and deploy the static export with the Python code; use the documented safe
restart procedure at deployment. This lane does not restart any service.
Sign-in and invitation responses use the saved instance name from Settings,
with the same bootstrap defaults as discovery; name changes apply immediately.

Monthly request usage is `null` until a complete monthly measurement source
exists. The diagnostic receipt store retains 14 days, so the UI says
“Not measured yet”. Access-plan cards do not fabricate a requests/day limit
or claim that cloud fallback permission is a paid-provider spending policy.
Optional passkeys are deferred because no WebAuthn library is in the locked
dependencies. Links are shared by the owner; email delivery is not added.

Settings updates for instance presentation, OIDC trust and media admission
require `If-Match` with the revision returned by their GET response. OIDC and
instance responses include `revision`; operator overrides include
`overrides.revisions.media_job_admission_overrides`. Missing revisions return
428 (`settings_revision_required`), including for existing automation clients.
Stale revisions return 409 (`settings_conflict`), preserve the current settings,
and create no success audit event. Reload before resubmitting a draft. Revisions
advance on every write, even if two writes occur in the same second.

Disabling OIDC while the saved sign-in choice is OIDC-only is rejected. Select
built-in or both first. Sign-in choice writes and OIDC writes validate this under
the database write lock. The existing owner recovery flow through admin-key
sign-in remains available and unchanged.

Existing settings receive their first revision automatically during database
initialization at startup. The idempotent `INSERT OR IGNORE` backfill uses the
existing settings generation table and preserves existing registry generations.
No operator script or schema column change is required.

Invitation inspection uses `POST /auth/api/invite` with a JSON `token` field.
The former GET query-token endpoint returns 405. The invitation page captures
and clears the fragment immediately, retains it in tab session storage for
OIDC sign-in and reloads, and clears that stored token after acceptance.

Account suspension now blocks access through account state without changing
individual app disable flags. Re-enabling an account leaves independently
disabled apps disabled; revoked keys and sessions remain revoked. Existing app
disable flags are preserved, including flags set before this change, because
their original intent cannot be recovered safely.

People shows the effective access plan from the same resolver used by session
and app authorization, with its assigned, email, domain, group, role or gateway
default source. Each active administrator grant shows its manual or OIDC-claim
source. Grant and revoke audit events include provenance; invitation acceptance
also records its first credential, without key material. Account actions record
the authenticated actor kind, including owner service accounts.
