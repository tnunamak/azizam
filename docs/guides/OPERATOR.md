# Operator guide

Use this guide to run a Azizam for yourself, family, friends,
or local apps. The gateway gives clients one endpoint and one key while you
control engines, providers, people, access plans, and spend.

## Start a new gateway

For the Docker Compose path:

```bash
docker compose up -d
```

Then open:

```text
http://127.0.0.1:5000/setup
```

`/setup` creates or claims the first owner account. Continue to Home, choose an app, and use **Create key for** that app. Save the runtime key when it appears; it starts with `opk_` and is shown once. Use it in clients, and keep admin keys for administration.

Compose starts:

- `gateway`, the public API and UI on port 5000;
- `litellm`, the managed LiteLLM sidecar for chat-shaped execution.

Compose stores runtime state in the `gateway-data` Docker volume at
`/app/data`. If you want host files instead of a named volume, follow the
volume example in [First-run setup](../FIRST_RUN_SETUP.md).

## What lives where

The gateway has two main configuration areas.

Bootstrap configuration lives in files, env vars, or Docker secrets. Use it
for listener settings, TLS shape, session signing secrets, initial service
seed input, model directories, and provider secret references. A bootstrap
change can need a restart.

Runtime product state lives in `admin.db` and is edited through the dashboard
and `/admin/api/*`. This includes owners, admins, people, Apps, App keys,
access plans, identity-to-plan mappings, role and model grants, admission
caps, OIDC settings, and the service/provider registry. These changes apply
live unless the UI or API says otherwise.

`proxy_config.yml` can seed service records, but after boot the DB/UI service
registry is the live source of truth for non-managed services and providers.
The managed LiteLLM substrate is the exception: the gateway owns its endpoint
and protocol fields.

## Add local engines

Open `/ui/engines/` to manage local services. A service record should describe
an engine that already exists and is reachable by the gateway.

For a generic OpenAI-compatible chat engine:

1. Add a service record.
2. Set the OpenAI-compatible base URL as seen by the gateway.
3. Set a health endpoint such as `/v1/models`.
4. Add at least one model mapping from public model name to upstream model
   name.
5. Test the service.

When the gateway runs in Docker and an engine runs on the host, the engine
must listen on an address that the container can reach. Compose maps
`host.docker.internal` to the host gateway on Linux, but an engine bound only
to host `127.0.0.1` is usually not reachable from inside the container.

Engine-specific behavior belongs in service data, not gateway code. Use
service records, model mappings, and configured transforms rather than code
branches for an engine.

## Add remote providers

Remote providers are paid or externally hosted backends. They are service
registry records with provider-shaped config.

Provider secret values stay outside `admin.db`. Put the real key in the
gateway service environment or secret store, then store only the env var name
on the provider record. For example:

```text
OPENROUTER_API_KEY=...
TYPESAFE_API_KEY=...
```

Provider status and spend appear in `/ui/providers/` when the provider admin
API is available. The page reports enabled state, reachability, key state, and
spend totals. It does not create providers, edit providers, or show provider
key values.

Create provider records through the service registry. Use `/ui/engines/` when
the API supplies a matching preset. For records that are not exposed by that
dialog, use the `/admin/api/operator/services` service-registry API or
bootstrap/provisioning input, then manage the live DB record from the registry.
Remote provider records must be `kind: provider` and must use provider-shaped
config. See [Remote providers](../REMOTE_PROVIDERS.md) for the exact fields.

Provider cap and allowed-plan state is implemented by the admin provider API:

| Action | Endpoint |
| --- | --- |
| List providers | `GET /admin/api/providers` |
| Inspect a provider | `GET /admin/api/providers/{provider}` |
| Set or remove provider caps | `PUT` or `DELETE /admin/api/providers/{provider}/caps` |
| Set allowed plans | `PUT /admin/api/providers/{provider}/allowed-plans` |
| Set or remove global caps | `PUT` or `DELETE /admin/api/providers/-/global-caps` |

See [Remote providers](../REMOTE_PROVIDERS.md) for the request model and
ledger behavior.

## Control paid access

Paid remote execution requires all of these facts:

1. The provider is configured and enabled.
2. The provider key env var is present.
3. The requested model or alias names that provider.
4. The caller's access plan allows that provider and model.
5. Spend caps and request estimates permit the call.

The gateway does not fall back from local to paid remote execution on its own.
A paid fallback also needs explicit access-plan permission.

Provider calls reserve spend before dispatch. Settled spend is recorded in the
`provider_spend` ledger. If cost cannot be bounded while a cap applies, the
gateway fails closed before making the provider call.

Use provider-side limits as a second guard when the provider supports them.
For example, set an OpenRouter key credit limit that matches the gateway cap.

## Manage people and access plans

Open `/ui/people/`.

Use the People tab to:

- view accounts;
- change a person's access plan;
- disable or enable access;
- create a password reset link for a built-in account;
- remove a person.

Use the Invites tab to:

