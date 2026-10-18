# Config Source of Truth

Date: 2026-05-21

Self-hosted operators currently see *many* config sources surfaced through the
admin "Sources of Truth" panel — proxy YAML, env, workflow manifests, the admin
database, and the service registry. The
`gateway/config_sources.py` inventory already classifies every domain with
explicit lifecycle facts (`authored_via`, `stored_in`, `writable`,
`provisioning`, `apply_mode`). This note pins down the *policy* behind those
facts so future work has a target, and so no setting silently drifts between
file and dashboard ownership.

The principle is Rich-Hickey-shaped: each setting has exactly one durable
home. For services/providers, the product source of truth is the DB/UI service
registry, not live reads from `proxy_config.yml`.

For the operator-facing first-run story this model implies, see
[`FIRST_RUN_SETUP.md`](FIRST_RUN_SETUP.md). In short:

- **`.env` / Docker secrets / `proxy_config.yml`** own bootstrap, seed, and
  provisioning input only — the things the gateway needs to start safely or
  import initial service records.
- **`admin.db` and the dashboard** own runtime product state — owner/admin
  roles, Apps, Access plans, App API keys, role/model
  bindings, admission caps, runtime routing toggles, OIDC provider settings,
  and the service/provider registry.
- **A read-only service-registry export is shipped.** Authenticated
  `GET /admin/api/config/export` serializes the current registry's service
  records and omits ephemeral runtime state. It is an operator download, not an
  authoritative configuration file or a restore command.
- **Instance presentation is dashboard-owned.** The owner settings API stores
  the instance name, public base URL and default theme in the existing
  `admin.db` operator settings table. Bootstrap branding and public URL values
  provide defaults until the first save. Saving does not change listener or
  TLS configuration. The former universal `all_users` runtime mapping is not
  a current access-default mechanism: schema 7 drops the runtime mapping table.
  Model aliases remain read-only bootstrap data.
- **Advanced export is a redacted snapshot.** Owner-only
  `GET /admin/api/settings/effective/export` exports the current configuration,
  resolved OIDC provider, registry and runtime overrides. It is for inspection,
  does not restore a gateway, and cannot write bootstrap configuration.
- **Trusted addresses belong to Apps.** An owner adds them under
  **Apps & keys → app menu → Trusted addresses**. A matching request without
  a key uses that App and its access plan. Otherwise, a missing key returns
  `401 key_required`. A sent key always wins. Placeholder keys such as
  `loopback` return `401 key_invalid`; there is no placeholder-key list or
  global LAN bypass. `X-Forwarded-For` counts only from proxies configured in
  `auth.trusted_proxies`, which remains bootstrap-owned.
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

There are four planes. Every domain in `CONFIG_SPECS` belongs to exactly one.
Concretely:

1. **Bootstrap plane** — `proxy_config.yml`, service environment variables,
   and referenced secret stores. Everything the gateway needs to *start*: the
   listener, TLS, optional explicit session signing keys, trust roots,
   secret references, and on-disk model directories. Never dashboard-writable.
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
   the repo (`workflows/`, the model compatibility table). Service records
   may be seeded from provisioning config, but after that one-time seed the
   DB/UI registry is authoritative.
4. **Secret/bootstrap plane** — env, Docker secrets, and secret references for
   bootstrap secrets. Remote provider keys have a deliberate product exception:
   owners can submit a write-only key through the Providers UI. Its ciphertext
   lives in `admin.db`, with a separate 0600 encryption key file in the data
   directory. The dashboard shows status, last four characters, and update time,
   never the stored value. Environment references remain supported.

Service/provider records carry a `source` value as provenance/history, not a
permission boundary. After the one-time seed, every non-managed service row
can be edited, enabled or disabled, and removed from the DB/UI registry;
removed rows are not re-imported on later boots:

- `ui`, `provisioned`, `builtin`, and `discovered` identify how the row entered
  the registry; they do not change its DB/UI permissions.
- The managed LiteLLM substrate is the sole exception. Its endpoint and
  protocol fields are gateway-derived and locked, and it cannot be removed;
  its name, enabled state, and note remain editable.

## Per-domain ownership policy

This table is the single source of truth that the inventory in
`gateway/config_sources.py` must agree with. `tests/test_config_sources.py`
pins the mapping; if you change one side you must change the other.

| Domain               | Plane       | Dashboard-editable                              | Stays in YAML/env                                |
|----------------------|-------------|-------------------------------------------------|--------------------------------------------------|
| `proxy/network`      | bootstrap   | nothing                                         | listener host/port, TLS, deployment shape        |
| `services/providers` | dashboard   | DB-owned service records and enablement, health/test actions, routing metadata (`source` is provenance) | seed/provisioning input and immutable package/source artifacts, secret refs, irreducible bootstrap |
| `model routing`      | dashboard   | runtime overrides and role policy               | model directories, compat tables                 |
| `roles/models`       | dashboard   | role definitions, role→model grants             | nothing                                          |
| `media jobs`         | dashboard   | admission, queue policy, cache limits           | URL signing secrets, storage roots               |
| `resource admission` | dashboard   | caps, throttles, reserved tokens                | nothing                                          |
| `workflow manifests` | provisioned | nothing                                         | the manifest files themselves                    |
| `model compatibility`| bootstrap   | nothing                                         | the compatibility table file                     |
| `auth/admin`         | dashboard   | instance presentation, OIDC provider settings, App trusted addresses, admin user list, roles, Access plans, Apps, App keys | explicit session keys, trust roots, optional OIDC bootstrap fallback |

Implications:

- The dashboard never writes a value that lives in YAML or env. If an
  operator wants such a value to change, the change path is "edit the file
  and restart with `scripts/safe-restart`" — surfaced in `change_path` and `apply_mode`.
- The dashboard writes all non-managed service/provider records regardless of
  `source`; source remains provenance after the one-time seed. The managed
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
  can seed or provision registry records; it is not the normal live operator
  surface.

## Provisioning bundles, restore, and reconciliation (deferred)

The gateway ships a narrow, read-only service-registry export at
`GET /admin/api/config/export`. It serializes service records for operator
download and excludes ephemeral runtime state. It does not restore a gateway,
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

- A database-backed config plane that absorbs `proxy_config.yml`. The
  bootstrap plane exists for a reason (early-startup, secrets, deployment
  shape); collapsing it into `admin.db` would add a chicken-and-egg loader
  problem with no operator benefit.
- A second policy editor inside Services or APIs. Policy stays on Access plans
  (see `docs/ADMIN_ACCESS_UX_RESEARCH.md`).
- A second service-registry ownership mode. The dashboard makes provenance
  legible, but the DB/UI registry remains authoritative after its one-time
  seed.
- Per-field schema validation in the dashboard for bootstrap fields. They
  are validated at gateway start; making the dashboard a second validator
  duplicates truth.

## How this stays honest

`tests/test_config_sources.py::test_doc_and_inventory_agree_on_domains`
asserts that every `CONFIG_SPECS.domain` appears in the table above and
vice versa. The table is the operator-facing source; the inventory is the
machine-facing source; the test prevents either from drifting silently.
