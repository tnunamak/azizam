# Web API contract — all seven admin surfaces

The JSON contract the React client (`web/`, per `WEB_UI_STEERING.md`) uses to
render all admin surfaces. Verified against `gateway/admin_api.py` +
`gateway/admin_runtime.py` (`AdminSQLiteStore`) and the route tests in
`tests/test_admin_api.py` on 2026-06-11.

---

## Global conventions

- **Base path**: `/admin/api`. All requests are same-origin; the browser's
  session cookie is the credential (`fetch(..., { credentials: "same-origin" })`).
- **Timestamps** are epoch **seconds** as integers (`created_at`,
  `last_used_at`, `revoked_at`, `disabled_at`, `expires_at`, `last_login_at`),
  or `null` when unset. Render via `<RelativeTime>` only.
- **IDs** are prefixed strings: `usr_…`, `app_…`, `cred_…`, `svc_…`,
  `prof_…`, `aud_…`, `map_…`. Render via `<EntityRef>` only.
- **No raw key material** ever appears in list/read responses. The one-time
  full key appears exactly once, in the `201` response of a mint call, under
  `credential.api_key`.

## Error envelope (every endpoint)

Non-2xx responses are JSON:

```json
{ "error": { "code": "app_not_found", "message": "app 'app_x' not found" } }
```

Statuses used: `400` (validation: `invalid_app`, `invalid_credential`,
`invalid_profile`, `invalid_runtime_mapping`, `invalid_operator_override`,
`invalid_oidc_config`, `invalid_service`, `invalid_service_registry`,
`invalid_extension_package_source`, `invalid_extension_package`, `invalid_json`),
`401 admin_unauthenticated`, `403` (`admin_forbidden`,
`admin_csrf_required`, `admin_action_header_required`,
`admin_role_forbidden`, `service_not_editable`),
`404` (`app_not_found`, `credential_not_found`, `profile_not_found`,
`runtime_mapping_not_found`),
`409` (`profile_name_conflict`, `profile_in_use`, `service_conflict`,
`extension_package_conflict`),
`503 probe_unavailable`.

## CSRF recipe (writes from a cookie session)

Reads (`GET`) need only the session cookie. Every mutation (`POST`/`PATCH`/
`PUT`/`DELETE`) from a cookie session must send **both** headers:

```
x-ai-gateway-admin-action: 1
x-ai-gateway-csrf: <csrf_token from GET /admin/api/session>
```

Server behavior (see `_require_admin`): when the session carries a CSRF token
(it always does for real sessions), `x-ai-gateway-csrf` must match or the write
fails `403 admin_csrf_required` — the action header alone is not enough.
Bearer-key (`Authorization: Bearer oak_…`) callers need neither header. Wire
both into the single `adminFetch` in `web/src/lib/api.ts`.

---

## Surface 0 — Bootstrap (all surfaces)

### GET /admin/api/session

Bootstrap call: actor identity + CSRF token as JSON (the server-rendered admin
embeds these in HTML; this is the JS-client equivalent).

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

`csrf_token` is `""` for bearer-key callers (no CSRF needed). Fetch once on
app load; refetch after a `403 admin_csrf_required` (session rolled).

---

## Surface 1 — Overview

### GET /admin/api/overview

Single fetch that backs the entire Overview page. Returns:

- `actor` — subject/email/roles (**no** `csrf_token`; use `/session` for that).
- `counts` — quiet stat strip numbers.
- `attention` — the "things that want a look" list (single source:
  `derive_admin_attention`).
- `runtime_profile_mappings` — full list for the overview teaser.
- `access_model` — vocabulary reference (nouns, relationships).

The page also calls `GET /audit?limit=3` for the 3-row recent audit teaser;
that is a separate fetch, not embedded in the overview response.

