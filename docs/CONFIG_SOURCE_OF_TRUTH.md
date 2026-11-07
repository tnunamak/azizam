# Config Source of Truth

Date: 2026-05-21. Revised 2026-11-07 for config homes C10.

Each setting has one durable home. Settings the owner may change live in the admin database (`admin.db`), as sparse
overrides of release defaults that ship read-only in `registry/defaults/`. What the gateway needs before it can open
that database, or cannot change while it runs, lives in a small startup file whose keys are a closed list
(`registry/startup.schema.json`). The design is `docs/design/CONFIG_HOMES.md`; this note states the policy that
follows from it.

The inventory of every file-borne setting, `gateway/config_sources.py`, is generated from the section schemas and the
startup schema. It holds no hand-kept table. A new key in either schema is a new row, and
`tests/test_config_sources.py` fails if a row is missing.

For the operator-facing first-run story this model implies, see
[`FIRST_RUN_SETUP.md`](FIRST_RUN_SETUP.md). In short:

- **The startup file (`proxy_config.yml`), `.env` and Docker secrets** own what the gateway needs to start safely: the
  listener, storage roots, login boot rules and secrets. Start-up refuses a key that is not in
  `registry/startup.schema.json`, and its message names where the setting lives now.
- **`admin.db` and the dashboard** own runtime product state: owner/admin roles, Apps, Access plans, App API keys,
  every config section (model aliases, roles, models, `llm`, images, videos, audio, voices, model compatibility,
  workflow execution, admission caps, instance presentation, service availability), OIDC provider settings, and the
  service/provider registry.
- **A read-only service-registry export is shipped.** Authenticated
  `GET /admin/api/config/export` serializes the current registry's service
  records and omits ephemeral runtime state. It is an operator download, not an
  authoritative configuration file or a restore command. It is owner-only: it
  keeps each start command and environment value whole so that it round-trips,
  while every view an administrator reads hides those values.
- **Instance presentation is dashboard-owned.** The owner settings API stores
  the instance name, public base URL and default theme in the existing
  `admin.db` operator settings table. The release default is neutral until the first save; no environment variable
  or startup key sets the public URL. Saving does not change listener or
  TLS configuration. The former universal `all_users` runtime mapping is not
  a current access-default mechanism: schema 7 drops the runtime mapping table.
  Model aliases are a config section: the release ships the defaults and the owner's overrides live in `admin.db`.
- **The effective view is per section.** `GET /admin/api/config/sections/<section>` shows each value and the source
  that won (release or owner override). The whole-configuration snapshot routes
  (`GET /admin/api/settings/effective` and its `/export`) are removed: a snapshot of settings that live in several homes
  hid which home to edit. The owner's export is the config API's export, which round-trips.
- **Trusted addresses belong to Apps.** An owner adds them under
  **Apps & keys → app menu → Trusted addresses**. A matching request without
  a key uses that App and its access plan. Otherwise, a missing key returns
  `401 key_required`. A sent key always wins. Placeholder keys such as
  `loopback` return `401 key_invalid`; there is no placeholder-key list or
  global LAN bypass. `X-Forwarded-For` counts only from proxies configured in
  `auth.trusted_proxies`, which stays in the startup file.
- **OIDC uses the configured public origin.** When Settings has a public base
  URL, browser sign-in replaces the OIDC `redirect_uri` origin with that URL
  and keeps the callback path. For example, a login started on
  `api.example.com` with `https://ai.example.com` saved in Settings sends the
  identity provider `https://ai.example.com/admin/oidc/callback`. Without a
  public base URL, the configured OIDC `redirect_uri` stays unchanged; the
  incoming host does not choose it. Register the resulting exact callback URL
  with the identity provider. A pending login keeps its original callback URL
  for the token exchange even if Settings changes before it finishes.
  Sign-in first redirects to the callback host when started through an alias,
  then sets a separate host-only cookie for that pending login. The callback
  requires that browser cookie before exchanging the authorization code.
  Cookies for parallel sign-in tabs remain independent and expire after ten
  minutes. TLS proxies do not need to rewrite the ASGI request scheme.
  The session cookie is issued on the callback host, and its configured domain
  still applies. With the default host-only cookie, the person remains signed
  in on that callback host. Relative return paths bring them to the requested
  page there, including its query string. Local accounts use the requested
  relative page on the host where sign-in started. Invalid return paths fall
  back to `/`.
