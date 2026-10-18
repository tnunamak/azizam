# Sign-in sessions

How long a browser sign-in lasts, when it renews, how to end it, and which
actions ask you to confirm your password again. This is the sign-in of the web
app and the installed phone app (PWA). API keys (`opk_...`, `oak_...`) are not
sessions and follow none of these rules.

## How long a sign-in lasts

A sign-in works like a phone app. It stays active while you use it.

| Setting | Default | Allowed | Meaning |
|---|---|---|---|
| Idle timeout | 30 days | 1 to 90 days | A sign-in that is not used for this long ends. |
| Absolute lifetime | 90 days | 1 to 365 days | A sign-in ends this long after the original sign-in, however much it is used. You sign in again. |
| Confirmation window | 15 minutes | 1 to 1440 minutes | How recent your last sign-in or password confirmation must be for the risky actions listed below. |

The idle timeout cannot be longer than the absolute lifetime. The owner changes
the three values in Settings ("Sign-in sessions"), or with `GET` and `PUT /admin/api/settings/sessions`
(an owner key can do this; a browser session needs a recent confirmation).
A change applies at once to sessions that are already signed in, in the
stricter direction: a shorter idle timeout or absolute lifetime signs out a
session that is past the new value. A longer idle timeout reaches a session when
it next renews. A longer absolute lifetime applies to new sign-ins only.

### Renewal

Any request that carries a live session cookie can renew it. A session renews
only when more than a quarter of the idle timeout has passed since it was last
renewed (7.5 days with the default), so a busy session does not re-issue its
cookie on every request. A renewal sets a new expiry and the same session
identity. It never goes past the absolute lifetime. The server also extends its
own record of the session first, so a cookie never outlives the record.

Renewing does not count as confirming who you are. Only a sign-in or a password
confirmation does.

### Sessions from before this change

Cookies that were issued with the old fixed 8-hour life stay valid until that
expiry. The first request that uses one upgrades it in place (same session,
new sliding expiry), so people are not signed out when the old 8 hours end. The
absolute lifetime is counted from the original sign-in.

### Provider-granted administrator roles

A role that the identity provider granted (an administrator group or role
claim) still counts only while a sign-in confirmed it within the last 8 hours
(`admin.oidc.cookie.max_age_seconds`). A longer session does not extend this.
An administrator made by hand, and the owner, are not affected. A person with a
provider-granted role who stays signed in for weeks will lose administrator
access after 8 hours until they sign in with the provider again.

## Ending sessions

- **Sign out** ends the session on this device. A copy of its cookie stops working.
- **Sign out everywhere** (on the "Your settings" page in the account menu, or
  `POST /auth/api/sign-out-everywhere` with the CSRF header) ends every session the person has on every device,
  including this one, and the keys that were made on those sessions for the Try page.
- A **password change or reset** ends the person's other sessions. When the owner
  sets their own password in Settings, the session that made the change stays.
- **Removing or disabling a person** ends all their sessions.
- A refused provider sign-in ends that person's sessions.

The server keeps a record of each registered session (`account_sessions`) and of
revoked ones (`account_session_revocations`). Once a person has used "Sign out
everywhere", a password change or a reset, or has been disabled or removed, only
sessions with a live record count for them, so an old cookie that is still
inside its expiry stops working. A single sign-out revokes that session's id
until the longest time any copy of its cookie could live.

The time a session is last used is recorded when it renews, so with the
defaults an unused session ends between 22 and 30 days after its last use.

A browser session that was claimed by pasting an admin key at `/setup` is not a
person's sign-in. It is not recorded, never renews, and ends after the fixed
8-hour cookie life (`admin.oidc.cookie.max_age_seconds`), whatever happens to the key.

## Actions that ask you to confirm

A session whose last sign-in or confirmation is older than the confirmation
window gets `401` with the code `reauth_required` on the actions below. The web
app shows a password prompt (or sends you to your identity provider), then
repeats the action.

- Export the configuration, or the effective settings.
- Add a provider, or replace or clear a provider key. Create or delete a LiteLLM key. Create an admin key or an App key.
- Trust an address for an App, or enable an extension package.
- Turn on or off running commands on this computer.
- Invite a person, copy an invitation link, change or remove a person, create a password reset link.
- Change sign-in settings (providers, rules, the owner's password).
- Change the session settings.

The list is `SENSITIVE_ROUTES` in `gateway/step_up.py`. A test checks that every
entry names a real route. A pasted engine key in a general engine edit is not on
the list, because that route is shared with ordinary engine edits.

The response names the ways to confirm:

```json
{"error": {"code": "reauth_required", "message": "Confirm it's you to do this.",
  "reauth": {"methods": ["password", "oidc"], "window_seconds": 900, "oidc_url": "/login?reauth=1"}}}
```

- `password`: `POST /auth/api/reauth` with `{"password": "..."}` and the CSRF
  header. A good password sets the confirmation time and re-issues the cookie.
  It shares the sign-in throttle.
- `oidc`: open `/login?reauth=1&return_to=<path>`. The provider is asked to log
  the person in again (`prompt=login`, `max_age=0`). If the provider reports
  `auth_time`, it must be from after the request began; a provider that does not
  report it is taken at its word. The sign-in must be by the same person. The
  new session replaces the old one. An ordinary provider sign-in also counts as
  confirmed only as far as the provider says: with `auth_time`, a silent single
  sign-on that reuses an older login does not count as a recent sign-in.
- `sign_in`: neither is possible for this session (for example it came from an
  admin key claim). Sign in again.

API keys are never asked. An admin key with the owner role can still do all of
these actions headless. A request that carries a valid session cookie is treated
as that session, even if it also carries a key.

## Cookie

The cookie is `opx_admin_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and
`Secure` unless the documented plain-HTTP first-run setting is on
(`admin.oidc.cookie.secure: false`). Its `Max-Age` is the time left to its
expiry. CSRF protection is unchanged: the token is bound to the session
identity, which a renewal keeps.

## Phone app (PWA)

The web manifest (`web/src/app/manifest.ts`) declares a standalone app. The web
app has no service worker and does not clear cookies, `localStorage`,
`sessionStorage` or caches when it loads, so opening the app never signs anyone
out. The only things that end a sign-in are the rules above.

iOS may delete website data of a home-screen app that has not been opened for
about 7 days (this is iOS behavior, not Azizam's, and Apple's rules change).
The session cookie is set by the server, but if a phone is signed out after a
week without use, check the idle timeout first, then expect to sign in again.
This document was written without testing on a physical iPhone.
