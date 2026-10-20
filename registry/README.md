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

## STT sample recordings

`stt_samples.yml` lists long recordings with human reference transcripts
(AMI, ICSI, Earnings-21) for measuring long-audio transcription settings.
It is data only: each sample names its licence, every file to download
(https URL, sha256, size) and a recipe (`ami-words`, `icsi-words` or
`earnings21`) that says how to read the reference words. Azizam ships no
audio.

`gateway/stt_samples.py` reads the file (`load_manifest`) and prepares one
sample on demand (`fetch_sample`). It downloads each source into a cache
named by sha256, so an archive that several samples share downloads once.
It refuses a file that is larger than declared or does not match its
checksum, and deletes it. It then writes a 16 kHz mono PCM16 WAV with
ffmpeg and builds the word list `[(word, t0, t1), ...]`. To add a sample
from a corpus that a recipe already reads, add a row; a new corpus format
needs a new recipe function. Read each licence note before you use a
sample: the Earnings-21 audio is for private local evaluation only.

## Retention

`retention.yml` holds the bundled defaults of the request-retention contract:
the shared retention choices and their lengths, the kill-switch default, one
member switch per group of kinds, and each kind's purpose, parts and readers.
`retention.schema.json` is its closed schema and `gateway/retention.py` adds the
cross-row rules. The gateway does not start without a valid file, and Python and
the web read these values through the settings API
(`docs/design/REQUEST_RETENTION.md` section 3.2).
Restart the gateway after editing `retention.yml`. A stored period keeps its
duration while its ID remains in `retention_choices`, even if it is no longer
offered for new selections; a stored `never` stays no-expiry. If its ID is
deleted, the shortest finite offered period applies. A file that offers only
`never` is valid until a stored period ID is deleted and no finite offered
period can replace it; then the gateway stops at startup and names the ID.
Startup validates the file and attempts to shorten saved prompt deadlines
before serving. If that repair fails, the owner's prompt status shows
`deadline_repair_pending` and the recurring maintenance pass retries it until
it succeeds. A transient database lock defers the stored-ID check to repair;
it does not by itself invalidate the retention file.
Slice 1 refuses `household.never_keep: true` in this file; the owner setting
performs the required erasure when that switch is enabled.
