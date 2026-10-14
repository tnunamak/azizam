# Web API contract — all eight admin surfaces

The binding JSON contract the React client (`web/`, per
`WEB_UI_STEERING.md`) uses to render all admin surfaces. It supersedes earlier
seven-surface and backend-specific UI descriptions.

---

## Global conventions

- **Base path**: `/admin/api`. All requests are same-origin; the browser's
  session cookie is the credential (`fetch(..., { credentials: "same-origin" })`).
- **Timestamps** are epoch **seconds** as integers (`created_at`,
  `last_used_at`, `revoked_at`, `disabled_at`, `expires_at`, `last_login_at`),
  or `null` when unset. Render via `<RelativeTime>` only.
- **IDs** are prefixed strings: `usr_…`, `app_…`, `cred_…`, `svc_…`,
  `aud_…`. Render via `<EntityRef>` only; access-plan IDs are opaque.
- **No raw key material** ever appears in list/read responses. The one-time
  full key appears exactly once, in the `201` response of a mint call, under
  `credential.api_key`.
- **Principals are distinct.** A person is a human/OIDC identity. A service
  principal is automation. Do not count, label, or resolve one as the other.

## Error envelope (every endpoint)

Non-2xx responses are JSON:

```json
{ "error": { "code": "app_not_found", "message": "app 'app_x' not found" } }
```

Statuses used: `400` (validation: `invalid_app`, `invalid_credential`,
`invalid_access_plan`, `invalid_operator_override`,
`invalid_oidc_config`, `invalid_service`, `invalid_service_registry`,
`invalid_extension_package_source`, `invalid_extension_package`, `invalid_json`),
`401 admin_unauthenticated`, `403` (`admin_forbidden`,
`admin_csrf_required`, `admin_action_header_required`,
`admin_role_forbidden`, `service_not_editable`),
`404` (`app_not_found`, `credential_not_found`, `access_plan_not_found`),
`409` (`access_plan_name_conflict`, `access_plan_in_use`, `service_conflict`,
`extension_package_conflict`),
`503 probe_unavailable`.

## CSRF recipe (writes from a cookie session)

Reads (`GET`) need only the session cookie. Every mutation (`POST`/`PATCH`/
`PUT`/`DELETE`) from a cookie session must send **both** headers:

```
x-azizam-admin-action: 1
x-azizam-csrf: <csrf_token from GET /admin/api/session>
```

Server behavior (see `_require_admin`): when the session carries a CSRF token
(it always does for real sessions), `x-azizam-csrf` must match or the write
fails `403 admin_csrf_required` — the action header alone is not enough.
Bearer-key (`Authorization: Bearer oak_…`) callers need neither header. Wire
both into the single `adminFetch` in `web/src/lib/api.ts`.

## Canonical admin navigation contract

The React client has exactly these shared link helpers:

- `adminStatusHref(serviceId?)` links to diagnosis. With an ID it focuses that
  service on Status; without one it opens Status.
- `adminServiceHref(serviceId)` links to the Services configuration surface.
- `adminTargetHref(targetKind, targetId)` is the one configuration-target
  mapping. Its `service` mapping is `adminServiceHref`, not
  `adminStatusHref`.

The canonical target identity is exactly `target_kind` + `target_id`; an
optional `target_name` is display-only. API payloads must not add `entity_*`
duplicates or another target identity. A compatibility `href`, if present,
must equal `adminTargetHref(target_kind, target_id)` exactly. A system advisory
has neither target fields nor `href`.

For the canonical `service` query parameter, the client matches the exact
stable `service.id` only. It does not normalize, or match, a display name or
stored configuration name. A server migration may emit a separately named
legacy parameter if compatibility is required; it must not overload the
canonical target parameter.

Historical: earlier releases redirected `/admin/status` to `/ui/admin/status/`
with the query string preserved. Both routes are gone. The Status page is now
`/status/` (for example, `/status/?service=svc_1`), and `/admin/<section>`
browser aliases no longer exist. The JSON routes under `/admin/api/*` are
unchanged.

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

## Surface 1 — Status

### GET /admin/api/status

