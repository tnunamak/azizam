# Web API contract — Admin → Apps & Keys

The JSON contract the React client (`web/`, per `WEB_UI_STEERING.md`) uses to
render the Apps & Keys surface. Verified against `gateway/admin_api.py` +
`gateway/admin_runtime.py` (`AdminSQLiteStore`) and the route tests in
`tests/test_admin_api.py` on 2026-06-11.

Conventions that apply to everything below:

- **Base path**: `/admin/api`. All requests are same-origin; the browser's
  session cookie is the credential (`fetch(..., { credentials: "same-origin" })`).
- **Timestamps** are epoch **seconds** as integers (`created_at`,
  `last_used_at`, `revoked_at`, `disabled_at`, `expires_at`, `last_login_at`),
  or `null` when unset. Render via `<RelativeTime>` only.
- **IDs** are prefixed strings: `usr_…`, `app_…`, `cred_…`, `svc_…`. Render via
  `<EntityRef>` only.
- **No raw key material** ever appears in list/read responses. The one-time
  full key appears exactly once, in the `201` response of a mint call, under
  `credential.api_key`.

## Error envelope (every endpoint)

Non-2xx responses are JSON:

```json
{ "error": { "code": "app_not_found", "message": "app 'app_x' not found" } }
```

Statuses used: `400` (validation, e.g. `invalid_app`, `invalid_credential`),
`401 admin_unauthenticated`, `403` (`admin_forbidden`,
`admin_csrf_required`, `admin_action_header_required`,
`admin_role_forbidden`), `404` (`app_not_found`, `credential_not_found`,
`profile_not_found`), `409` (profile conflicts).

## CSRF recipe (writes from a cookie session)

Reads (`GET`) need only the cookie. Every mutation (`POST`/`PATCH`/`PUT`/
`DELETE`) from a cookie session must send **both** headers:

```
x-ai-gateway-admin-action: 1
x-ai-gateway-csrf: <csrf_token from GET /admin/api/session>
```

Server behavior (see `_require_admin`): when the session carries a CSRF token
(it always does for real sessions), `x-ai-gateway-csrf` must match or the
write fails `403 admin_csrf_required` — the action header alone is not
enough. Bearer-key (`Authorization: Bearer oak_…`) callers need neither
header. Wire both into the single `adminFetch` in `web/src/lib/api.ts`.

## GET /admin/api/session  ← added for the React client

Bootstrap call: actor identity + CSRF token as JSON (the server-rendered
admin embeds these in HTML; this is the JS-client equivalent).

```json
{
  "actor": {
    "subject": "usr_1a2b…",
    "email": "owner@example.com",
    "roles": ["owner"],
    "csrf_token": "tok…"
  }
}
```

`csrf_token` is `""` for bearer-key callers (no CSRF needed there). Fetch
once on app load; refetch after a 403 `admin_csrf_required` (session rolled).

## GET /admin/api/apps

```json
{ "apps": [ {
  "id": "app_…",
  "kind": "service" | "personal",
  "name": "brother-laptop",
  "owner_user_id": "usr_…" | null,
  "owner_service_account_id": "svc_…" | null,
  "default_profile_id": "prof_…" | null,
  "created_at": 1765432100,
  "disabled_at": null,
  "effective_profile_id": "prof_… | default-local",
  "effective_profile_name": "Trusted local",
  "effective_profile_source": "app_default" | "fallback"
} ] }
```

Notes for the surface:

- **Owner name is a client-side join**: apps carry `owner_user_id` only.
  Resolve against `GET /admin/api/users` (`id → display_name ?? email`).
  Shared apps have `owner_service_account_id` instead (render as "Shared").
- **Key count is a client-side derive**: count `GET /admin/api/credentials`
  rows where `app_id === app.id && !revoked_at`. There is no per-app count
  field; with both lists in React Query cache this is one selector.
- Profile column: use `effective_profile_name` (already resolved server-side;
  `effective_profile_source === "fallback"` means inherited default).

## POST /admin/api/apps  (Create app dialog)

Request: `{ "name": "required", "kind": "service" | "personal", "profile_id": "prof_…?", "owner_user_id": "usr_… (required when kind=personal)" }`

`201` → `{ "app": { …same shape as list… } }`. `400 invalid_app` on missing
name / unknown profile / personal without owner.

