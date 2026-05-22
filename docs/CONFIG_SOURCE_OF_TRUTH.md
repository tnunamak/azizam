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

For the operator-facing first-run story this split implies, see
[`FIRST_RUN_SETUP.md`](FIRST_RUN_SETUP.md). In short: bootstrap once via
`proxy_config.yml` + env, then drive everything else from `/admin`.

> **Implementation honesty.** Not every dashboard-plane domain is fully
> editable in the UI yet. The inventory in `gateway/config_sources.py` and
> the table below define the *target* ownership; some domains still require
> editing `admin.db` directly or going through `/admin/api/*`. The dashboard
> should never *write* a bootstrap-plane field; if a UI control appears to,
> that is a bug.

## Ownership planes

There are four planes. Every domain in `CONFIG_SPECS` belongs to exactly one.
Concretely:

1. **Bootstrap plane** — `proxy_config.yml`, service environment variables,
   and referenced secret stores. Everything the gateway needs to *start*: the
   listener, TLS, OIDC issuer/client, signing/session keys, trust roots,
   downstream provider credentials, on-disk model directories. Never
   dashboard-writable. Restart to apply. Provisioning bundles never carry
   these values.
2. **Dashboard plane** — `admin.db`, edited through `/admin` (and the
   `/admin/api/*` JSON surface). Runtime access policy: the admin user list,
   role grants, Profiles, Apps, App API keys, role→model bindings, admission
   caps, throttles, runtime routing overrides — anything an operator should
   legitimately be able to change after boot without touching a file. Audited,
   applies live unless the apply_mode says otherwise.
3. **Provisioned plane** — workflow manifests and catalog files committed in
   the repo (`workflows/`, the model compatibility table). The operator's
   change path is editing the file in version control. The dashboard surfaces
   which manifest is loaded and whether it is healthy, but does not edit it.
4. **Split plane** — domains with a legitimate bootstrap half (secrets, base
   URLs, deployment shape) and a runtime half (enablement, policy, caps). The
   bootstrap half stays in `proxy_config.yml`/env; the runtime half lives in
   `admin.db`. The dashboard must render *both* paths so an operator can see
   where each field actually lives, and only writes to the `admin.db` half.

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
