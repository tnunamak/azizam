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
  `prof_…`, `aud_…`, `map_…`. Render via `<EntityRef>` only.
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

`/admin/status` and `/admin/status/` require admin browser authentication.
Both redirect to `/ui/admin/status/` while preserving the full query string
(for example, `/admin/status/?service=svc_1` redirects to
`/ui/admin/status/?service=svc_1`); the same query-preserving trailing-slash
canonicalization applies to every admin section route.

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
    "service_principals": 1,
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
      "target_kind": "app",
      "target_id": "app_…",
      "target_name": "brother-laptop",
      "href": "/admin/apps?app=app_…"
    }
  ],
  "current_health": { "…": "exact GET /admin/api/status.current_health shape" },
  "runtime_profile_mappings": [ { "...": "..." } ],
  "access_model": { "nouns": ["user", "app", "profile", "key", "runtime_mapping", "admin_role"], "..." : "..." }
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
- **Owner name is a client-side join**: apps with `owner_user_id` resolve
  against `GET /admin/api/users` (`id → display_name ?? email`). Apps with
  `owner_service_account_id` are automation. They never resolve through Users
  or render as a person; use the service-principal label/name available in the
  Apps & keys credential context.
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

## Surface 5 — Profiles

### GET /admin/api/policy/editor

Admin-only discovery contract for the Profiles and Users editors. It is
intentionally separate from Overview and does not carry a registry revision or
become a write precondition. `200` returns schema version `1`, a create policy
document serialized from the runtime default, `omitted: "no_opinion"` / `empty:
"deny_all"` set semantics, opaque policy choices, mapping source descriptors,
and the helper-defined fallback profile descriptor. Dynamic choices are
projected from normalized registry data; catalog data supplies only labels and
descriptions. A saved identifier absent from the response is not invalid: the
client renders it as **Not currently advertised** and retains it unless the
operator removes it.

If registry or presentation projection fails, the endpoint returns `503`:

```json
{"error":{"code":"policy_editor_unavailable","message":"Policy editor choices could not be read."}}
```

Clients with no previous successful response present Unavailable and disable
discovery controls. Clients retaining a previous response mark it Stale, do
not add stale choices, and may still retain/remove saved values or edit
name/description. Profile and mapping writes remain independent of the
registry and accept opaque saved policy identifiers.

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
- A fallback profile may be supplied separately by the API. The client must
  not prepend or infer a static default selection.
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
    "service_availability": { "text-service": { "state": "degraded", "reason": "slow", "retry_after_seconds": null } },
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

### GET /admin/api/oidc-config

Returns the **effective** runtime configuration: bootstrap OIDC configuration
merged with non-blank database overrides. **Never returns raw secrets.**
`client_secret` is replaced with `"<redacted>"` and the companion boolean
`client_secret_set` indicates whether the effective configuration has a
secret. Returns `null` only when neither source configures OIDC.

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
    "groups_claim": "groups",
    "updated_at": 1765…,
    "source": "bootstrap" | "database" | "merged" | "runtime_default",
    "provenance": {
      "summary": "bootstrap" | "database" | "merged" | "runtime_default",
      "fields": { "issuer": "bootstrap", "client_id": "database" }
    },
    "apply_mode": "live"
  } | null
}
```

`groups_claim` is the name of the ID-token claim that carries the user's group
memberships (default `"groups"`). Change this when your IdP uses a different
claim name — e.g. `"cognito:groups"` (AWS Cognito), `"roles"`, or a custom
namespaced claim. The gateway reads this claim for two purposes:
1. **Admin grant** — if the claim value intersects `admin_groups`, the user is
   granted the `admin` role.
2. **Profile mapping** — the claim values are stored as `oidc_group` access
   names (see Users → runtime-profile-mappings) so operators can map groups
   to gateway access profiles.

`groups_claim` is returned plainly by GET (it is not a secret). `client_secret`
is **always** redacted in GET responses; it is write-only.

> **Runtime note**: the `groups_claim` field is fully wired — the live
> OIDC auth path in `proxy.py` reads the configured claim name for both admin
> role evaluation and profile mapping. Setting this field is immediately
> effective for the next login/callback without a gateway restart. `apply_mode`
> is `"live"` because the current runtime reads the effective setting on those
> paths; session signing key and cookie settings are not editable through this
> endpoint.

### PUT /admin/api/oidc-config  (save OIDC config)

Request: any subset of the `oidc_config` fields above. When `client_secret` is
omitted, blank, or `"<redacted>"`, the existing stored secret is preserved
(patch semantics). Omitting `groups_claim` preserves the existing value
(patch semantics). Unknown fields return `400 invalid_oidc_config`.

Accepted fields: `enabled`, `issuer`, `client_id`, `client_secret`,
`redirect_uri`, `scopes`, `allowed_email_domains`, `admin_emails`,
`admin_groups`, `admin_roles`, `authorization_endpoint`, `token_endpoint`,
`jwks_uri`, `clock_skew_seconds`, `groups_claim`.

For non-secret string fields, `null` or `""` explicitly clears a bootstrap
value; for list fields, `[]` explicitly clears it. Omit a field to preserve
the database override. `client_secret` remains write-only: blank, null, and
`"<redacted>"` preserve its stored value.

`200` → `{ "oidc_config": { …same scrubbed shape… } }`

The existing write-only secret behavior is preserved. An empty, omitted, or
`"<redacted>"` `client_secret` preserves the stored database secret. Dashboard
provider changes apply live for subsequent OIDC authentication requests. To
recover access, use an existing authenticated admin session, or generate an
admin key on the host and claim it at `/setup/claim`; do not promise a hidden
local-owner sign-in path while OIDC is enabled.

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
                          "scope": "plan" | "provider" | "global" } | null } }
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