The single current health projection for Status, Overview, Services, shell
status, and current advisories. Do not merge this response with a stale
registry-only count or make a second all-clear calculation in the client.

```json
{
  "status": {
    "…": "raw cached status fields",
    "current_health": {
      "state": "ok" | "warn" | "bad" | "unavailable" | "empty" | "inactive",
      "tone": "ok" | "warn" | "bad" | "neutral",
      "label": "Operational",
      "detail": "Current configured-service health is normal.",
      "observation_available": true,
      "incomplete": {
        "observations": ["service display name"],
        "routing": ["service display name"]
      },
      "signals": [ {
        "source": "api" | "host" | "…",
        "tone": "warn" | "bad",
        "reasons": ["producer-authored reason"]
      } ],
      "services": [ {
        "service_id": "svc_…",
        "name": "Text generation",
        "configured": true,
        "enabled": true,
        "role": "required" | "optional" | "standby",
        "routable": true | null,
        "observation": "up" | "degraded" | "down" | "standby_stopped" | "disabled" | "unavailable",
        "tone": "ok" | "warn" | "bad" | "neutral",
        "last_error": null
      } ]
    }
  }
}
```

`state: "ok"` is the only green state. `unavailable` means current observation
could not be obtained and must stay visibly distinct from a green status and
from an empty configured-service list; it is never converted to an empty
advisory list or an all-clear. `empty` is "No services configured"; `inactive`
is a neutral state when all configured services are disabled or stopped in
standby. `configured`, `enabled`, role, observation, and routability answer
different questions and remain separate fields. A stopped standby service and
a disabled service are not represented as a required-service failure.
`signals` preserves independent producer facts (for example API or host
health) as structured data. Legacy raw aggregate banners do not override the
more-specific service reduction and are never parsed for source meaning.
`routable: true` currently means only that the registry has a non-empty model
mapping, which declares a mapped model route. `routable: null` means the
projection has no global routing fact. Empty and absent `config.models` are
both `null`; a capability list or `native_passthrough` flag is also `null`
until a producer supplies a comprehensive route-family fact with all dispatch
prerequisites. The projection does not emit `false` until a producer supplies
a comprehensive fact covering every supported route family.
`incomplete` is optional coverage detail: `observations` identifies services
without a current live observation and `routing` identifies live-up services
without a global route fact. It never reduces a simultaneous known `warn` or
`bad` aggregate; that severity remains authoritative while the incomplete fact
and detail stay visible.

## Surface 2 — Overview

### GET /admin/api/overview

Single fetch that backs the entire Overview page. Returns:

- `actor` — subject/email/roles (**no** `csrf_token`; use `/session` for that).
- `counts` — quiet stat strip numbers.
- `current_health` — the exact current health projection from Status (not a second
  summary calculation).