```json
{
  "actor": {
    "subject": "usr_…",
    "email": "admin@example.com",
    "roles": ["owner"]
  },
  "counts": {
    "users": 4,
    "apps": 7,
    "profiles": 3,
    "runtime_profile_mappings": 2,
    "credentials": 12,
    "active_credentials": 9,
    "services_total": 3,
    "services_enabled": 2
  },
  "attention": [
    {
      "kind": "app",
      "severity": "problem",
      "title": "brother-laptop is disabled",
      "detail": "Re-enable it or remove it.",
      "section": "apps-keys"
    }
  ],
  "runtime_profile_mappings": [ { "...": "..." } ],
  "access_model": { "nouns": ["user", "app", "profile", "key", "runtime_mapping", "admin_role"], "..." : "..." }
}
```

`attention` item shape:
- `kind` — `"app"` | `"service"`.
- `severity` — `"problem"` (broken, needs fixing) | `"attention"` (works but
  incomplete). Items sorted problems-first.
- `title` — plain-language one-liner for the row.
- `detail` — one sentence of guidance.
- `section` — route slug the action button links to (`"apps-keys"`,
  `"services"`).

`counts.services_total` — count of services in the stored service registry
(static config; live connectivity is not probed here).
`counts.services_enabled` — count of services with `enabled: true` (or
`enabled` absent, which defaults to true).

---

## Surface 2 — Services

### GET /admin/api/operator/service-registry

```json
{
  "service_registry": {
    "schema_version": 1,
    "services": {
      "tabby": {
        "id": "tabby",
        "name": "TabbyAPI",
        "kind": "provider",
        "source": "provisioned" | "ui",
        "enabled": true,
        "capabilities": ["vision"],
        "config": { "host": "127.0.0.1", "port": 5050, "base_url": "http://127.0.0.1:5050/v1" }
      }
    },
    "protocol_frontends": { "…": { "id": "…", "name": "…", "protocol": "…", "routes": [], "intents": [], "canonical": true } },
    "workflows": { "…": { "…": "…" } },
    "capabilities": { "…": { "…": "…" } }
  }
}
```

`source` field: `"provisioned"` = from bootstrap YAML/config (read-only in UI;
lock icon); `"ui"` = created via admin API (full CRUD).

### POST /admin/api/operator/services  (add service)

Request:
```json
{
  "name": "My vLLM",
  "preset": "vllm",
  "base_url": "http://10.0.0.5:8000/v1",
  "api_key_env": "VLLM_API_KEY",
  "capabilities": ["chat.completions"]
}
```

Rules: `name` required; `base_url` OR `host` required; `api_key` (inline
secret) is rejected — use `api_key_env` (env-var name) instead.

`201` → `{ "service_registry": { …same shape as GET… } }`

`409 service_conflict` if a service with this id (normalised from `name`) already
exists. `403 service_not_editable` if it exists but is `provisioned`.

### PATCH /admin/api/operator/services/{service_id}  (edit service)

Request: same fields as POST (partial ok; `name` defaults to `service_id` when
omitted). `403 service_not_editable` for provisioned services.

`200` → `{ "service_registry": { … } }`

### DELETE /admin/api/operator/services/{service_id}  (remove service)

`200` → `{ "service_registry": { … } }`; `403` for provisioned; `404` after
second delete.

### POST /admin/api/operator/services/{service_id}/enabled  (toggle enabled)

Request: `{ "enabled": true | false }`.

`200` → `{ "service_registry": { … } }`. Works on both `provisioned` and `ui`
sources (toggle is not a content edit).

### POST /admin/api/operator/services/test  (test connection)

Request: same shape as add/edit form (base_url or host). Not persisted.

`200` → `{ "probe": { "ok": true | false, "status": 200 | null, "error": "", "url": "http://…" } }`

`503 probe_unavailable` when the process was not started with a probe injected.

### POST /admin/api/operator/services/detect-models  (detect models)

Request: same shape as test. Not persisted.

`200` → `{ "detected": { "ok": true, "models": ["gpt-4o", "gpt-4-turbo"], "error": "" } }`

`models` is empty and `ok` is false when the host is unreachable.

### GET /admin/api/operator/litellm-config  (read-only viewer)