- **Full provisioning / as-code reconciliation is deferred.** Access plans live
  in `admin.db`. The old user-access defaults are not a current seeded setting:
  schema 7 deletes `operator_settings.user_access_defaults` and its generation
  record. No full bundle restore or file-wins reconciliation is specified here.

> **Access-plan migration boundary.** `gateway/access_plan_migration.py`
> removes `profiles`, `runtime_profile_mappings`, and `user_access_defaults`.
> `gateway/admin_api.py` exposes access-plan CRUD; it does not register the
> legacy profile, runtime-mapping, or user-default write routes. Do not treat
> `admin.user_defaults`, `AZIZAM_USER_SELF_SERVICE`, or
> `AZIZAM_AUTO_CREATE_PERSONAL_APP` as a supported way to recreate them.
> UNVERIFIED: current bootstrap handling of those legacy inputs outside these
> two reviewed modules.

## Ownership planes

There are four planes. Every setting in the generated inventory belongs to exactly one.
Concretely:

1. **Bootstrap plane** — the startup file (`proxy_config.yml`), the environment
   variables of its keys, and referenced secret stores. Everything the gateway needs to *start*: the
   listener, optional explicit session signing keys, trust roots, the sign-in
   provider of a first boot, secret references, and storage roots (model, image, voice and
   audio directories). Never dashboard-writable.
   Restart to apply, with `scripts/safe-restart`. Provisioning bundles never
   carry secrets.
2. **Dashboard plane** — `admin.db`, edited through the web app (and the
   `/admin/api/*` JSON surface). Runtime access policy: the admin user list,
   role grants, Access plans, Apps, App API keys, role→model bindings, admission caps, throttles,
   runtime routing overrides, OIDC provider settings, and service/provider
   records — anything an operator should legitimately be able to change after
   boot without touching a file. Audited, applies live unless the apply_mode
   says otherwise.
3. **Provisioned plane** — workflow manifests and catalog files committed in
   the repo (`workflows/`, the model compatibility table, `registry/`). The
   bundled `registry/` records are the release layer of the service registry:
   `admin.db` holds only the owner's data (records the owner made, field-level
   patches of release records, tombstones), and a release change to a field the
   owner did not touch reaches every install. `proxy_config.yml` `backends` is
   not read, and start-up refuses it.
4. **Secret/bootstrap plane** — env, Docker secrets, and secret references for
   bootstrap secrets. Remote provider keys have a deliberate product exception:
   owners can submit a write-only key through the Providers UI. Its ciphertext
   lives in `admin.db`, with a separate 0600 encryption key file in the data
   directory. The dashboard shows status, last four characters, and update time,
   never the stored value. Environment references remain supported.

Service/provider records carry a `source` value as provenance/history, not a
permission boundary. Every non-managed service row can be edited, enabled or
disabled, and removed from the DB/UI registry; a removed release row is a
tombstone, so later boots do not bring it back:

- `ui`, `provisioned`, `builtin`, and `discovered` identify how the row entered
  the registry; they do not change its DB/UI permissions.
- The managed LiteLLM substrate is the sole exception. Its endpoint and
  protocol fields are gateway-derived and locked, and it cannot be removed;
  its name, enabled state, and note remain editable.

## Per-domain ownership policy

The generated inventory (`gateway.config_sources.inventory()`) is the single list of file-borne settings, one row per
section entry and one per startup key, each with its home and how to change it. The table below is the summary.

| Domain               | Home                         | Change path                                                            |
|----------------------|------------------------------|------------------------------------------------------------------------|
| listener, storage roots, login boot rules, secrets | startup file (`registry/startup.schema.json`), env for some keys and for every secret | edit the file or set the variable; restart |
| `services/providers` | `admin.db` service registry  | Services in the admin UI, or `POST /admin/api/config/import`           |
| model aliases, roles, models, `llm`, `reasoning`, `model_routing` (owner parts) | `admin.db` config sections | `PATCH /admin/api/config/changes` |
| images, videos, audio, voices, `model_compatibility` (owner parts) | `admin.db` config sections | `PATCH /admin/api/config/changes` |
| `resource_admission`, `service_availability`, `instance` | `admin.db` config sections | their settings routes, or the config API |
| `workflow_execution`, `models` | `admin.db` config sections | `PATCH /admin/api/config/changes`                                      |
| workflow manifests   | repo (provisioned)           | the manifest files themselves                                          |
| `auth/admin` runtime | `admin.db`                   | instance presentation, OIDC provider settings, trusted addresses, admin list, roles, Access plans, Apps, App keys |