- `attention` — the "things that want a look" list (single source:
  `derive_admin_attention`).
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
    "service_principals": 1,
    "apps": 7,
    "access_plans": 3,
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
      "target_kind": "app",
      "target_id": "app_…",
      "target_name": "brother-laptop",
      "href": "/admin/apps?app=app_…"
    }
  ],
  "current_health": { "…": "exact GET /admin/api/status.current_health shape" },
  "access_model": { "nouns": ["user", "app", "access_plan", "key", "admin_role"], "..." : "..." }
}
```

`attention` item shape:
- `kind` — advisory category, such as `"app"`, `"service"`, or `"system"`.
- `severity` — `"problem"` (broken, needs fixing) | `"attention"` (works but
  incomplete). Items sorted problems-first.
- `title` — plain-language one-liner for the row.
- `detail` — one sentence of guidance.
- `target_kind` + `target_id` — the only canonical object target. They are
  present together or both absent.
- `target_name` — optional display-only text. It never identifies or routes an
  object.
- `href` — compatibility field only. When a target exists it is exactly the
  result of `adminTargetHref(target_kind, target_id)`; clients may recompute
  it. It is absent when the tuple is absent.

A system advisory is not an object link: it has no target tuple, no
`target_name`, and no `href`. The client must not invent a section link for it.

`counts.users` counts human/OIDC identities only; `counts.service_principals`
counts automation only. `counts.services_total` and `counts.services_enabled`
are configuration facts, not live-health claims. Current health language comes
only from `current_health`.

---

## Surface 3 — Services

### GET /admin/api/operator/service-registry

```json
{
  "service_registry": {
    "schema_version": 1,
    "services": {
      "text-service": {
        "id": "text-service",
        "name": "Text service",
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

`source` is provenance (history), not permission. A record may expose
`editable: false` only when the API also supplies the generic derived-field
reason. The UI renders that reason and must not infer it from an ID, kind, or
engine name. Config-imported rows removed through the API are not re-imported:
the registry is the source of truth after its initial seed.

**`models` field** in `config`: list of `{ model_group, upstream_model }`
deployment mappings. It makes a service's public model names routable and
round-trips through PATCH without a client-side provider branch.

### GET /admin/api/operator/service-presets

Returns optional service-record presets and the canonical capability vocabulary.
They are operator-configurable data; the UI consumes them without encoding a
provider catalog or choosing a preferred/default preset.

`200` →
```json
{
  "presets": [
    {
      "id": "custom-http",
      "label": "Custom HTTP service",
      "base_path": null,
      "default_port": null,
      "protocol": null,
      "capabilities": ["chat", "completions"],
      "native_passthrough": true
    }
  ],
  "capabilities": [
    { "id": "chat", "label": "Chat" },
    { "id": "completions", "label": "Completions" },
    { "id": "vision", "label": "Vision" },
    { "id": "embeddings", "label": "Embeddings" },
    { "id": "image", "label": "Images" },
    { "id": "audio", "label": "Audio" },
    { "id": "speech", "label": "Speech" },
    { "id": "generations", "label": "Generations" },
    { "id": "videos", "label": "Videos" }
  ]
}
```

Every preset has: `id`, `label`, `base_path`, `default_port`, `protocol`,
`capabilities`, and `native_passthrough`. Their order has no product meaning.

`capabilities` is the canonical list of capability identifiers and human labels
for the service form. Do not fall back to a client-maintained copy. The add
form starts with no selected preset, handles loading/error/empty responses
explicitly, and submits only an API-supplied preset selected by the operator.

### POST /admin/api/operator/services  (add service)

Request:
```json
{
  "name": "Text generation",
  "preset": "custom-http",
  "base_url": "http://10.0.0.5:8000/api",
  "api_key_env": "TEXT_SERVICE_API_KEY",
  "capabilities": ["chat.completions"],
  "models": [
    { "public": "llama-3-8b", "upstream": "Meta-Llama-3-8B-Instruct" }
  ]
}
```

Rules: `name` required; `base_url` OR `host` required; `api_key` (inline
secret) is rejected — use `api_key_env` (env-var name) instead. `models` is
optional — a list of `{ "public": "…", "upstream": "…" }` mappings (also
accepts `model_group`/`upstream_model` keys); stored as
`config.models = [{ model_group, upstream_model }]`.

`201` → `{ "service_registry": { …same shape as GET… } }`

`409 service_conflict` if a service with this id (normalised from `name`)
already exists.

### PATCH /admin/api/operator/services/{service_id}  (edit service)

Request: same fields as POST (partial ok; `name` defaults to `service_id` when
omitted). `base_url`/`host` are optional for metadata-only edits. A record the
API marks non-editable may accept only its declared editable fields. Accepts a
`models` list with the same shape as POST.

Full field list accepted by PATCH (all optional in partial edits):

**Connection**: `name`, `base_url` (or `host`+`port`+`base_path`),
`api_key_env` (env var name only, never a key value), `protocol` (an opaque
service-record value), `health_endpoint`, `timeout` (integer seconds).

**Routing**: `capabilities` (list of strings; canonical values: `chat`,
`completions`, `vision`, `embeddings`, `image`, `audio`, `speech`,
`generations`, `videos`), `native_passthrough` (boolean), `models` (list of `{ public, upstream }`
mappings, same shape as POST).

**Admission / Advanced**: `role` (`"required"`, `"optional"`, `"standby"`),
`priority` (integer, lower = preferred, default 100), `note` (string),
`resources` (object: `gpu_required` bool, `max_parallel` int, `vram_gb_estimate`
number, `duration_class` `"interactive"|"batch"`).

**Top-level**: `enabled` (boolean).

**Additional service config** is opaque record data. The server validates and
round-trips it without asking the browser to inspect the operator filesystem or
derive a selector. A UI can render explicit values and server-supplied choice
sets, but cannot manufacture engine/protocol-specific controls.

**Honesty law H1 (preserve mechanism)**: `AdminSQLiteStore.update_service`
starts its merge base from ALL keys in the existing `config` dict (not a
hand-listed whitelist). It then overlays the caller-supplied form fields.
`service_form_to_raw` explicitly handles known fields and passes any remaining
unknown keys through verbatim (see `_FORM_CONSUMED_KEYS`). This means any
config key ever written to the DB — including future protocol fields and
extension-added keys — survives a partial PATCH by default, with zero per-field
maintenance.

Unknown config keys in the existing record are **never dropped** by a partial
PATCH.

`200` → `{ "service_registry": { … } }`

`403 service_not_editable` only when a request changes a field that the API
marks derived/non-editable.

### DELETE /admin/api/operator/services/{service_id}  (remove service)

`200` → `{ "service_registry": { … } }`; `403 service_not_editable` only for
a record the API marks non-removable; `404` after second delete.

Config-imported rows show a provenance-aware confirmation: "imported from your
config file; your YAML is not modified and will not re-import it".

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

Model detection is advisory only. A failed or incomplete probe must remain an
honest failed/incomplete state; the client must not replace it with browser
filesystem inspection or invent a provider-specific selector.

### GET /admin/api/operator/litellm-config

`200` → `{ "litellm_config": { "model_list": [ { "model_name": "…", "litellm_params": { "api_base": "…", "api_key": "os.environ/KEY_NAME" } } ], "general_settings": { "…": "…" } } }`

This is a derived runtime artifact. Secrets appear as `os.environ/…`
references, never as values. If exposed, render it as generic read-only
diagnostic data; it is not a named-engine configuration surface.

---

## Surface 4 — Apps & keys

### GET /admin/api/apps

Returns `200` → `{ "apps": [ …store app records… ] }`.

The access-plans migration removed `apps.default_profile_id` and
`apps.owner_service_account_id`. Do not use the old profile/fallback payload
or derive profile usage counts from it. The router's `access_model_summary`
states that each app has an accountable person and one assignment: an access
plan or the same plan as its person. It runs only what both plans allow.

UNVERIFIED: the complete app record and assignment-write schemas require the
store implementation; the router passes these records through.

### POST /admin/api/apps  (create app)

The router requires a non-empty string `name` and passes the JSON body to
`store.create_app`. `201` → `{ "app": { … } }`; `400 invalid_app`
for an invalid name or a store validation error. Do not send legacy
`profile_id` fields based on the retired profile contract.

### PATCH /admin/api/apps/{app_id}  (edit app)

The router validates `name` when supplied and passes the JSON body to
`store.update_app`. `200` → `{ "app": { … } }`;
`400 invalid_app`, `404 app_not_found`. Assignment-write fields are
UNVERIFIED here; `default_profile_id` is not a current database column.

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

The router passes the JSON body to `store.create_credential`; its complete
input schema is UNVERIFIED here. Keys carry no plan of their own
(`access_model_summary`); the migration removed `credentials.profile_id`.
Do not offer a per-key profile selector.

`201` → `{ "credential": { …credential fields above…, "api_key": "opk_<full key>" } }`

`api_key` is **reveal-once**: show in the reveal dialog, never log, never
refetchable. `404 app_not_found`, `400 invalid_credential`.

### POST /admin/api/admin-keys  (admin automation key)

Request: `{ "name": "required", "role": "admin" | "owner" }`. Minting `owner`
requires the caller to hold `owner` (else `403 admin_role_forbidden`). The key
works only while the person who minted it still holds a role that permits it.

`201` → `{ "credential": { …, "api_key": "oak_<full key>", "role": "admin", "service_account": "<name>" } }`

### DELETE /admin/api/credentials/{credential_id}  (revoke)

`200` → `{ "credential": { …, "revoked_at": 1765… } }`; `404
credential_not_found`. (`POST /admin/api/credentials/{id}/revoke` is an
equivalent legacy alias; the React client uses the DELETE form.) Revocation is
idempotent: revoking an already-revoked key returns `200`.

---

## Surface 5 — Access plans

The schema-7 migration in `gateway/access_plan_migration.py` drops the
`profiles` and `runtime_profile_mappings` tables and deletes the
`user_access_defaults` operator setting. There is no profile compatibility
layer. The following routes are registered in `gateway/admin_api.py`.

### GET /admin/api/access-plans

`200` → `{ "access_plans": [ …store plan records… ] }`.

### GET /admin/api/access-plans/editor

Returns the contract from `admin_catalog.access_plan_editor_contract`.
A failed projection returns `503 access_plan_editor_unavailable`, with
message `Access plan choices could not be read.`. The full editor and plan
record schemas are UNVERIFIED from this router alone; do not reuse the
retired policy-editor shape or its mapping-source descriptors.

### POST /admin/api/access-plans

Passes the JSON body to `store.create_access_plan`.
`201` → `{ "access_plan": { …store plan record… } }`;
`400 invalid_access_plan`, `409 access_plan_name_conflict`.

### GET /admin/api/access-plans/{plan_id}

`200` → `{ "access_plan": { …store plan record… } }`;
`404 access_plan_not_found`.

### PUT /admin/api/access-plans/{plan_id}

Passes the JSON body to `store.update_access_plan`.
`200` → `{ "access_plan": { …store plan record… } }`;
`400 invalid_access_plan`, `404 access_plan_not_found`,
`409 access_plan_name_conflict`. The method is PUT, not the retired profile
PATCH route. Complete write-body semantics are UNVERIFIED here.

### DELETE /admin/api/access-plans/{plan_id}

`200` → `{ "access_plan": { …store plan record… } }`;
`404 access_plan_not_found`, `409 access_plan_in_use`.

### POST /admin/api/access/check

Request: `{ "app": "<app ID>", "request": { … } }` evaluates the app's
two plans as the runtime does; `{ "plan": "<plan ID>", "request": { … } }`
evaluates that plan on both sides.
`200` → `{ "plan_ids": [ … ], "decision": { … } }`.

### POST /admin/api/access/preview

Request: `{ "plan": "<plan ID>" }` or `{ "app": "<app ID>" }`.
Returns `plan_ids` plus the injected preview's `summary` and `entries`.
A missing or failed preview returns `503 access_preview_unavailable`.

The router no longer registers `/admin/api/profiles` (including item routes),
`/admin/api/policy/editor`, or `/admin/api/policy/simulate`. The access-plan
routes above are not aliases accepting the old profile payloads.

---

## Surface 6 — Users

### GET /admin/api/users

```json
{ "users": [ {
  "id": "usr_…",
  "issuer": "https://accounts.google.com",
  "subject": "oidc-sub-string",
  "email": "tim@example.com",
  "display_name": "Owner" | null,
  "name": "Owner" | null,
  "created_at": 1765…,
  "last_login_at": 1765… | null,
  "roles": ["owner"]
} ] }
```

`name` mirrors `display_name`. This endpoint contains **people only**: owner
cell text is `display_name ?? email`, with `usr_…` as the quiet copyable detail.
No user CRUD — identities come from the sign-in provider; say so in the
empty/explainer copy. It must never contain an admin key, CLI identity, or
service principal.

**Apps count** for the Users table is client-side: count `GET /admin/api/apps`
rows where `owner_user_id === user.id`.

### Removed user-access defaults and identity mappings

`GET /admin/api/operator/overrides` still exists, but the migration deletes
the `user_access_defaults` setting. Do not render a defaults form from the
retired payload. The router does not register
`PUT /admin/api/operator/user-access-defaults` or the former
GET/POST/DELETE `/admin/api/runtime-profile-mappings` routes.

OIDC group-to-access-plan auto-assignment is **not built** on this router
surface. Configuring a groups claim does not establish an access-plan mapping.
UNVERIFIED: persistence in `user_identity_claims` and whether any other
module reads those claims require source outside this review's two files.

---

## Surface 7 — Audit

### GET /admin/api/audit

Reverse-chronological list of audit events with server-side filtering and
keyset cursor pagination. The 200-row truncation ceiling is **gone**: paginate
with `next_cursor` to reach arbitrarily old events for any filter.

**Query parameters** (all optional):

| Param | Type | Description |
|---|---|---|
| `limit` | int | Page size. Default 50, max 200 per page. |
| `actor_id` | string | Exact match on `actor_id` (e.g. `usr_…`, `svc_…`, `"cli"`). |
| `action_prefix` | string | Prefix match on `action` (e.g. `"service."`, `"app."`). |
| `before` | string | Keyset cursor from a prior `next_cursor`; encoded as `"<created_at>:<id>"`. Fetches the next page older than that position. |

**Response**:

```json
{
  "records": [ {
    "id": "aud_…",
    "created_at": 1765…,
    "actor_kind": "admin" | "user" | "service_account" | "system" | "cli" | null,
    "actor_label": "Owner" | "release-bot" | "System" | "Unknown principal",
    "actor_id": "usr_… | svc_… | cli",
    "action": "app.create",
    "target_kind": "app" | "profile" | "credential" | "operator_setting" | "service" | "extension_package" | "runtime_profile_mapping" | null,
    "target_id": "app_… | prof_… | …",
    "metadata": { "name": "brother-laptop", "kind": "service" } | null
  } ],
  "next_cursor": "<created_at>:<id>" | null
}
```

Field notes:
- Legacy `profile` / `runtime_profile_mapping` targets and profile,
  mapping, or user-default actions below describe historical audit records,
  not current writable resources or routes.
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
- `actor_kind` and `actor_label` are authoritative server facts. The gateway
  obtains them from the authenticated principal record when it writes an
  event, then resolves the display label through that recorded kind. Clients
  must not infer a kind or label from `actor_id` prefixes. `"system"` and
  `"cli"` render as `"System"`.
- `metadata` is a bounded safe-detail projection, not the stored free-form
  event payload. The API permits only its documented non-secret field
  vocabulary, limits keys/list items/string length, and drops unknown nested
  objects at this trust boundary. Clients may copy or render this response,
  but must not fetch or reconstruct raw event metadata.
- Results are sorted newest-first (`created_at DESC`).
- `next_cursor` is `null` exactly when no more rows exist **for the current
  filter set** — the UI may display "End of audit log" (or "No more matching
  events" under an active filter). A non-null cursor is opaque; pass it
  verbatim as the `before` param on the next fetch.
- Filter chips in the UI drive the **server** query, not a client-side slice.
  `actor_id` + `action_prefix` work across the entire log, not just the first
  200 rows.

`GET /admin/api/audit-events` is a canonical noun-based alias for the same
endpoint; the React client should prefer `/audit`.

---

## Surface 8 — Settings

The identity provider connection is read and written only through the Sign-in
routes below (`GET` and `PUT /admin/api/signin`). It has no route of its own.
`client_secret` is write-only: the snapshot says only whether one is saved
(`client_secret_saved`), and a save that leaves the secret blank keeps the
stored one. A save that changes the connection while the provider is on must
carry a passed test sign-in.

### Sign-in rules and the Sign-in page (`/admin/api/signin*`)

Who may sign in, and what they get, is a set of rules (`gateway/signin_rules.py`).
The Settings > Sign-in page reads and writes them through these routes; an
administrator may `GET /admin/api/signin`, and every other route is owner-only
(`403 owner_required`). Types: `web/src/lib/signin-api.ts`.

| Route | What it does |
|---|---|
| `GET /admin/api/signin` | One snapshot: the two sign-in switches, the provider connection (no secret), the rules, values past sign-ins carried, plans, and a revision |
| `POST /admin/api/signin/preview` | `{proposal, test_id}` → who changes (by name), settings, blockers, who each rule matches, and the test verdict. Same evaluator as sign-in |
| `PUT /admin/api/signin` | Save the proposal with `If-Match`; `keep_admin_keys` and `save_for_later`. `409` for `last_sign_in_method`, `test_required`, `incomplete_rule`, `owner_needs_password` |
| `POST /admin/api/signin/discover` | `{address}` → the endpoints found at the provider address |
| `POST /admin/api/signin/test` | Start a test sign-in for the connection as typed; `GET …/test/{id}/start` opens it in a pop-up; `GET …/test/{id}` polls; `DELETE …/test/{id}` cancels |
| `POST /admin/api/signin/link-owner` | Link the provider identity from a passed test to the owner account |
| `POST /admin/api/signin/notice/dismiss` | Dismiss the one-time note from the upgrade |
| `POST /admin/api/signin/owner-password` | Set the owner's own password |

`PUT /admin/api/signin` is one SQLite transaction: the rules, the switches, the connection (and its secret),
and every grant removed, session ended and key revoked for the people they touch commit together or not at
all. What a rollback cannot undo (a person's MCP connections) runs after the commit. A save that moves the
provider to another host, or to other endpoints, must carry the client secret typed again
(`409 connection_incomplete`; the preview names it as the `secret_needed` blocker), so a saved secret goes only
to the host it was entered for. A save that changes the issuer of a live provider ends the sessions of
everyone linked to the old issuer (not the owner) and pauses their personal keys until they sign in again;
`issuer_change.signed_out` in the preview counts them.

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

---

## Member surfaces (`/me/api`)

The member Home and Status pages read the signed-in person's own view. The
fields below are additive; the payloads keep every earlier field.

### Availability evidence (`availability.evidence`)

Every capability `availability` object carries `evidence`, an allowlisted
summary of the fact behind the state. All times are epoch seconds. A time is
absent when it is not known; the gateway never substitutes the current time.

| Field | Meaning |
|---|---|
| `source` | Where the fact came from: `health` (health probe), `status` (status grid), `lifecycle`, `request` (a recent request receipt), `registry`, `capability`. |
| `kind` | Optional qualifier, for example `no_response` (the check got no usable answer), `disabled`, `unconfigured`. |
| `receipt_status` | For `source: "request"`, the terminal status of that request. |
| `observed_at_s` | When the check or request behind this evidence happened. A failed check is observed at its attempt time, so read it together with `kind` and `last_success_at_s`. Absent when nothing was checked (for example, a service without a health endpoint). |
| `last_attempt_at_s` | The latest check or proven request from this source. Equal to `observed_at_s` today; kept separate so that clients do not have to infer it. |
| `last_success_at_s` | The latest successful check or request from this source that the gateway knows of. Health checks remember it for the life of the gateway process; request evidence looks back over the recent-evidence window (15 minutes). Absent means no success is known, not that none happened. |

A placeholder for an unmeasured service (`status: "unknown"`, no time) never
hides a timed observation from another source.

### GET /me/api/status — capability evidence

Each `capabilities[]` row carries `availability: { "evidence": { … } }`, the
same allowlisted evidence object as the matching `/me/api/home` capability
(fields above). The older top-level `observed_at_s` stays for compatibility.

### Busy versus failing

`status: "busy"` means a status or health check reported the service busy or
degraded and no recent request proves it is failing. When a request to the
service failed with a server error (HTTP 5xx, a timeout, or a disconnect) in
the recent-evidence window (15 minutes) and no later request succeeded, the
capability projects `status: "unavailable"`, `available: false`, with
`evidence.source: "request"`, even if a newer check says busy or degraded.
This projection is for presentation only; routing and fallback do not read it.

### Gateway owner (`gateway` on /me/api/home and /me/api/status)

```json
{ "gateway": { "owner_name": "Sam" | null } }
```

`owner_name` is the display name of the earliest active owner (role `owner`,
not revoked) that has one. It is `null` when no owner has a display name; the
client supplies its own wording ("the gateway owner"). No `owner_contact` is
published: the store has no contact field, and the owner's sign-in email is an
identity, not a contact the owner chose to share.

### Receipts — `refusal` (client, admin and internal audiences)

A receipt for a request that a spending limit refused carries:

```json
{ "refusal": { "code": "budget_exceeded",
               "limit": { "period": "daily" | "monthly" | "request",
                          "scope": "person" | "app" | "provider" | "global" } | null } }
```

- `code` is the error code the caller received: `budget_exceeded`, or
  `provider_needs_reconciliation` when unsettled charges block paid requests.
- `limit` names the limit that refused the request. `plan` is the caller's
  access plan (profile) cap, `provider` a cap on the provider (registry or
  owner setting), `global` the owner's gateway-wide cap. `request` is the
  plan's per-request cap. `limit` is `null` when no single limit applies (for
  example, the price is unknown while a cap is set).
- The field holds codes only: no amounts, provider names or other people's
  usage. It is absent on receipts that were not refused by a spending limit.
  Clients must use it instead of matching warning or error text.
- Written today by paid chat/embeddings execution and the paid OpenAI-style
  `/v1/decisions` path. The TypeSafe `/v1/decisions` receipts (a separate
  receipt type) carry `error_code: "budget_exceeded"` but no `limit`.

### Plan spend (member response; admin plan fields UNVERIFIED)

The former admin profiles endpoint is removed. The current access-plan list
passes store records through; the presence and shape of `plan_spend` on
those records are UNVERIFIED from `gateway/admin_api.py` alone. The member
description below was not revalidated in this scoped admin-route review.

```json
{ "plan_spend": [
  { "period": "daily", "window": "rolling_24h",
    "spent_usd": "0.300000000" | null, "cap_usd": "1",
    "resets_at": 1765… | null },
  { "period": "monthly", "window": "calendar_month_utc",
    "spent_usd": "0.300000000" | null, "cap_usd": "8",
    "resets_at": 1767… }
] }
```

- One entry per period for which the plan has a cap
  (`remote.budget_usd_per_day`, `remote.budget_usd_per_month`). A plan without
  caps has `[]`. `plan_spend` is `null` when the plan itself cannot be loaded.
- The numbers are the cap barrier's own accounting (the provider spend
  ledger): the daily window is the rolling last 24 hours, the monthly window
  is the UTC calendar month, and reserved (in-flight), settled and unreconciled
  charges all count. USD values are decimal strings.
- `spent_usd` is `null`, with `unknown_reason`, when the spend is not known:
  `ledger_unavailable` (no ledger wired or it cannot be read) or
  `needs_reconciliation` (the plan has a charge of unknown cost; paid requests
  are refused until an owner settles it). It is never reported as `0` in
  these cases. A ledger file that does not exist yet means no paid request has
  reserved spend, which the barrier also counts as `0`.
- Like the barrier, a `reserved` charge that has expired, has no owning
  process generation, or belongs to another process generation counts as an
  unreconciled charge, so the spend is unknown (`needs_reconciliation`).
- `resets_at` (epoch seconds): for `monthly`, the start of the next UTC month.
  For `daily`, the rolling window has no single reset; this is when the oldest
  counted charge leaves the window and the spend first drops. Always `null`
  when the spend is unknown; for `daily` also `null` when no charge is counted.
- On `/me/api/home` this is the member's own plan (the plan their keys
  inherit). The spend is the plan's total across everyone on it, because the
  caps are shared; it names no person and no request.
- Provider and global caps (owner settings) also refuse requests; they are not
  part of `plan_spend`. A refused receipt names the limit (see `refusal`).

### Accepted limits of these member facts

- **Shared plan totals.** `plan_spend` on `/me/api/home` shows the plan's
  total spend and the daily `resets_at` (the time of the plan's oldest charge
  in the last 24 hours, plus 24 hours). On a plan with very few members this
  hints at when and how much others on the same plan spent. This is accepted:
  the caps are shared, and a member cannot understand a refusal without the
  shared total. No person, request or provider is named.
- **`/v1/decisions` (TypeSafe) refusals.** Those receipts are a separate type
  and carry `error_code: "budget_exceeded"` without `refusal.limit`.
- **Health endpoints that answer 404/405** project `status: "unknown"` with
  `evidence.kind: "no_response"` (unchanged earlier behavior): the gateway
  cannot tell a missing health route from a down service.
- **Same-second evidence.** When two sources observed a service in the same
  second, a check that saw it ready wins over a busy/degraded row; otherwise
  the source merged last wins.