- create a single-use invite link;
- set an optional email restriction;
- choose an expiry of 1 day, 7 days, or 30 days;
- copy a pending invite link again;
- revoke a pending invite.

Use the Access plans tab to:

- create and edit plans;
- restrict capabilities, model locations, models, protocols, workflows, and
  engines;
- allow selected remote providers;
- set remote spend limits;
- test access before assigning a plan.

When an invitee accepts a link, the gateway assigns the chosen access plan,
creates a personal App and key, consumes the invitation, and records an audit
event. The new key is shown once.

Disabling a person revokes their keys and browser sessions and disables their
Apps. Enabling the person allows fresh sign-in and new keys, but old revoked
keys stay revoked.

## Manage apps and keys

Members can manage their own app keys at `/ui/apps/` when their account allows
it. Owners can also manage runtime access from the admin surfaces.

Use one App key per app. This keeps rotation and revocation small. App keys
start with `opk_` and authenticate runtime requests.

Admin keys start with `oak_` and belong in admin and recovery paths; keep them out of client apps.

## Back up the gateway

Back up the runtime state directory. In the default local layout, that is
`data/` next to `proxy.py`. In Docker Compose, it is the `gateway-data` volume
mounted at `/app/data`.

Make a consistent snapshot. Quiesce writes first, or use a SQLite backup method
that includes committed data from the write-ahead log (WAL). Do not copy only a
live `.db` file while a `-wal` file can contain committed data.

At minimum, preserve:

- `admin.db`;
- `admin_session_key`, if the gateway generated one;
- response, receipt, metrics, and media job databases if you need that history;
- generated media and voice data that your deployment uses;
- any external secret store or env file that holds provider keys.

The service-registry export at `GET /admin/api/config/export` is read-only. It
does not restore a gateway. Full provisioning bundles, full restore, and
file-wins reconciliation are deferred. Treat `admin.db` and the runtime data
directory as the backup artifact for the current implementation.

## Upgrade and restart

Before an upgrade, back up the runtime state directory and review the release
notes or diff for changes to config, database state, provider behavior, and UI.
Build and deploy the Python gateway and the web UI together so the static UI
matches the JSON APIs it calls.

For Docker Compose deployments, preserve the data volume. Do not remove it with
`docker compose down -v` unless you intentionally want to delete gateway state.
Pull or rebuild the image as required by your deployment, then start the stack
again with the same data volume.

For a running local service managed by systemd, restart the gateway only with
the `scripts/safe-restart` procedure. Use the dry-run mode described there
when you need to see in-flight work. A bare `systemctl --user restart azizam` bypasses that procedure. It can cut
streaming replies and synchronous media calls that are still running. See
[Safe restart](../ops/SAFE_RESTART.md).

After the upgrade, verify `/health`, sign in to the UI, check service/provider
status, and run at least one representative app request with an `opk_...` key.

Some changes do not need a restart, such as People, Apps, access plans,
OIDC settings edited in the dashboard, non-managed service records, provider
enablement, and admission caps. Bootstrap changes such as listener, TLS,
session key, env secrets, or deployment shape can need a restart.

## Troubleshooting

### `/setup` says an owner already exists

Sign in with the owner account. If you lost the browser session or OIDC is
down, use the recovery admin-key path documented in
[First-run setup](../FIRST_RUN_SETUP.md).

### A client cannot authenticate

Check that the client uses an `opk_...` runtime key. Admin keys are rejected by
runtime client routes.

### A local engine is unreachable

Confirm that the gateway can reach the service base URL. In Docker, use a
host address that the container can reach. Check the service health endpoint
and model mapping.

### A model is blocked

Inspect the caller's access plan. It can restrict capabilities, locations,
models, protocols, workflows, engines, and remote providers.

### A provider is missing its key

Set the provider key in the gateway service environment or secret store. The
provider record should store the env var name, not the key value.

### A paid request is denied

Check provider enablement, provider key state, allowed plans, request estimate,
daily or monthly caps, and unresolved provider-spend reservations. Unknown cost
can block a capped request.

### Usage says `Not measured yet`

Some monthly usage values are intentionally null until a complete measurement source exists, so this text means unknown usage rather than zero.

### A link or asset is visible without a key

Generated asset URLs can be signed capability URLs. Anyone with the signed URL
can fetch the asset until it expires.

## Further reading

| Task | Guide |
| --- | --- |
| Start and configure an instance | [First-run setup](../FIRST_RUN_SETUP.md), [configuration ownership](../CONFIG_SOURCE_OF_TRUTH.md) |
| Manage accounts | [People and built-in accounts](../PEOPLE_AND_ACCOUNTS.md) |
| Connect providers and clients | [Remote providers](../REMOTE_PROVIDERS.md), [client recipes](../LOCAL_CLIENT_RECIPES.md) |
| Check API and engine boundaries | [Public API support matrix](../SUPPORT_MATRIX.md), [engine knowledge boundary](../design/ENGINE_KNOWLEDGE_BOUNDARY.md) |
| Restart safely | [Safe restart](../ops/SAFE_RESTART.md) |
