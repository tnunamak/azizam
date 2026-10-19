# Registry Seed Catalogs

This directory contains bundled seed catalogs for the gateway registry. Core
code loads these files generically; it must not hardcode particular providers,
services, workflows, targets, or protocol frontends.

At startup, these catalogs may seed service-registry records. The catalog files
remain immutable package data, but their service rows live in `admin.db` after
the one-time seed. A service row's `source` is provenance, not permission:
every non-managed service row remains editable, enablable/disableable, and
removable from `/admin`, regardless of whether it was bundled, provisioned,
builtin, discovered, or created in the UI.

The managed LiteLLM substrate is the sole exception. Its gateway-derived
endpoint/protocol fields are locked and it cannot be removed; its name, enabled
state, and note remain editable.

## Protocol descriptions

`registry/protocols/*.yml` hold protocol descriptions: data that says how
Azizam talks to a kind of engine (requests, body mappings, framing, auth,
async jobs). `protocol.schema.json` is their closed schema; the loader is
`gateway/engine_protocol/description.py`. The file name must equal the `id`.
A description names no engine and no code. See
`docs/design/ENGINE_PROTOCOL_EXECUTOR.md`.

Three descriptions say `executor: adapter:<id>`: `node-graph`, `inline-tts` and
`chunked-stt`. They list operation names only (the reviewed adapter owns the
wire) and carry `serves` (capabilities the adapter answers for when an engine
record names no protocol), `label` (the engine's name in caller-facing
messages) and, for `node-graph`, `binding` (the key under a workflow manifest's
`bindings`). An engine record selects an adapter by naming its protocol, or by
the capability of the route it serves.

## Retention

`retention.yml` holds the bundled defaults of the request-retention contract:
the shared retention choices and their lengths, the kill-switch default, one
member switch per group of kinds, and each kind's purpose, parts and readers.
`retention.schema.json` is its closed schema and `gateway/retention.py` adds the
cross-row rules. The gateway does not start without a valid file, and Python and
the web read these values through the settings API
(`docs/design/REQUEST_RETENTION.md` section 3.2).
Restart the gateway after editing `retention.yml`. Startup validates the file
and shortens saved prompt deadlines under stricter periods before serving.
Slice 1 refuses `household.never_keep: true` in this file; the owner setting
performs the required erasure when that switch is enabled.
