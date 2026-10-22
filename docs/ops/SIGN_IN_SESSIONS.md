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
cookie on every request. A renewal sets a new expiry and **a new session id**. It never goes past the
absolute lifetime. The server writes the new id first, so a cookie never
outlives its record.

Renewing does not count as confirming who you are. Only a sign-in or a password
confirmation does.

### Copied cookies and lost responses (decisions D35, D35b)

All ids that descend from one sign-in are its *family*. A renewal retires the
old id and keeps it for about 60 seconds, so a request that was already in
flight when the browser swapped cookies still works. A retired id never renews.
The CSRF token belongs to the family, so a page that is open across a renewal
keeps working. Confirming a password does the same swap.

The server notes the first request that carries each id (`used_at` on its row in
`account_session_lineage`, one write per id). After the 60 seconds, showing a
retired id has two possible causes, and the gateway tells them apart by that
note:

- **No later id of the sign-in was ever used.** The response that carried the
  new id was lost (a phone that was suspended, a dropped connection), so the
  person is still the only holder. The gateway signs them in again: it issues a
  new id in the same family, retires the old one again (a new 60-second grace
  window) and marks the id that never arrived as replaced. The person stays
  signed in; their Try keys and CSRF token are unchanged. Nothing is written to
  the audit log.
- **A later id was used.** Two holders exist. The gateway **revokes the whole
  family** (the person's other sign-ins are not touched), ends the keys made on it
  for the Try page, writes `account.session.reuse_detected` to the audit log and
  asks for a fresh sign-in. An id that a reissue replaced is treated the same
  way when it is shown later: it was handed out, so someone received it.

Several requests with the same retired id (a phone that wakes up and fires a
burst) make one new id. The reissue is a single database transaction that checks
the state again, so the first request to commit gets the new cookie, and the
others find the old id inside its new grace window: they are served, get no
cookie and do not renew. If the response with the new id is lost as well, the
next request after the window reissues again.

Cost of this rule: a thief who shows a stolen retired id before the person has
used their newer id also gets a session. The person's next request shows the id
that was replaced and ends both, so detection comes at the person's next use
rather than at the thief's first.

### Sessions from before this change

Cookies that were issued with the old fixed 8-hour life stay valid until that
expiry. The first request that uses one upgrades it (new session id in the same
family, new sliding expiry, and a server record, which the old cookie did not
have), so people are not signed out when the old 8 hours end. The absolute
lifetime is counted from the original sign-in. An old cookie that a key claim
made (its subject is a service account, or its email is `local-admin`) is not
upgraded: it ends at its 8 hours.

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
  including this one, and the keys that were made on those sessions for the Try page. A key made
  by "Make a key" on an App is not a session key: it stays until it is revoked, which is why making
  one asks you to confirm (below).
- A **password change or reset** ends the person's other sessions. When the owner
  sets their own password in Settings, the session that made the change stays.
- **Removing or disabling a person** ends all their sessions.
- A refused provider sign-in ends that person's sessions.

The server keeps a record of each registered session (`account_sessions`), of the
ids a sign-in has held since its first renewal (`account_session_lineage`) and of
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
repeats the action. Owners, administrators and members are asked alike, except
where noted (decision D36).

- Export the configuration, or the effective settings (the inline view and the download).
- Make a credential: an App key (administrator or the person's own, **members too**), an admin
  key, a LiteLLM key, the first key on the setup page. A new Try key asks owners and
  administrators only; the Try page reuses the key it holds without asking, and a member's Try key
  ends with their sign-in. Allowing an MCP client to connect asks (refusing one does not).
- Add a provider, or replace or clear a provider key.
- Change where a stored credential is sent or what it is: any save to the service registry that
  adds, replaces, clears or moves a key, sets a lifecycle driver or changes the address of an
  engine that holds a key (`credential_binding_changes`). This covers the whole-registry save, adding
  or editing a service, the engine page, fixing a left-out record, secret commands and enabling an
  extension package. A save that only renames or switches a service does not ask.
- Trust an address for an App, or enable an extension package.
- Turn on or off running commands on this computer.
- Invite a person, copy an invitation link, change or remove a person, create a password reset link.
- Change sign-in settings (providers, rules, the owner's password).
- Change the session settings.

The lists are `SENSITIVE_ROUTES` (the middleware asks) and `HANDLER_GATED` (the handler asks,
because only some requests to the route are risky) in `gateway/step_up.py`.
`tests/test_step_up_coverage.py` finds the risky routes in the running app, by path and by the calls a
handler makes (and the calls those make, up to four steps down, through the repo's own functions it can
resolve: `tests/support/call_graph.py`), and fails for one that is in neither list and has not been
reviewed as safe. It cannot follow a call through a local variable or a table of callbacks; a handler that
does its risky work that way must be listed by hand.

The response names the ways to confirm:

```json
{"error": {"code": "reauth_required", "message": "Confirm it's you to do this.",
  "reauth": {"methods": ["password", "oidc"], "window_seconds": 900, "oidc_url": "/login?reauth=1"}}}
```

- `password`: `POST /auth/api/reauth` with `{"password": "..."}` and the CSRF
  header. A good password sets the confirmation time and re-issues the cookie.
  It shares the sign-in throttle, which counts every attempt before any password is
  hashed, also from a person who has no password (that answer is `409`).
- `oidc`: open `/login?reauth=1&return_to=<path>`. The provider is asked to log
  the person in again (`prompt=login`, `max_age=0`). The provider must report
  `auth_time`, from after the request began; a response without it, or with an
  older one, does not confirm anyone. The sign-in must be by the same person. The
  new session replaces the old one. **Clock-skew allowance:** the provider's clock is not ours,
  so an `auth_time` up to 60 seconds before the request began still counts
  (`AUTH_TIME_CLOCK_SKEW_SECONDS` in `gateway/admin_oidc.py`; decision D40). One older than that does not.
- `sign_in`: neither is possible for this session (for example it came from an
  admin key claim). Sign in again.

API keys are never asked. An admin key with the owner role can still do all of
these actions headless. A request that carries a valid session cookie is treated
as that session, even if it also carries a key.

### A provider that does not report the sign-in time (decisions D40, D54)

Every sign-in from the sign-in page asks the provider to log the person in again (`prompt=login`,
`max_age=0`). The provider must then report `auth_time`, and Azizam accepts it only when it is at or after
the time the sign-in began, less the 60-second clock-skew allowance. Without such an `auth_time`, Azizam
signs no one in and returns the person to the sign-in page with `error=not_fresh`. A provider that ignores
`max_age` and leaves `auth_time` out therefore stops every sign-in from the page. Before you turn a
provider on, check the provider test in Settings: it reports whether the provider asks for a password
every time.

A session made before this change may carry no sign-in time. Azizam treats it as signed in but never
recent, and every action in the list above asks the person to confirm. They can confirm with their Azizam
password, or through the provider when it reports `auth_time` for the explicit confirmation (which asks
with `max_age=0`).

If the provider answers the explicit confirmation with no `auth_time` too, nobody can be confirmed
through it. The person returns to the sign-in page with a plain message: the sign-in provider does not
tell Azizam when they signed in, so Azizam cannot tell they just confirmed; confirm with the Azizam
password instead, or use an admin key. A person with no password and such a provider cannot set a
password in the browser (setting one is also a confirmed action). An admin key does it, or the
provider's administrator turns on `auth_time` for this client. Sign-ins by password or by an admin key
are not affected: the sign-in itself is the proof.

### Making a credential after signing out (decision D41)

Every credential a browser sign-in creates (an App key, an admin key, a Try key, a lasting MCP
connection, an invitation or reset link, the owner's password) checks, inside the database transaction
that writes it, that the sign-in is still live. A request that passed its cookie check a moment before
"Sign out everywhere" committed therefore gets `401` (`login_required`) and writes nothing.

The same check runs inside every service-registry save made from a browser sign-in (the registry
save, adding, editing, enabling or removing an engine or provider, secret commands, fixing a left-out
record, enabling or removing an extension package). Such a save can move where a stored key is sent, so a
sign-out that committed between its binding check and its commit stops it.

Stored secrets follow the same rule: a provider key, an engine's key, the secret entries of a record and
the identity provider's client secret are stored, replaced or deleted only when the sign-in that asks is
still live. So are a trusted address (it lets a device in with no key) and the link of an identity-provider
account to the owner. A request that is refused part-way (the sign-out lands between two of its secret
writes) takes back the secrets it had already stored, so a refused request leaves no secret behind; that
clean-up carries no sign-in and runs even after sign-out.

A role is granted to a browser sign-in's request in one place only: the dashboard's admin key. The service
account, its role and its key are made in one transaction that checks the sign-in first, so a signed-out
request makes none of them. The other role grants are the command line, the first-owner claims at setup and
the identity provider's group claims at sign-in; no signed-in browser asks for those.

### A write sent with a retired session id

A renewal replaces the session id, and the old id still works for 60 seconds (see "Copied cookies and lost
responses"). During that window, and after a lost response until the gateway reissues, the old id serves
reads but is not "live" for a write that makes a credential or a stored secret. Such a write answers
`401 login_required` and writes nothing, although the person is still signed in. It is rare: it needs a
credential request in flight at the moment of a renewal, or a phone that woke up with an id whose
successor never arrived. The web app sends such a write once more at once; that second request carries the
cookie the browser holds by then, and it succeeds. A client that is not the web app (a script holding a
copied cookie) should do the same, or sign in again. Only writes that make or change a credential are
affected, and a `401 login_required` for one never means that something was written.

"Live" includes the deadlines. A request whose session ran past its idle timeout or its absolute
lifetime while the request was in flight is refused too, whether or not the server requires a stored
record for that person's sessions: the cookie's deadlines and the stored row's deadline are both
checked, and the deadline second itself counts as ended.
## Cookie

The cookie is `opx_admin_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and
`Secure` unless the documented plain-HTTP first-run setting is on
(`admin.oidc.cookie.secure: false`). Its `Max-Age` is the time left to its
expiry. CSRF protection is unchanged: the token is bound to the sign-in (the
family), which a renewal keeps.

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