## PATCH /admin/api/apps/{app_id}  (rename, Change profile dialog)

Request: any of `{ "name": "…", "default_profile_id": "prof_… | default-local" }`
(`profile_id` accepted as alias). `200` → `{ "app": {…} }`;
`400 invalid_app` (unknown profile), `404 app_not_found`.

## DELETE /admin/api/apps/{app_id}  (Disable, not delete)

`200` → `{ "app": { …, "disabled_at": 1765… } }`; `404 app_not_found`.

## GET /admin/api/users

```json
{ "users": [ {
  "id": "usr_…",
  "issuer": "https://…",
  "subject": "oidc-sub",
  "email": "tim@example.com",
  "display_name": "Tim" | null,
  "name": "Tim" | null,
  "created_at": 1765…,
  "last_login_at": 1765… | null,
  "roles": ["owner"]
} ] }
```

`name` mirrors `display_name`. Owner cell text: `display_name ?? email`,
with `usr_…` as the quiet copyable detail.

## GET /admin/api/credentials  ← added for the React client

**Flat list** of every credential — app keys *and* admin automation keys —
scrubbed of key material. The client groups it: rows with
`subject_kind === "app"` belong to the app keys table (group by `app_id`);
rows with `subject_kind === "service_account"` (or `"user"`) are the admin
automation keys subsection. There is no per-app keys endpoint; this is
intentional (one fetch, client-side grouping).

```json
{ "credentials": [ {
  "id": "cred_…",
  "subject_kind": "app" | "service_account" | "user",
  "subject_id": "app_… | svc_… | usr_…",
  "app_id": "app_…" | null,
  "profile_id": "prof_…" | null,
  "prefix": "opk_AbC123…",
  "display_name": "first key",
  "scopes": [],
  "created_at": 1765…,
  "expires_at": null,
  "revoked_at": 1765… | null,
  "last_used_at": 1765… | null
} ] }
```

- `prefix` = first 18 chars of the full key (`opk_` app keys, `oak_` admin
  keys) — render via `<SecretMask>`.
- `last_used_at` is real data (touched on data-plane auth, write-coarsened);
  `null` = never used.
- Status badge: `revoked_at` set → revoked; else active.

## POST /admin/api/apps/{app_id}/keys  (Mint key dialog)

Request: `{ "label": "human name?", "profile_id": "prof_…?", "scopes": []? }`
(`name` accepted as alias for `label`; defaults to "API key").

`201` → `{ "credential": { …credential fields above…, "api_key": "opk_<full key>" } }`

`api_key` is **reveal-once**: show in the reveal dialog, never log, never
refetchable. `404 app_not_found`, `400 invalid_credential`.

## POST /admin/api/admin-keys  (admin automation subsection)

Request: `{ "name": "required", "role": "admin" | "owner" }`. Minting
`owner` requires the caller to hold `owner` (else `403 admin_role_forbidden`).

`201` → `{ "credential": { …, "api_key": "oak_<full key>", "role": "admin", "service_account": "<name>" } }`

## DELETE /admin/api/credentials/{credential_id}  (Revoke)

`200` → `{ "credential": { …, "revoked_at": 1765… } }`; `404
credential_not_found`. (`POST /admin/api/credentials/{id}/revoke` is an
equivalent legacy alias; the React client uses the DELETE form.) Revocation
is idempotent-ish: revoking an already-revoked key returns `200`.

## GET /admin/api/profiles  (profile selects in Create app / Change profile)

```json
{ "profiles": [ {
  "id": "prof_…",
  "name": "Trusted local",
  "description": "…" | null,
  "created_at": 1765…,
  "profile": { …policy document… },
  "protocols": ["openai"],
  "workflows": [],
  "status_visibility": "basic"
} ] }
```

The built-in fallback `default-local` is **not** in this list; selects must
prepend it as a static option (`id: "default-local"`).

## GET /admin/api/overview  (optional, attention banner)

Returns `actor` (subject/email/roles — **no** `csrf_token`; use `/session`),
`counts` (`users`, `apps`, `profiles`, `runtime_profile_mappings`,
`credentials`, `active_credentials`), `attention`
(`[{kind, severity: "problem"|"attention", title, detail, section}]`), and
`runtime_profile_mappings`. The Apps & Keys surface doesn't need it, but
`attention` items with `section === "apps-keys"` can back a banner.