`200` → `{ "litellm_config": { "model_list": [ { "model_name": "…", "litellm_params": { "api_base": "…", "api_key": "os.environ/KEY_NAME" } } ], "general_settings": { "…": "…" } } }`

This is the compiled LiteLLM substrate config derived from the service registry.
Secrets appear as `os.environ/…` references, never as values. Render as a
read-only mono viewer inside a collapsed disclosure.

---

## Surface 3 — Apps & keys

### GET /admin/api/apps

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

Notes:
- **Owner name is a client-side join**: apps carry `owner_user_id` only.
  Resolve against `GET /admin/api/users` (`id → display_name ?? email`).
  Shared apps have `owner_service_account_id` instead (render as "Shared").
- **Key count is a client-side derive**: count `GET /admin/api/credentials`
  rows where `app_id === app.id && !revoked_at`. There is no per-app count
  field; with both lists in React Query cache this is one selector.
- **Profile** column: use `effective_profile_name` (already resolved
  server-side; `effective_profile_source === "fallback"` means inherited
  default).
- **Apps include `default_profile_id`** so the Profiles surface can derive
  "apps using this profile" as a client-side count.

### POST /admin/api/apps  (create app)

Request: `{ "name": "required", "kind": "service" | "personal", "profile_id": "prof_…?", "owner_user_id": "usr_… (required when kind=personal)" }`

`201` → `{ "app": { …same shape as list… } }`. `400 invalid_app` on missing
name / unknown profile / personal without owner.

### PATCH /admin/api/apps/{app_id}  (rename, change profile)

Request: any subset of `{ "name": "…", "default_profile_id": "prof_… | default-local" }`
(`profile_id` accepted as alias).

`200` → `{ "app": { … } }`; `400 invalid_app` (unknown profile), `404 app_not_found`.

### DELETE /admin/api/apps/{app_id}  (disable — one-way soft delete)

`200` → `{ "app": { …, "disabled_at": 1765… } }`; `404 app_not_found`.

### POST /admin/api/apps/{app_id}/enable  (re-enable — undo for disable)

Clears `disabled_at`. Idempotent: enabling an already-active app returns `200`
with the unchanged record. This is the smallest honest undo for the optimistic
disable flow (toast + "Undo" action). No request body needed.

`200` → `{ "app": { …, "disabled_at": null } }`; `404 app_not_found`.
Requires admin auth + CSRF (write).

### GET /admin/api/credentials

Flat list of every credential — app keys **and** admin automation keys —
scrubbed of key material. The client groups it: rows with
`subject_kind === "app"` belong to the app keys table (group by `app_id`);
rows with `subject_kind === "service_account"` are the admin automation keys
subsection. There is no per-app keys endpoint; this is intentional (one fetch,
client-side grouping).

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

### POST /admin/api/apps/{app_id}/keys  (mint key)

Request: `{ "label": "human name?", "profile_id": "prof_…?", "scopes": []? }`
(`name` accepted as alias for `label`; defaults to `"API key"`).

`201` → `{ "credential": { …credential fields above…, "api_key": "opk_<full key>" } }`

`api_key` is **reveal-once**: show in the reveal dialog, never log, never
refetchable. `404 app_not_found`, `400 invalid_credential`.

### POST /admin/api/admin-keys  (admin automation key)

Request: `{ "name": "required", "role": "admin" | "owner" }`. Minting `owner`
requires the caller to hold `owner` (else `403 admin_role_forbidden`).

`201` → `{ "credential": { …, "api_key": "oak_<full key>", "role": "admin", "service_account": "<name>" } }`

### DELETE /admin/api/credentials/{credential_id}  (revoke)

`200` → `{ "credential": { …, "revoked_at": 1765… } }`; `404
credential_not_found`. (`POST /admin/api/credentials/{id}/revoke` is an
equivalent legacy alias; the React client uses the DELETE form.) Revocation is
idempotent: revoking an already-revoked key returns `200`.

---

## Surface 4 — Profiles

### GET /admin/api/profiles

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

Notes:
- `protocols`, `workflows`, `status_visibility` are extracted from the nested
  `profile` policy document by `_profile_summary` in `admin_runtime.py` — the
  surface uses these for the "Access summary" column without parsing the full
  policy document.