Implications:

- The dashboard never writes a value that lives in the startup file or env. If an
  operator wants such a value to change, the change path is "edit the file
  and restart with `scripts/safe-restart`" — surfaced in `change_path` and `apply`.
- The dashboard writes all non-managed service/provider records regardless of
  `source`; source remains provenance. The managed
  LiteLLM substrate has the limited fields described above.
- The dashboard never writes browser session signing keys. Remote provider
  secrets are an intentional write-only exception, encrypted in `admin.db`;
  provider catalog YAML, registry exports, and generated LiteLLM YAML carry
  metadata or references only. Provider catalog files are extensible provisioning
  data; a saved provider record is dashboard-owned runtime state. OIDC
  `client_secret` is another intentional exception:
  when configured from the web app, it is encrypted in the same `admin.db` secret
  store; `operator_settings.oidc_provider_config` keeps only its
  `client_secret_ref`. It is write-only (UI/API responses show only
  `client_secret_set`), preserved on blank updates, and audited. Startup moves
  a plaintext `client_secret` left in that setting into the store. Operators
  who require an external secret store should use the `client_secret_env`
  bootstrap fallback.
- Runtime Apps, Access plans, and keys live in `admin.db`. Bootstrap config stays
  in YAML/env, but runtime authorization decisions do not read a key file.
- Service/provider availability is read from the registry. `proxy_config.yml`
  does not seed or provision registry records (its `backends` block is not
  read); the registry is the live operator surface.

## Provisioning bundles, restore, and reconciliation (deferred)

The gateway ships a narrow, read-only service-registry export at
`GET /admin/api/config/export`. It serializes service records for operator
download (owner only, values whole) and excludes ephemeral runtime state. It does not restore a gateway,
make the downloaded file authoritative, reconcile it with the dashboard, or
cover all dashboard-owned settings.

Full provisioning / as-code reconciliation remains deliberately out of scope
for the current MVP. The inventory still classifies rows with
`provisioning == "bundle_candidate"` so a future bundle serializer has an
obvious target, but no shipped restore command consumes that classification and
the normal first-run path does not assume a bundle exists.

When full provisioning lands, the smallest useful "export this gateway" artifact
will be the subset of `admin.db` rows whose inventory entry has
`provisioning == "bundle_candidate"`. That will give an operator a way to:

- copy a gateway to a new machine without copying secrets,
- diff "what changed via the dashboard" against the committed YAML, and
- treat `admin.db` as authoritative for everything the dashboard can edit.

Rows tagged `bootstrap_only`, `secret_bootstrap`, or `source_manifest` will
*not* be part of the bundle. Provisioned service rows can be exported as
references to their provisioning source; UI-managed service rows export as
runtime product state without secret values.

Until that exists, treat `admin.db` itself as the artifact — back it up,
mount its directory on a durable volume in container deployments, and rely on
the dashboard as the only edit surface for runtime policy.

## Things to deliberately *not* build yet

- A database-backed config plane that absorbs the startup file. The
  bootstrap plane exists for a reason (early-startup, secrets, deployment
  shape); collapsing it into `admin.db` would add a chicken-and-egg loader
  problem with no operator benefit. C10 shrank it to the keys that need it.
- A second policy editor inside Services or APIs. Policy stays on Access plans
  (see `docs/ADMIN_ACCESS_UX_RESEARCH.md`).
- A second service-registry ownership mode. The dashboard makes provenance
  legible, but the DB/UI registry (the release layer under the owner's data)
  remains authoritative.
- Per-field schema validation in the dashboard for bootstrap fields. They
  are validated at gateway start; making the dashboard a second validator
  duplicates truth.

## How this stays honest

`tests/test_config_sources.py` generates the inventory and checks that it covers every entry the section schemas
declare and every key the startup schema lists, with unique, sorted ids, the environment variable of each startup key,
and no values. `tests/test_startup_config.py` checks the startup schema, its moved-key map, and that the tracked
`proxy_config.yml` is a valid first-run file. Nothing in a table here can drift from the code without one of them
failing.
