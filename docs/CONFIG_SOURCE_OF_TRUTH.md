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
  roles, Apps, Profiles, App API keys, identity/Profile mappings, role/model
  bindings, admission caps, runtime routing toggles, OIDC provider settings,
  and the service/provider registry.
- **Provisioning / as-code config export is deferred.** Default Profiles and
  user-access defaults are seeded into `admin.db` on startup; once seeded, the
  dashboard is authoritative. No YAML "policy file" exists for those settings.

> **Implementation invariant.** The first-run access path is DB-backed:
> `/setup` and `/admin` create Apps, App keys, Profiles, identity/Profile
> mappings, and user access defaults in `admin.db`. `admin.user_defaults` in
> `proxy_config.yml` is a bootstrap seed only; after startup, dashboard/API
> state is authoritative. The legacy `AI_GATEWAY_USER_SELF_SERVICE` and
> `AI_GATEWAY_AUTO_CREATE_PERSONAL_APP` env vars are also bootstrap seed
> inputs, not live overrides after the setting exists in `admin.db`.
> Services/providers follow the same rule: YAML/env can seed or provision
> records, but the DB/UI registry is the live operator surface.

## Ownership planes

There are four planes. Every domain in `CONFIG_SPECS` belongs to exactly one.
Concretely:

1. **Bootstrap plane** — `proxy_config.yml`, service environment variables,
   and referenced secret stores. Everything the gateway needs to *start*: the
   listener, TLS, optional explicit session signing keys, trust roots,
   secret references, and on-disk model directories. Never dashboard-writable.
   Restart to apply. Provisioning bundles never carry secrets.
2. **Dashboard plane** — `admin.db`, edited through `/admin` (and the
   `/admin/api/*` JSON surface). Runtime access policy: the admin user list,
   role grants, Profiles, Apps, App API keys, user access defaults,
   identity/Profile mappings, role→model bindings, admission caps, throttles,
   runtime routing overrides, OIDC provider settings, and service/provider
   records — anything an operator should legitimately be able to change after
   boot without touching a file. Audited, applies live unless the apply_mode
   says otherwise.
3. **Provisioned plane** — workflow manifests and catalog files committed in
   the repo (`workflows/`, the model compatibility table), plus service
   records seeded or reconciled from provisioning config. The dashboard
   surfaces provisioned services as visible and testable, but read-only unless
   the operator detaches or copies them into a UI-managed record.
4. **Secret/bootstrap plane** — env, Docker secrets, and secret references for
   values that cannot live as editable product state. The dashboard may show
   that a secret reference exists, but never exposes or edits the secret value.

Service/provider records carry an explicit ownership mode so there is no
split-brain:

- `ui` — created and edited in `/admin`; DB is authoritative.
- `provisioned` — imported/reconciled from `proxy_config.yml` or another
  provisioning source; visible and testable in `/admin`, read-only until
  detached or copied to UI-managed.
- `builtin` — shipped product defaults; visible and testable, read-only unless
  copied to UI-managed.
- `discovered` — found from runtime discovery; visible/testable as inventory,
  not editable product config until adopted.

## Per-domain ownership policy

This table is the single source of truth that the inventory in
`gateway/config_sources.py` must agree with. `tests/test_config_sources.py`
pins the mapping; if you change one side you must change the other.

| Domain               | Plane       | Dashboard-editable                              | Stays in YAML/env                                |
|----------------------|-------------|-------------------------------------------------|--------------------------------------------------|
| `proxy/network`      | bootstrap   | nothing                                         | listener host/port, TLS, deployment shape        |
| `services/providers` | dashboard   | service records, ownership mode, enablement, health/test actions, routing metadata | seed/provisioning input, secret refs, irreducible bootstrap |
| `model routing`      | dashboard   | runtime overrides and role policy               | model directories, compat tables                 |
| `roles/models`       | dashboard   | role definitions, role→model grants             | nothing                                          |
| `media jobs`         | dashboard   | admission, queue policy, cache limits           | URL signing secrets, storage roots               |
| `resource admission` | dashboard   | caps, throttles, reserved tokens                | nothing                                          |
| `workflow manifests` | provisioned | nothing                                         | the manifest files themselves                    |
| `model compatibility`| bootstrap   | nothing                                         | the compatibility table file                     |
| `auth/admin`         | dashboard   | OIDC provider settings, admin user list, roles, Profiles, Apps, App keys, user access defaults, identity/Profile mappings | explicit session keys, trust roots, optional OIDC bootstrap fallback |

Implications:

- The dashboard never writes a value that lives in YAML or env. If an
  operator wants such a value to change, the change path is "edit the file
  and restart" — surfaced in `change_path` and `apply_mode`.
- The dashboard does write UI-managed service/provider records. Provisioned,
  builtin, and discovered records remain visible and testable but read-only
  until copied or detached into `ui` ownership.
- The dashboard never writes downstream provider secrets or browser session
  signing keys. OIDC `client_secret` is the single intentional exception:
  when configured from `/admin`, it is stored in `admin.db`, redacted in UI/API
  responses, preserved on blank updates, and audited. Operators who require an
  external secret store should use the `client_secret_env` bootstrap fallback.
- `admin.user_defaults` and the user-access env flags are bootstrap seeds, not
  ongoing runtime policy. On startup, the gateway imports self-service defaults
  and identity Profile mappings into `admin.db` when they are not already
  present. Disabled dashboard mappings are not re-enabled by the bootstrap
  seed.
- Runtime Apps, Profiles, and keys live in `admin.db`. Bootstrap config stays
  in YAML/env, but runtime authorization decisions do not read a key file.
- Service/provider availability is read from the registry. `proxy_config.yml`
  can seed or provision registry records; it is not the normal live operator
  surface.

## Provisioning bundles (deferred)

Provisioning / as-code config export is deliberately out of scope for the
current MVP. The inventory still classifies rows with
`provisioning == "bundle_candidate"` so a future serializer has an obvious
target, but no shipped command consumes that classification today, and the
normal first-run path does not assume a bundle exists.

When this lands, the smallest useful "export this gateway" artifact will be
the subset of `admin.db` rows whose inventory entry has
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
- A second policy editor inside Services or APIs. Policy stays on Profiles
  (see `docs/ADMIN_ACCESS_UX_RESEARCH.md`).
- Inline editing of provisioned service definitions. The dashboard's job is
  to make provenance legible; operators edit the provisioning source or copy
  the service into `ui` ownership.
- Per-field schema validation in the dashboard for bootstrap fields. They
  are validated at gateway start; making the dashboard a second validator
  duplicates truth.

## How this stays honest

`tests/test_config_sources.py::test_doc_and_inventory_agree_on_domains`
asserts that every `CONFIG_SPECS.domain` appears in the table above and
vice versa. The table is the operator-facing source; the inventory is the
machine-facing source; the test prevents either from drifting silently.