- The built-in fallback `default-local` is **not** in this list; selects must
  prepend it as a static option (`id: "default-local"`).
- **"Apps using it" count is client-side**: count `GET /admin/api/apps` rows
  where `default_profile_id === profile.id`.

### GET /admin/api/profiles/{profile_id}

`200` → `{ "profile": { …same shape as list item… } }`; `404 profile_not_found`.

### POST /admin/api/profiles  (create profile)

Request:
```json
{
  "name": "Family access",
  "description": "Optional",
  "profile": {
    "id": "family",
    "protocols": ["openai"],
    "status_visibility": { "level": "basic" },
    "allow": { "intents": ["chat.completions"] }
  }
}
```

`201` → `{ "profile": { … } }`. `400 invalid_profile` (bad policy doc).
`409 profile_name_conflict` if name already taken.

### PATCH /admin/api/profiles/{profile_id}  (edit profile)

Request: any subset of `{ "name": "…", "description": "…", "profile": { … } }`.

`200` → `{ "profile": { … } }`; `400 invalid_profile`, `404 profile_not_found`,
`409 profile_name_conflict`.

### DELETE /admin/api/profiles/{profile_id}  (delete profile)

`200` → `{ "profile": { … } }`; `404 profile_not_found`. `409 profile_in_use`
when apps still reference this profile — the client must explain this in the
`ConfirmDialog` and block the action until the apps are reassigned.

### POST /admin/api/policy/simulate  (test access — "trust feature")

Request:
```json
{
  "profile": "prof_…" | { "id": "…", "protocols": ["openai"], "allow": { "intents": [] } },
  "request": {
    "protocol": "openai",
    "intent": "chat.completions",
    "workflow": null,
    "model": "gpt-4o",
    "backend": null,
    "capability_exposure": "default",
    "capability_trust_tier": "stable",
    "capability_resource_tier": "interactive",
    "capability_tags": ["coder"],
    "requires_cloud_fallback": false,
    "native_passthrough": false,
    "native_operation": null,
    "native_backend": null
  }
}
```

`profile` may be a stored `prof_…` id (requires a `profile_resolver` in the
router — always present in the production gateway) **or** an inline policy dict
(useful for previewing an unsaved edit before Save).

All `request` fields are optional and default to `null`/`false`. Unknown fields
return `400 invalid_request`.

`200` →
```json
{
  "profile_id": "prof_… | <inline>",
  "decision": {
    "decision": "allowed" | "denied",
    "allowed": true | false,
    "sources": ["protocols"],
    "reasons": ["protocol openai is allowed"],
    "visibility_level": "basic"
  }
}
```

`404 profile_not_found` when a stored `prof_…` id is supplied but not found.

---

## Surface 5 — Users

### GET /admin/api/users

```json
{ "users": [ {
  "id": "usr_…",
  "issuer": "https://accounts.google.com",
  "subject": "oidc-sub-string",
  "email": "tim@example.com",
  "display_name": "Tim" | null,
  "name": "Tim" | null,
  "created_at": 1765…,
  "last_login_at": 1765… | null,
  "roles": ["owner"]
} ] }
```

`name` mirrors `display_name`. Owner cell text: `display_name ?? email`, with
`usr_…` as the quiet copyable detail. No user CRUD — identities come from the
sign-in provider; say so in the empty/explainer copy.

**Apps count** for the Users table is client-side: count `GET /admin/api/apps`
rows where `owner_user_id === user.id`.

### GET /admin/api/operator/overrides  (user-access-defaults section)

Returns the full operator overrides block. The Users surface renders
`user_access_defaults` from it. Same endpoint used by the Services and Settings
surfaces (different sub-keys).

```json
{
  "overrides": {
    "user_access_defaults": {
      "self_service_apps": true,
      "auto_create_personal_app": true
    },
    "service_availability": { "openai": { "state": "degraded", "reason": "slow", "retry_after_seconds": null } },
    "media_job_admission": {
      "disabled_intents": [],
      "disabled_resource_classes": [],
      "reserved_tokens": [],
      "max_queue_depth": null,
      "retry_after_seconds": null
    },
    "extension_package_index": { "packages": {} }
  }
}
```

