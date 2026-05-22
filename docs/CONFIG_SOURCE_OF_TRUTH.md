# Config Source of Truth

Date: 2026-05-21

Self-hosted operators currently see *many* config sources surfaced through the
admin "Sources of Truth" panel — proxy YAML, env, workflow manifests, the admin
database, and split bootstrap/runtime fields. The
`gateway/config_sources.py` inventory already classifies every domain with
explicit lifecycle facts (`authored_via`, `stored_in`, `writable`,
`provisioning`, `apply_mode`). This note pins down the *policy* behind those
facts so future work has a target, and so no setting silently drifts between
file and dashboard ownership.

The principle is Rich-Hickey-shaped: each setting has exactly one durable
home, and the dashboard only writes settings whose home is `admin.db`.

## Ownership planes

There are four planes. Every domain in `CONFIG_SPECS` belongs to exactly one.

1. **Dashboard plane** — authored in the admin UI/API, stored in `admin.db`,
   audited, included in provisioning bundles, applies live unless the
   apply_mode says otherwise. Operators expect to manage these without
   touching files or restarting the gateway.
2. **Bootstrap plane** — authored in `proxy_config.yml` or service env,
   stored where it was authored, never dashboard-writable, restart to apply.
   Includes all secrets. Bundles never carry these values.
3. **Provisioned plane** — workflow manifests and catalog files. Hackable on
   disk by the operator, read-only in the dashboard. The dashboard shows
   which manifest is loaded and whether it is healthy.
4. **Split plane** — domains that legitimately have a bootstrap half and a
   runtime half. The runtime half lives in `admin.db`; the bootstrap half
   stays in YAML/env. The dashboard must render *both* paths, never collapse
   them.

## Per-domain ownership policy

This table is the single source of truth that the inventory in
`gateway/config_sources.py` must agree with. `tests/test_config_sources.py`
pins the mapping; if you change one side you must change the other.

| Domain               | Plane       | Dashboard-editable                              | Stays in YAML/env                                |
|----------------------|-------------|-------------------------------------------------|--------------------------------------------------|
| `proxy/network`      | bootstrap   | nothing                                         | listener host/port, TLS, deployment shape        |
| `backends`           | split       | enablement, priority, routing policy            | provider credentials, base URLs, timeouts        |
| `model routing`      | split       | runtime overrides and role policy               | model directories, service names, compat tables  |
| `roles/models`       | dashboard   | role definitions, role→model grants             | nothing                                          |
| `media jobs`         | split       | admission, queue policy, cache limits           | URL signing secrets, storage roots               |
| `resource admission` | dashboard   | caps, throttles, reserved tokens                | nothing                                          |
| `workflow manifests` | provisioned | nothing                                         | the manifest files themselves                    |
| `model compatibility`| bootstrap   | nothing                                         | the compatibility table file                     |
| `auth/admin`         | split       | admin user list, admin runtime policy           | session keys, OIDC client secret, trust roots    |

Implications:

- The dashboard never writes a value that lives in YAML or env. If an
  operator wants such a value to change, the change path is "edit the file
  and restart" — surfaced in `change_path` and `apply_mode`.
- The dashboard never writes a secret. Secrets are `secret_bootstrap` in the
  inventory and excluded from provisioning bundles by construction.
- Split domains keep two rows of mental model: a YAML row for the bootstrap
  half, an `admin.db`-backed row for the runtime half. The inventory's
  `authored_via` and `stored_in` strings spell this out so neither half
  hides behind the other.

## Provisioning bundles

The smallest useful "export this gateway" artifact is the subset of
`admin.db` rows whose inventory entry has
`provisioning == "bundle_candidate"`. That gives an operator a way to:

- copy a gateway to a new machine without copying secrets,
- diff "what changed via the dashboard" against the committed YAML, and
- treat `admin.db` as authoritative for everything the dashboard can edit.

Rows tagged `bootstrap_only`, `secret_bootstrap`, `source_manifest`, or
`split` are *not* part of the bundle. The split rows' YAML half is already
under version control where the operator wants it; the runtime half is
recreated from the bundle.

This is the contract the next tranche of code should target. No new tables,
no new abstractions — just a serializer that walks `admin.db` and filters
by inventory provisioning class.

## Things to deliberately *not* build yet

- A database-backed config plane that absorbs `proxy_config.yml`. The
  bootstrap plane exists for a reason (early-startup, secrets, deployment
  shape); collapsing it into `admin.db` would add a chicken-and-egg loader
  problem with no operator benefit.
- A second policy editor inside Services or APIs. Policy stays on Profiles
  (see `docs/ADMIN_ACCESS_UX_RESEARCH.md`).
- Inline editing of split-plane YAML halves. The change path is editing
  the file; the dashboard's job is to make that legible, not to redo it.
- Per-field schema validation in the dashboard for bootstrap fields. They
  are validated at gateway start; making the dashboard a second validator
  duplicates truth.

## How this stays honest

`tests/test_config_sources.py::test_doc_and_inventory_agree_on_domains`
asserts that every `CONFIG_SPECS.domain` appears in the table above and
vice versa. The table is the operator-facing source; the inventory is the
machine-facing source; the test prevents either from drifting silently.