### PUT /admin/api/operator/user-access-defaults  (save defaults)

Request: any subset of `{ "self_service_apps": true | false, "auto_create_personal_app": true | false }`.

`200` → `{ "user_access_defaults": { "self_service_apps": true, "auto_create_personal_app": true } }`

### GET /admin/api/runtime-profile-mappings  (default access mappings)

```json
{
  "mappings": [ {
    "id": "map_…",
    "source_kind": "oidc_group" | "oidc_role" | "email" | "domain" | "gateway_role" | "all_users",
    "source_value": "trusted-users",
    "profile_id": "prof_… | default-local",
    "created_at": 1765…,
    "created_by": "usr_…",
    "disabled_at": null
  } ]
}
```

Only active (non-disabled) mappings are returned by default.

### POST /admin/api/runtime-profile-mappings  (create mapping)

Request: `{ "source_kind": "oidc_group", "source_value": "trusted-users", "profile_id": "prof_… | default-local" }`

`201` → `{ "mapping": { … } }`. `400 invalid_runtime_mapping` (unknown profile).

### DELETE /admin/api/runtime-profile-mappings/{mapping_id}  (disable mapping)

`200` → `{ "mapping": { …, "disabled_at": 1765… } }`; `404 runtime_mapping_not_found`.

---

## Surface 6 — Audit

### GET /admin/api/audit?limit=50

Reverse-chronological list of audit events. Default limit 50, maximum 200.
There is no cursor/offset pagination — for "Load more" the client increases
`limit` and re-fetches the full window (acceptable at current volumes; add a
`cursor` parameter when the table grows).

```json
{
  "audit": [ {
    "id": "aud_…",
    "created_at": 1765…,
    "actor_kind": "admin" | "cli" | null,
    "actor_id": "usr_… | svc_… | cli",
    "action": "app.create",
    "target_kind": "app" | "profile" | "credential" | "operator_setting" | "service" | "extension_package" | "runtime_profile_mapping" | null,
    "target_id": "app_… | prof_… | …",
    "metadata": { "name": "brother-laptop", "kind": "service" } | null
  } ],
  "limit": 50
}
```

Field notes:
- `action` is the raw event-type string (`"app.create"`, `"app.disable"`,
  `"app.enable"`, `"credential.create"`, `"credential.revoke"`,
  `"profile.create"`, `"profile.update"`, `"profile.delete"`,
  `"admin_key.create"`,
  `"runtime_profile_mapping.create"`, `"runtime_profile_mapping.disable"`,
  `"operator.user_access_defaults.update"`,
  `"operator.service_availability.update"`,
  `"operator.media_job_admission.update"`,
  `"operator.service_registry.update"`,
  `"operator.service.added"`, `"operator.service.updated"`,
  `"operator.service.removed"`, `"operator.service.toggled"`,
  `"operator.extension_package.enable"`,
  `"operator.oidc_provider_config.update"`).
  Map to sentence-case human phrases client-side; display the raw value as
  `micro` mono detail.
- `actor_id` resolves to a user via `GET /admin/api/users`
  (`id === actor_id`); display as `EntityRef`. `"cli"` is a non-user actor
  (bootstrap scripts); render as "System".
- `metadata` is a free-form object specific to each action; render as
  mono JSON in the row detail Sheet.
- Results are sorted newest-first (`created_at DESC`).

`GET /admin/api/audit-events` is a canonical noun-based alias for the same
endpoint; the React client should prefer `/audit`.

---

## Surface 7 — Settings

### GET /admin/api/oidc-config

**Never returns raw secrets.** `client_secret` is replaced with `"<redacted>"`
and the companion boolean `client_secret_set` indicates whether a secret is
saved. Returns `null` when no OIDC config has been saved yet (UI shows "Not
configured").

```json
{
  "oidc_config": {
    "enabled": true,
    "issuer": "https://accounts.google.com",
    "client_id": "my-app.apps.googleusercontent.com",
    "client_secret": "<redacted>",
    "client_secret_set": true,
    "redirect_uri": "https://gateway.example.com/auth/callback",
    "scopes": ["openid", "email", "profile"],
    "allowed_email_domains": ["example.com"],
    "admin_emails": ["ops@example.com"],
    "admin_groups": [],
    "admin_roles": [],
    "authorization_endpoint": "",
    "token_endpoint": "",
    "jwks_uri": "",
    "clock_skew_seconds": 60,
    "updated_at": 1765…
  } | null
}
```

### PUT /admin/api/oidc-config  (save OIDC config)

Request: any subset of the `oidc_config` fields above. When `client_secret` is
omitted, blank, or `"<redacted>"`, the existing stored secret is preserved
(patch semantics). Unknown fields return `400 invalid_oidc_config`.

Accepted fields: `enabled`, `issuer`, `client_id`, `client_secret`,
`redirect_uri`, `scopes`, `allowed_email_domains`, `admin_emails`,
`admin_groups`, `admin_roles`, `authorization_endpoint`, `token_endpoint`,
`jwks_uri`, `clock_skew_seconds`.

`200` → `{ "oidc_config": { …same scrubbed shape… } }`

After saving, the gateway must be restarted for the new OIDC config to take
effect. Display the same "Saved — restart to apply" banner as service changes.

### GET /admin/api/extensions

```json
{
  "extension_packages": {
    "packages": {
      "demo-package": {
        "id": "demo-package",
        "version": "0.1.0",
        "source": { "type": "local_directory", "path": "/extensions/demo" },
        "enabled": true,
        "enabled_at": 1765…
      }
    }
  }
}
```

### POST /admin/api/extensions/validate  (validate before enable)

Request: `{ "source": { "type": "local_directory" | "git", "path": "…" | "url": "…", "ref": "…?" } }`

`200` → `{ "review": { "ok": true | false, "package": { "id": "…", "version": "…" }, "errors": [], "warnings": [], "requires_code_trust": false, "resolved_source": { … }, "package_hash": "sha256…", "object_counts": { "services": 1, "protocol_frontends": 0, "capabilities": 0, "workflows": 0 } } }`

### POST /admin/api/extensions/enable  (install extension)

Request: `{ "source": { … }, "review": { "resolved_source": { "resolved_ref": "git-sha" } } }`.
`review.resolved_source.resolved_ref` pins the exact git SHA from the prior
validate call to prevent TOCTOU.

`200` → `{ "extension_package": { … }, "review": { … }, "service_registry": { … }, "extension_packages": { … } }`

`400 invalid_extension_package` if the package fails validation.
`409 extension_package_conflict` if the package's services conflict with
existing registry entries.

### GET /admin/api/operator/overrides  (media-job-admission section)

Same endpoint as Users surface. The Settings surface reads
`overrides.media_job_admission`:

```json
{
  "disabled_intents": ["video.generate"],
  "disabled_resource_classes": ["gpu"],
  "reserved_tokens": ["gpu"],
  "max_queue_depth": 5,
  "retry_after_seconds": 30
}
```

### PUT /admin/api/operator/media-job-admission  (save media admission)

Request: any subset of `{ "disabled_intents": [], "disabled_resource_classes": [], "reserved_tokens": [], "max_queue_depth": null | int≥0, "retry_after_seconds": null | int≥0 }`.

`200` → `{ "media_job_admission": { …same shape… } }`

### PUT /admin/api/operator/service-availability  (service status override)

Request: `{ "overrides": { "<service_id>": { "state": "available" | "degraded" | "draining" | "maintenance" | "disabled", "reason": "…?", "retry_after_seconds": null | int≥0 } } }`

Setting `state: "available"` with no `reason` clears any existing override for
that service (idempotent clear).

`200` → `{ "service_availability": { "<service_id>": { "state": "…", "reason": null, "retry_after_seconds": null } } }`
