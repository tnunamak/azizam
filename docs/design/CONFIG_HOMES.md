# One home per setting

Status: design, revision 1, 2026-11-05. Base: main 810b57a25. Not built. An independent review of the first draft
found twelve defects (load-path widening, alias rollback, slice dependencies, registry stale copies, CAS gaps, wrong
line numbers, over-design); this revision includes the fixes.

The owner approved this rule on 2026-11-05:

1. Anything the owner might change lives in admin.db. The API edits it (the UI comes later). An export writes it to a
   file for backup.
2. Defaults that Azizam ships are read-only release files. The owner's sparse overrides in the database win. The owner
   can reset a value to inherit, and an effective-source view shows which source won for each value.
3. Startup basics (port, admin.db path, login boot settings) stay in a tiny file or `.env.local`.

This document settles the foundation: the override store, the layering, validation, live reload, export and import,
the API, the migration and the slice plan. Section 11 is the red team.

Evidence rule. Each claim names a file and line, or a prior-art research note. "Ungrounded" marks a claim with neither.

Research notes used (internal research notes):

- **[MASTER]** A prior-art note on configuration homes for UI-configured self-hosted apps. One database master per setting, read-only shipped defaults, sparse overrides, an effective-source
  view, reset-to-inherit and a complete export. A seed-once file is the worst shape (Open WebUI). Two writable masters
  for one key cause the bugs (LiteLLM). The export matters more than which side is master (NPM, Uptime Kuma).
  Confidence: moderate. No source measures outcomes.
- **[RB]** A red-team report on Azizam's access model, Report B. Read-only upstream snapshot keyed by stable id, override records (replace, delete, reset-to-inherit),
  tombstones, an effective view, and lists that replace by default. Helm, Renovate, dpkg and ClearlyDefined are its
  sources (S46 is the Helm values-files page).

---

## 1. What exists today (measured on 810b57a25)

### 1.1 Where settings live

| Fact | Evidence |
|---|---|
| `proxy_config.yml` has 1,660 lines and 17 top-level sections. | `proxy_config.yml:1-1660` |
| A fresh install boots from `first-run.yml`. That file holds only `proxy`, `admin.oidc.cookie` and `backends: {}`. It has no aliases, roles or media sections. | `first-run.yml`; `docs/FIRST_RUN_SETUP.md:44,146` |
| So a third party's "defaults" today are code literals in proxy.py, for example `GGUF_DIR` defaults to `/media/windows/AI Models/LLMs/GGUF` and `AUDIO_DEFAULT_WORKFLOW` to `ace-step-1.5-xl-turbo`. | `proxy.py:807`, `proxy.py:25132` |
| The tracked `proxy_config.yml` holds this deployment's own values: roles name GGUF files on a local disk, `auth.trusted_proxies` names a LAN address, `identity.face_service` names a user unit. | `proxy_config.yml:860-980`, `:1641`, `:832-849` |
| The live host has an uncommitted edit. It points chat aliases `default` and `fast` at Strata services and adds `default-fast`. | `git -C ~/applications/azizam diff proxy_config.yml` (hunk at line 1238) |
| The service registry is one JSON document in `operator_settings` (key `service_registry`) with one CAS generation. | `gateway/admin_store.py:250-264`, `:4219-4245`, `:4352-4398` |
| At every boot the registry copies bundled metadata and `proxy_config.yml` backend fields into the stored records. | `gateway/service_registry.py:94` (`backfill_seed_catalog_metadata`), `:533-581`, `:612-650` |
| The live registry holds 24 services (1 builtin, 8 provisioned from the file seed, 15 made in the UI) and 8 protocol frontends, all `source: builtin`, that is, seed copies. | `sqlite3 -readonly` count, key names only |
| A bundled workflow manifest beats a stored one with the same id. The handoff placed this in discovery.py; the skip is in proxy.py. | `proxy.py:13246` (`if registry.get(record.id) is not None: continue`) |
| Four override mechanisms already exist, each with its own merge rule: `instance_presentation` (shallow update over defaults), `media_job_admission_overrides` (an allow-list of keys over a file dict), `service_availability_overrides`, and the separate `retention_fact_overrides` table. | `gateway/settings_api.py:18,23-28`; `proxy.py:17560-17582`; `proxy.py:17518`; `gateway/admin_store.py:267-274` |
| A hand-kept table says which source owns each section. A test pins it to a doc. | `gateway/config_sources.py:63-89`; `tests/test_config_sources.py`; `docs/CONFIG_SOURCE_OF_TRUTH.md` |
| Two precondition conventions are in use. The registry uses `If-Match: "os-N"`, 412 on a mismatch. Settings use `If-Match: "N"`, 428 when missing and 409 on a mismatch. | `gateway/admin_api.py:2526-2541`; `:3176-3183` |
| The `routing` section (`default_backend`, `audio_backend`) has no reader except a projection type. | `grep` shows only `gateway/projection_types.py:266` |
| Release data with closed JSON Schemas (Draft 2020-12) already lives in `registry/`. A broken file stops the gateway with the rule it broke. | `registry/retention.yml:1-8`; `gateway/retention.py:37,204` |

### 1.2 Import-time config reads in proxy.py

Method: an AST scan of the statements that run at module level in proxy.py (function and class bodies skipped). It
follows names assigned from `config`, `auth_cfg`, `admin_cfg` and `_bootstrap_config`, and from names derived from
those. Script: kept with this design's scratch notes, rerun by slice C0 as a ratchet.

- 43 module-level statements read a root config name directly.
- 159 module-level statements are in the closure (they read a derived name).
- 96 named constants hold a config-derived value: 93 found by the scan, plus 3 computed inside a called function that
  the scan cannot see (the scan of this paragraph skipped class bodies and counted lambda bodies; the corrected scan in
  `scripts/config_import_reads.py` prunes function and lambda bodies and counts class bodies, defaults, decorators,
  calls through `runtime()`, containers filled at import and calls handed a root, and measures 97 before C0 and after C1) (`LLM_BACKEND_HOSTS` `proxy.py:653`, `_GRAPH_BINDING` `:943`, `ADMIN_OIDC_CONFIG` `:1969`).

The handoff counted 74. The method differs (the handoff did not say which names it counted). This design uses 96 named
constants as the ratchet base.

Classes:

- **A. Re-derived on reload today.** `_derive_backend_constants` (`proxy.py:1095-1142`) reassigns these.
- **B. Registry-derived, not re-derived.** They go stale after a registry edit until a restart. This is a defect today.
- **C. Owner setting; becomes a per-request read from the snapshot (live).**
- **R. Owner setting; stored in admin.db, applies at restart** (shown as "pending restart" in the effective view).
- **S. Startup basic (rule 3).** Stays in the startup file or `.env.local`.

| Line(s) in proxy.py | Constant | Reads | Class | Slice |
|---|---|---|---|---|
| 457, 1966 | `ADMIN_DB_PATH` | `admin.db_path`, env `AZIZAM_ADMIN_DB` | S | C10 |
| 598, 599 | `PROXY_HOST`, `PROXY_PORT` | `proxy.*`, env `AZIZAM_PORT` | S | C10 |
| 653 | `LLM_BACKEND_HOSTS` | `backends` (registry) | A | C9 |
| 807 | `GGUF_DIR` | `model_routing.gguf_dir` | S (storage root) | C10 |
| 808 | `SWITCH_TIMEOUT` | `model_routing.switch_timeout` | C | C7 |
| 820 | `UPSTREAM_READ_TIMEOUT` | `model_routing.upstream_read_timeout`, env | C | C7 |
| 824 | `ROLES` | `roles` | C | C7 |
| 875 | `MODELS_CATALOG` | `models` | C | C7 |
| 879, 880, 897, 898, 899, 903, 907, 914, 915 | `TTS_ENABLED`, `TTS_HOST`, `STT_ENABLED`, `STT_HOST`, `STT_TIMEOUT`, `EMBEDDINGS_ENABLED`, `EMBEDDINGS_HOST`, `COMFY_ENABLED`, `COMFY_HOST` | `backends.{tts,stt,embeddings,comfyui}` (registry) | A | C9 |
| 881, 908, 909, 910, 916, 921, 928 | `TTS_TIMEOUT`, `EMBEDDINGS_HEALTH_ENDPOINT`, `EMBEDDINGS_TIMEOUT` (also env), `EMBEDDINGS_BACKEND_MODEL`, `COMFY_TIMEOUT`, `COMFY_DEFAULT_MODEL`, `COMFY_VIDEO_TIMEOUT` | same registry records | B | C9 |
| 918, 925 | `COMFY_WORKFLOWS_DIR`, `COMFY_VIDEO_WORKFLOWS_DIR` | registry record `comfyui` | B | C9 |
| 943 | `_GRAPH_BINDING` | registry record `comfyui` | B | C9 |
| 957, 961, 991 | `IMAGE_CACHE_DIR`, `VRAM_ESTIMATES_FILE`, `IMAGE_MEDIA_JOB_PAYLOAD_DIR` | `images.*` | S (storage roots) | C10 |
| 965, 967, 968, 969, 971, 972, 979, 984, 985 | `IMAGE_SIGNED_URL_TTL`, `IMAGE_VRAM_RESERVE_BYTES`, `IMAGE_FREE_VRAM_ON_EXHAUSTION`, `IMAGE_MAX_BATCH_SIZE`, `IMAGE_BACKGROUND_REMOVAL_ENABLED`, `IMAGE_BACKGROUND_REMOVAL_TIMEOUT`, `IMAGE_VARIATIONS_DEFAULT_WORKFLOW`, `IMAGE_VARIATIONS_DENOISE`, `IMAGE_MULTIREF_EDIT_DEFAULT_WORKFLOW` (and holders `IMAGE_BACKGROUND_REMOVAL_CFG` 970, `IMAGE_VARIATIONS_CFG` 974, `IMAGE_EDITS_CFG` 975) | `images.*` | C | C8a |
| 966 | `IMAGE_SIGNED_URL_SECRET_CFG` | `images.signed_url_secret` | S (secret: env or the secret store) | C10 |
| 973, 995, 996, 997, 999 | `IMAGE_MEDIA_JOBS_CFG`, `IMAGE_MEDIA_JOB_MAX_ATTEMPTS`, `..._RETRY_BASE_SECONDS`, `..._RETRY_MAX_SECONDS`, `..._RESUME_ON_START` | `images.media_jobs.*` | C (`resume_on_start`: R, read once at start by meaning) | C8a |
| 998, 1003, 23034 | `IMAGE_MEDIA_JOB_CONCURRENCY`, `_image_media_job_semaphore`, `_audio_media_job_semaphore` | `images.media_jobs.concurrency` | R (a semaphore sized at import) | C8a |
| 1006, 1007 | `_SESSION_MAKE_CFG`, `SESSION_MAKE_LIMITS` | `images.media_jobs.make.*` | C | C8a |
| 988, 989, 990 | `GATEWAY_CFG`, `RESOURCE_ADMISSION_CFG`, `MEDIA_JOB_ADMISSION_CFG` | `gateway.resource_admission.media_jobs` | C (already overlaid per request at `proxy.py:17565`) | C6 |
| 1016, 1017, 1018, 1025, 1030, 1031, 1033, 1034 | `VIDEO_DEFAULT_WORKFLOW`, `VIDEO_WORKFLOWS`, `VIDEO_ALIASES`, `VIDEO_REMIX_WORKFLOW`, `VIDEO_EXTEND_WORKFLOW`, `VIDEO_MAX_TOTAL_SECONDS`, `VIDEO_ALLOW_UNTRACKED_IDS`, `VIDEO_CHAIN_SEGMENT_SECONDS` | `videos.*` (`aliases` is folded from `model_aliases` at `gateway/stable_aliases.py:401`) | C (`VIDEO_ALIASES` moves with aliases) | C8b, C3 |
| 1049 | `PIN_LOADED_MODEL` | `llm.pin_loaded_model` | C | C7 |
| 1870, 2106, 2110, 2129, 2131 | `AUTH_PUBLIC_PATHS`, `ALLOW_QUERY_PARAM_KEY`, `CLOUDFLARE_MODE`, `TRUSTED_PROXIES`, `SETUP_ALLOWED_CIDRS` | `auth.*` and env | S (login boot) | C10 |
| 1969 | `ADMIN_OIDC_CONFIG` | `admin.oidc` file fallback; admin.db `oidc_provider_config` wins | S (bootstrap fallback) | C10 |
| 2706, 2714, 2715, 2719, 2720 | `MODEL_COMPAT_ENABLED` (also env), `OPENAI_MODELS_API_URL`, `OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE` (also env), `MODEL_COMPAT_DEFAULTS`, `MODEL_COMPAT_ALIASES` | `model_compatibility.*` | C | C8c |
| 2710 | `OPENAI_MODEL_ALIASES_FILE` | `model_compatibility.generated_aliases_file`, env | release data file (`openai_model_aliases.generated.yml` moves under `registry/`) | C8c |
| 2721, 2731, 2732, 2733 | `TTS_BACKEND_MODEL`, `STT_BACKEND_MODEL`, `EMBEDDINGS_MODEL`, `IMAGE_EDIT_DEFAULT_MODEL` | `model_compatibility.defaults` plus registry records | C (and B for the registry part) | C8c, C9 |
| 13226, 13227 | `_PROTOCOL_ROUTE_REGISTRY`, `CANONICAL_PROTOCOL_ROUTE_ALIASES` | registry | B | C9 |
| 15053 | `MCP_OAUTH_ENABLED` | `mcp.oauth.enabled` | S (login boot) | C10 |
| 15165, 15363 | `DECISION_RUNTIME`, `MESH_DEPS` | `lambda: config` | already per request (reads the latest global; moves to the snapshot accessor) | C1 |
| 15403 | `IDENTITY_CFG` | `identity` | C | C8c |
| 19840, 19846, 28695 | `_RUNTIME_ROUTE_POLICY`, `_ASSET_CONTENT_ROUTE_POLICY_BY_KIND`, `_AZIZAM_POLICY` | registry | B | C9 |
| 25132, 25137, 25138 | `AUDIO_DEFAULT_WORKFLOW`, `AUDIO_WORKFLOWS_CFG`, `AUDIO_ALIASES` | `audio.*` | C (`AUDIO_ALIASES` moves with aliases) | C8b, C3 |
| 25134 | `AUDIO_WORKFLOWS_DIR` | `audio.workflows_dir` | S (storage root) | C10 |
| 31978, 32043 | frontend mount at import; `RequestBodyLimits.from_config(config)` | registry; `limits` | frontends: A (re-prepared at `proxy.py:1146`); body limits: R | C9, C10 |

Totals: class A 10, class B 17, class C 49, class R 4, class S 16 (the 96 constants; holders counted with their leaves).
The rest of the code reads `config` per request: 61 reads of `backends`, 10 of `model_aliases`, and a few of
`roles`, `videos`, `voices`, `reasoning`, `identity` (grep of `config.get("<section>"` in proxy.py). Those see a
reload already, because `_publish_runtime_config` swaps the global (`proxy.py:1254`). They do not see one snapshot per
request (section 5).

Env overrides of settings that are not startup basics: `UPSTREAM_READ_TIMEOUT` (`proxy.py:820`), `EMBEDDINGS_TIMEOUT`
(`:909`), `AZIZAM_MODEL_COMPAT_ENABLED` (`:2707`), `OPENAI_MODEL_ALIASES_FILE` (`:2711`),
`OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE`, `IMAGE_MEDIA_JOB_RESUME_ON_START`, `COMFY_ENABLED`, `COMFY_HOST`
(`gateway/config_sources.py:104-127`). Each is a second writable master for one key (section 11, F4).

---

## 2. Decisions in short

1. **One generic layer.** Every owner-changeable section is a *section*: a read-only release defaults file plus a
   closed JSON Schema under `registry/defaults/`, and sparse owner overrides in one admin.db table. There are no
   per-section tables.
2. **The unit of override is an entry.** The section schema declares where entries are (annotations). An override
   is keyed by `(section, JSON Pointer)`. Ops are `set` (replace the entry) and `delete` (a tombstone).
   Reset-to-inherit removes the row.
3. **The service registry is a peer under the same contract.** It keeps its document and its `os-N` generation in this
   program. It stops the backfill. It reads bundled records from release files at load and stores only owner records,
   field-level patches of bundled records and tombstones.
4. **Stored beats bundled.** A stored workflow with a bundled id is an owner override of that entry.
5. **A bad override never blocks boot.** It is rejected and reported by a deterministic greedy rebuild. Entries
   marked fail-closed become unconfigured instead of inheriting.
6. **One snapshot per request.** A reload builds an immutable runtime snapshot and publishes it with one reference
   swap. A request binds the snapshot at its start.
7. **Export holds overrides only**, with secret references and no secret values. Import is dry-run first, then one
   atomic change. Round trip on the same release gives the same effective config, byte for byte.
8. **Migration is one generic step per section.** At first boot of a section, a one-time import makes the effective
   section equal to the legacy file section, with the fewest overrides. The live Strata aliases move this way.
9. **Release defaults are neutral.** They reference only things the release ships. This deployment's values become
   its overrides through the import.
10. **`proxy_config.yml` ends as the startup file only.** `first-run.yml` merges into it. Host-specific startup values
    go in `.env.local` or an untracked file named by `AZIZAM_CONFIG_PATH`.

---

## 3. The override store

### 3.1 Sections are data

A section is two files:

```
registry/defaults/<section>.yml          # release defaults, read-only, replaced on upgrade
registry/defaults/<section>.schema.json  # closed JSON Schema, Draft 2020-12, with x-azizam-* annotations
```

The section id is the file stem. Adding a section needs no code for storage, API, export, validation of structure,
the effective view or the drift report. Code is still needed for whatever *reads* the setting, and for an optional
semantic validator (3.5). Precedent: `registry/retention.yml` and `registry/retention.schema.json`
(`gateway/retention.py:37,204`). The loader uses `jsonschema.Draft202012Validator`, as `gateway/retention.py:204`
and `gateway/engine_definitions.py:178` do.

Schema annotations (JSON Schema 2020-12 allows unknown keywords as annotations):

| Annotation | Values | Meaning |
|---|---|---|
| `x-azizam-entry` | `collection` | Each member of this object is one entry. The owner may add members only if the schema allows them (`additionalProperties` or `patternProperties`). |
| | `atomic` | This node is one entry. Its inner structure is replaced whole. Lists are always atomic. |
| (none) | | An object recurses into its properties. A scalar or a list is an entry. |
| `x-azizam-apply` | `live` (default) or `restart` | When a change takes effect. The effective view shows "pending restart". |
| `x-azizam-fail` | `inherit` (default) or `closed` | What an invalid override does (3.5). Policy and security entries use `closed`. A `closed` entry must be optional in the schema, so that its absence is valid. |
| `x-azizam-secret` | `true` | The entry holds only a secret reference (`gateway/secret_refs.py:1-9`). A plain value is refused. |

The entry index (every pointer where an entry may sit) is computed from the schema once per release.

Ungrounded: the annotation names. The idea of declared boundaries comes from [RB] ("define map/list semantics").

### 3.2 Tables

```sql
-- One row per owner override. Absent row = inherit the release default.
CREATE TABLE config_overrides (
  section     TEXT NOT NULL,                 -- section id, e.g. 'model_aliases'
  pointer     TEXT NOT NULL,                 -- RFC 6901 JSON Pointer inside the section, never ''
  op          TEXT NOT NULL CHECK (op IN ('set', 'delete')),
  value_json  TEXT,                          -- NULL exactly when op = 'delete'
  base_json   TEXT,                          -- the release value when the override was written; NULL = absent
  origin      TEXT NOT NULL,                 -- 'api' | 'import' | 'legacy-import' | 'migration:<step>'
  note        TEXT,
  change_id   TEXT NOT NULL,
  updated_at  INTEGER NOT NULL,
  updated_by  TEXT,
  PRIMARY KEY (section, pointer)
);

-- One row per owner change (an atomic group of entry edits). Values are not secret (3.1, x-azizam-secret).
CREATE TABLE config_changes (
  change_id   TEXT PRIMARY KEY,
  created_at  INTEGER NOT NULL,
  kind        TEXT NOT NULL,                 -- edit | reset | import | legacy-import | revert | migration
  actor_kind  TEXT, actor_id TEXT, actor_label TEXT,
  note        TEXT,
  items_json  TEXT NOT NULL                  -- [{section, pointer, before: {op, value} | null, after: {op, value} | null}]
);
```

- **Key.** `(section, pointer)`. The pointer uses RFC 6901 so that ids with `.` and `/` stay unambiguous, for example
  `/aliases/nomic-embed-text-v1.5` and `/defaults/speech` with a value `k2-fsa/OmniVoice`
  (`proxy_config.yml:1612-1629`). A pointer must be in the entry index; otherwise the write fails with
  `not_an_entry`.
- **Revision.** One revision per section, in the existing `operator_settings_generations` table under the key
  `config:<section>`, advanced in the write transaction (`gateway/admin_store.py:4201-4206`). The global config
  generation is the tuple of all section revisions plus the registry generation. A restored database is detected the
  same way the registry cache detects it: the digest of the stored rows is part of the snapshot key
  (`gateway/service_registry.py:163-170`).
- **Audit.** Each change writes one `audit_events` row (`gateway/admin_store.py:289-299`) in the same transaction:
  action `config.change`, target kind `config_change`, target id the change id, metadata `{kind, sections,
  entry_count}`. Values stay in `config_changes`, not in the audit feed.
- **Revert.** `POST .../changes/{id}/revert` writes the inverse items as a new change, under the same CAS rules.
  [RB] Report A lists rollback as missing from the earlier designs.
- **Tombstone.** `op = 'delete'` removes an entry from the effective section. It survives a release that adds the
  entry again, because the key is the stable id ([RB]: "tombstones so upstream reintroduction does not resurrect
  owner-deleted items").
- **Reset-to-inherit.** Deleting the row. This is different from a tombstone ([RB]: "explicit delete/reset-to-inherit").
- **Owner-added entries.** `op = 'set'` on a pointer the release does not ship (`base_json` is NULL).
- **Secrets.** Never in this table. A secret entry holds a `sref_...` reference.

### 3.3 Relation to the existing stores

| Store | Decision | Slice |
|---|---|---|
| `operator_settings.instance_presentation` | Becomes section `instance`. Defaults move from `gateway/settings_api.py:23` to a release file. | C6 |
| `operator_settings.media_job_admission_overrides` | Becomes overrides of section `resource_admission` (the file part of `gateway.resource_admission`). The key allow-list at `proxy.py:17567-17579` becomes the schema. | C6 |
| `operator_settings.service_availability_overrides` | Becomes section `service_availability` (a collection keyed by service id, no release entries). | C6 |
| `retention_fact_overrides` table | Stays a peer for now. It has its own schema and is deleted with its service (`gateway/admin_store.py:4383-4389`). Fold it in only if a second reason appears. | none |
| `operator_settings.oidc_provider_config` | Stays. The `admin.oidc` file block is a startup fallback that loses silently when the DB has a value (`gateway/config_sources.py:150-163`); the effective view must show which one won (F6). | C10 |
| `operator_settings.service_registry` | A peer under the same contract (3.4). | C4 |

### 3.4 The service registry: a peer under the same contract

The registry is a whole document with cross-record validators: public alias uniqueness, protocol dispatch, model policy
graphs (`gateway/service_registry.py:371-550`, `:1410`). Six write paths funnel through `admin_runtime.py`
(`:1162`, `:1251`, `:1726`, `:1826`). Moving its storage into `config_overrides` would touch every reader of the
document shape for no new behaviour. So in this program the registry keeps its storage and adopts the contract:

1. **Release records are read, not copied.** Bundled services (`registry/services.yml`), builtin protocol frontends
   (`registry/protocol_frontends.yml`) and bundled workflow manifests (`manifests/workflows/`) are the release layer.
   They are layered under the stored document at load time, every boot.
2. **The stored document holds only owner data:** records the owner made, field-level patches of release records
   (same id; RFC 7396 merge patch; the merged record must validate), and a `tombstones` map `{collection: [ids]}`.
   A patch holds only the fields the owner changed, so a release change to any other field reaches the install. The
   LiteLLM substrate is the first user: the owner may change `name`, `enabled` and `note`
   (`docs/CONFIG_SOURCE_OF_TRUTH.md`, "managed LiteLLM substrate").
3. **No backfill.** `backfill_seed_catalog_metadata`, `backfill_normalized_registry`, `_backend_config_seeds`,
   `add_mesh_upgrade_records` and the `MESH_UPGRADE_STEP` path go (`gateway/service_registry.py:533-720`, `:94-101`).
   The seed from `proxy_config.yml` `backends` goes (`:181-220`). The one-time registry migration in C4 turns stored copies of
   release records into patches of their differing fields only (8.3).
4. **Same views.** The registry GET returns the per-record source (`release`, `owner`, `owner-added`, `owner-deleted`),
   the drift state and the problems, in the same shape as a config section.
5. **Same export.** Owner registry records are part of the one export (section 6).

Storage unification (registry records as rows of `config_overrides`) is deferred. Do it only if the two stores cause
a measured problem. Ungrounded either way.

### 3.5 Validation

Two levels, both run on the *candidate effective section*, never on an override alone:

1. **Structure.** The section's JSON Schema. A section with only structural rules needs no code.
2. **Semantics.** An optional reviewed function registered by section id, for cross-entry rules. Example:
   `model_aliases` uses `gateway/stable_aliases.validate_aliases` (`gateway/stable_aliases.py:318-384`): an entry in
   the section's own `engines` map that a stable name lists but the map lacks, loops, glob names that do not match.
   A validator receives the full candidate as read-only context: `{"registry": the service registry, "sections":
   {id: candidate value of every section}}`. The startup file is not in it (C10 deletes the file's role). It returns
   problems as `(pointer, code, severity)`, where severity is `invalid` or `dangling`. Today `validate_aliases`
   returns bare strings; C3 changes it to this shape.

A problem's pointer maps to the entry that contains it (the longest entry pointer that is a prefix). Problems are of
two kinds:

- **Invalid** (structure, or a semantic rule inside the section). The override is not applied.
- **Dangling** (a reference to a thing outside the section that does not exist now: an alias target with no deployed
  service, a workflow default with no engine). The override is applied and shown with a warning. Rationale: services come and go at run
  time; if a missing service made the owner's alias fall back to the release target, deleting a service would silently
  move traffic. Today the resolver already returns `model_not_deployed` for this case (`gateway/stable_aliases.py:60-77`).

For `model_aliases`, every check in `validate_aliases` today is internal to the section, so each one is `invalid`. A
stable target that names no deployed service is `dangling`; `validate_aliases` does not check it today, and C3 adds it
as a warning only. A builder adding a check to any validator must state its severity in the check's test.

**On the write path** (API, import): a change that adds an invalid problem is refused with 422 and the list of problems by
pointer. Nothing is written. The write path builds the whole candidate config twice with the function the load path uses
(`ConfigHome.build_candidate`): once from the stored rows, once from the stored rows plus the change. It refuses
two kinds of `invalid` problem. (1) Any `invalid` problem on an entry the change touches (adds, replaces or removes), so
a new bad value written at a pointer that is already bad is refused; a value that fixes the pointer, or a `reset` of it,
is accepted. (2) On every other entry, the problems that the second candidate has and the first does not (the rule of the
greedy rebuild below). A stored row that already went bad, or release defaults that already break a rule, do not refuse a
later write to another entry. A valid pair of changes
to two sections is accepted whatever order their ids sort in, and a change to one section that breaks the rule of another
is refused, so a committed change is never silently rejected on the next reload. The check runs inside the write transaction (the `validate_in_transaction` hook of
`put_operator_setting_with_audit`, `gateway/admin_store.py:4115-4180`).

**On the load path** (boot, reload): deterministic, never blocks boot.

1. Validate the release defaults of every section, by the schema and by each section's rule together
   (`ConfigHome.verify_release_defaults`). They must be valid; CI checks them. A broken release file, or a `<id>.yml` with
   no `<id>.schema.json`, stops the gateway with the rule it broke, as `registry/retention.yml` does today. This is a
   packaging fault, not an owner fault. This check runs when `ConfigHome` is made, at import, before any registry is
   read, so a rule gets `context["registry"] = None` there. A rule that needs the registry cannot run yet; its
   `validator_error` is not a finding of this check. The rule runs with the real registry at every build and what it finds
   is a reported problem, not a stop. A rule must therefore return problems (never raise) when it has a registry.
2. Apply all overrides of the section. Validate. If there is no `invalid` problem, done (the normal case).
3. Otherwise, rebuild the section greedily. Start from the release defaults with every `closed` entry that has an
   override removed (every override whose pointer is a `closed` entry, even one that failed its shape check). Try the
   `delete` ops first and then the `set` ops, each in pointer order, one at a time; keep an op only if it adds no
   `invalid` problem. A `delete` of a required property never reaches this step: it is refused as `delete_required`.
   Repeat the pass over the rejected ops until a pass keeps none, so an override that refers to a later one is not lost. Each pass keeps at least
   one op or stops, so the loop ends after at most n passes, and the result is the same on every boot.
4. A rejected override follows its entry's `x-azizam-fail`. `inherit`: the release value stands. `closed`: the entry is
   absent (unconfigured), never the release value. Each rejected override is a problem with its pointer.
5. The greedy rebuild does not need problems to carry a pointer. A validator that returns a problem with no pointer
   still works; it only costs more validations.
6. Settle across sections. Steps 2 to 5 judge a section against the owner's intent for the others (what their rows would
   give if all applied). After them, every section's rule is judged against the sections as built. A section that now
   breaks its rule is rebuilt against that context (steps 2 to 5 again, so its offending overrides drop out and it
   inherits). All sections are judged and rebuilt together in a round, so no result depends on the order of the ids or the
   rows. Repeat until a round changes nothing; the loop is bounded by the number of sections plus one. If it has not
   settled by then, the sections that moved in the last round serve their release defaults and carry a
   `config_unsettled` problem. Nothing that breaks a rule across sections is published as if it were fine.

Problems are published with the snapshot as `CONFIG_PROBLEMS`, next to `REGISTRY_APPLY_PROBLEM` (`proxy.py:488`), and
shown on the Overview and in `GET /admin/api/config/problems`. This mirrors the registry's existing `partial` mode
(`proxy.py:519-548`).

### 3.6 Atomic multi-entry edits

One change may hold many items across sections. One `BEGIN IMMEDIATE` transaction checks every section revision named
in `if_match`, validates the candidate snapshot, writes the rows, advances the section revisions, writes
`config_changes` and the audit row, and checks the browser session is live (decision D41,
`gateway/admin_store.py:4314-4316`, `session_guard.require_live`). After commit, the handler calls the reload (section 5).

- Every section that an item touches must be named in `if_match`. A missing one gives 428 `revision_required`.
- Validation runs inside the transaction, so the registry cannot change under a validator that reads it. The client
  does not send the registry generation for a config change.
- A revert compares each current row with the change's `after`. If a later change edited the same pointer, the revert
  gives 412 `changed_since` and names the pointers.
- If the reload after commit fails, the old snapshot keeps serving (`proxy.py:1164-1201`). The change stays committed.
  The effective view then shows the stored value, the running value and the problem `reload_failed` with the change
  id, until a reload succeeds. The view reads the running values from the published snapshot, never only from the
  database.

---

## 4. Layering and the effective view

### 4.1 Order

```
startup file + env  ──►  startup settings (rule 3; not layered, shown read-only)
release defaults    ──┐
owner overrides     ──┴─►  effective sections  ──►  registry overlay  ──►  derived values  ──►  snapshot
```

Release defaults < owner overrides. Nothing else writes a section. Env does not override a section key (F4).

### 4.2 Merge semantics

| Shape | Rule |
|---|---|
| Scalar entry | `set` replaces. |
| List | Always one atomic entry. `set` replaces the whole list. No append, no union. Ordered rule lists (the OpenAI glob list, first match wins, `gateway/stable_aliases.py:240-246`) must not merge: [RB] "rule lists replace by default, because accidental union can widen an allow list". |
| Object, no annotation | Not an entry; its properties are. |
| Object, `collection` | Each member is an entry: `set`, `delete` (tombstone), or `set` on a new id. |
| Object, `atomic` | `set` replaces it whole. There is no field merge for config sections in v1. |
| Anything else | An object is closed (`additionalProperties: false`) or a `collection` or `atomic`; the lint refuses an open one. Nothing below a scalar, a list or an atomic entry is an entry, so `/extra` and `/extra/x` can never both exist. |

A tombstone (`delete`) is for a collection member, or an optional property. A `delete` of a required property is refused
on write (`delete_required`), because it would publish a section that fails its schema.

Known Helm deep-merge traps and the answer to each:

1. **Null deletes a key.** In Helm, a `null` value removes the default key, so "set to null" cannot be written
   ([RB] S46). Here, entry removal is its own op (`delete`). `set` stores a literal `null`.
2. **A merge can produce an invalid combination.** The Helm page shows a merge that is invalid until the old key is
   removed ([RB] S46: "invalid-merge example"). Here, the default op replaces a whole entry, and every merged entry is
   validated against the schema before it is used.
3. **Lists replace wholesale, which surprises people.** Here, lists are declared atomic, and the effective view shows
   the release list next to the owner list.
4. **A type change across layers** (a map over a scalar). Here, the schema refuses it on write, and skips it on load.

### 4.3 The effective-source view

For each entry the view shows: pointer, effective value, source, release value, the override (op, who, when, note,
origin), apply mode (and "pending restart"), drift state, and problems. Sources:

| Source | Meaning |
|---|---|
| `release` | Inherited. No override row. |
| `owner` | An override replaces a release entry. |
| `owner-added` | An override on a pointer the release does not ship. |
| `owner-deleted` | A tombstone. The release entry is hidden. |
| `startup` / `env` | Startup settings only; read-only in the API. |

Immich's maintainer declined a per-key source view as "tricky"; LiteLLM and Sentry built one ([MASTER] point 3). It is
the hard part, so it is part of the core slice, not a later UI task.

### 4.4 Drift report

Each override row keeps `base_json`: the release value it was written over. At load, for each override, compare
`base_json` with the current release value:

| Drift kind | Condition | Shown |
|---|---|---|
| `release_changed` | The release value under an owner override changed. | Old default, new default, owner value. Actions: keep, reset to inherit. |
| `release_removed` | The release no longer ships an entry the owner overrode. | The override now acts as `owner-added`. |

A tombstone keeps `base_json` too, so a release that changes an entry the owner deleted shows as `release_changed`.
Values the owner inherits change silently with the release, as inheriting means; the release notes are where those
show. A report of inherited changes is deferred.

The report is information, not a prompt. The owner acts when they want. dpkg prompts when both sides change one file
([RB], "dpkg compares old/local/new files and prompts on dual changes"); there is no incidence data for how often
([RB] negative findings). Keeping `base_json` gives the old/local/new triple without a merge engine. Three-way merge
stays deferred ([RB]: "CEREMONY as an initial requirement").

---

## 5. Live reload and the snapshot contract

### 5.1 The snapshot

```python
@dataclass(frozen=True)
class RuntimeSnapshot:
    generation: tuple            # (registry generation, {section: revision}, digest of override rows)
    config: Mapping              # effective config, same top-level shape readers use today
    sources: Mapping             # {section: {pointer: source record}}
    problems: Mapping            # CONFIG_PROBLEMS and REGISTRY_APPLY_PROBLEM
    derived: Mapping             # every value a reader used to compute at import (class A, B, C)
```

- **Build.** `_load_effective_runtime_config` (`proxy.py:460`) becomes: release defaults ⊕ overrides → effective
  sections; then the registry overlay (`apply_service_registry_to_config`); then `fold_workflow_aliases`
  (`gateway/stable_aliases.py:401`); then derive. `derive(snapshot)` replaces `_derive_backend_constants`
  (`proxy.py:1095`) and grows by one group per slice.
- **Publish.** `_publish_runtime_config` (`proxy.py:1229-1262`) already swaps one reference under `_RELOAD_LOCK`
  (`proxy.py:1161`). It publishes the snapshot the same way. A failed build leaves the old snapshot serving
  (`proxy.py:1164-1201`).
- **Trigger.** A committed config change, a registry change (`_fire_registry_changed`, `gateway/admin_runtime.py:1191`)
  and an import call the reload in the same process. The gateway runs one instance (single-instance lock,
  `private-tmp/.owner.lock`). The CLI writes through the HTTP API, never to the database directly, so every write
  reaches the running process.

### 5.2 One snapshot per request

An ASGI middleware binds the current snapshot to a context variable at request start. `runtime()` returns the bound
snapshot, or the latest one outside a request. Work that outlives a request (media jobs, queued work) captures the
snapshot generation at submit. CODE_TO_DATA S2 makes this the execution envelope (`docs/design/CODE_TO_DATA.md`, S2).
Background loops read the latest snapshot once per iteration.

**The holder is the only source.** `runtime()` returns the bound snapshot or the holder's latest one. It never builds a
view of the module's `config` name. A reload publishes the snapshot first, then assigns `config` and the derived names
from it, so a request that starts mid-reload binds a published snapshot whole. A snapshot carries the registry
problem and the layer's problems itself, so a reader never depends on an attribute of the config. The one exception is the
hand-off from the loader to the publisher: `_load_effective_runtime_config` returns the effective config, and a
`LayeredConfig` (a `dict` subclass) carries the layer and the registry generation that built it to `_publish_runtime_config`,
which passes both to `ConfigHome.snapshot` explicitly. The loader keeps that return type (a plain config) because its
stand-ins in tests return a plain dict; a plain dict carries no layer, which is right when no section is registered.

**Rule for long-lived code.** The middleware binds once per connection, so a websocket or a long stream keeps its
snapshot for its life. A task created inside a request inherits the bound snapshot through context variables; that is
intended for a media job, which should see the config it was submitted under. Long-lived code must not read `runtime()`
from a context it inherited by accident: a loop or worker that is started lazily from a request, or that outlives
requests, would read the snapshot of the one request that started it, forever. Such code does one of two things.
(1) It starts from a clean context (`contextvars.Context()` / `copy_context()` taken at module start, or a thread started
at boot) and calls `runtime()` once per iteration, so it gets the latest snapshot. (2) It captures the snapshot it needs
at submit time and passes it as an argument (the execution envelope of CODE_TO_DATA S2). A stream or a websocket that
must follow a reload re-reads `runtime()` after it clears the binding, or the owner reconnects it.

Transition: 108 direct `config.get(...)`/`config[...]` reads in proxy.py (grep) read the latest published dict,
so one request can mix two snapshots if a reload lands mid-request. Today's code has the same property. Each section
slice moves its readers to `runtime()`. New code must use `runtime()`. The ratchet in C0 counts import-time reads; a
second count of direct `config` reads falls as slices land. Neither reaches zero in this program. Confidence that the
mixed-read window causes a visible fault: low (a reload is an owner action, seconds apart). Ungrounded.

### 5.3 Class R and S

- **R (restart).** Stored in admin.db like any section entry, marked `x-azizam-apply: restart`. The effective view
  shows the value that runs and the value that will run after a restart. The two job semaphores (`proxy.py:1003`,
  `:23034`) can later rebuild on publish (new requests use the new size, in-flight work holds the old one). Deferred.
- **S (startup).** Not sections. A closed `registry/startup.schema.json` lists them with their defaults. The effective
  view shows them read-only with source `startup` or `env`.

---

## 6. Export and import

### 6.1 Format

```yaml
format: azizam-config/v1
exported_at: 2026-11-05T12:00:00Z
gateway_version: <git describe>
release_digests: {model_aliases: sha256:..., roles: sha256:...}   # release defaults the overrides were read against
sections:
  model_aliases:
    - {pointer: /chat/stable/default, op: set, value: qwen3.8-flash-next-strata, base: ""}
    - {pointer: /chat/stable/default-fast, op: set, value: qwen3.8-flash-next-strata-fast}   # no base: owner-added
registry:
  records: {services: {...}, protocol_frontends: {...}, workflows: {...}, capabilities: {...}}  # owner data only
  tombstones: {protocol_frontends: [...]}
secrets:                     # references only; values never leave the store
  - {ref: sref_..., used_by: services/kobold/config/api_key}
```

- **Overrides only.** Small and diffable. [MASTER] point 4: export overrides, and keep the database backup as the true
  restore path. `?view=effective` returns the effective config for inspection; it is not importable.
- **Secrets.** Excluded. Records keep `sref_...` references (`gateway/secret_refs.py:1-9`). An import onto a database
  that lacks a referenced secret applies the entry and reports `needs_secret` on it.
- **Ephemeral state.** Excluded: health, last use, VRAM, swap state, change history (as
  `docs/CONFIG_SOURCE_OF_TRUTH.md` says of the current registry export).
- **Startup settings.** Excluded. They are host facts; the export names which startup keys differ from release
  defaults, without values.

### 6.2 Import

1. `POST /admin/api/config/import?dry_run=1`: parse, check the format version, validate every section against the
   receiving release, and return the effective diff per entry plus problems. Unknown sections are listed and skipped.
2. `POST /admin/api/config/import` with `If-Match` revisions from the dry run: one change of kind `import`, atomic,
   all or nothing. Mode `replace` (default) makes the owner layer equal to the file. Mode `merge` adds and replaces
   only the entries the file names.

### 6.3 Round-trip guarantee

On the same release: `import(export(A))` into an empty owner layer gives `effective(B) == effective(A)` by canonical
JSON digest, excluding secret values and ephemeral state. A test runs this on the live-like fixture (C2) and on every
section as it lands. Across releases: the overrides apply over the new defaults, and the drift report shows what moved.
Uptime Kuma's maintainer warns that a text export nobody tests rots ([MASTER] point 4); the round-trip test is the
answer.

### 6.4 How this replaces "edit proxy_config.yml"

A reproducible setup is: the release (a git ref) plus the export file plus the startup file plus the secrets. An
operator who wants review in Git commits the export file to their own repo and applies it with a dry run. This keeps
the goal of ENGINE_REGISTRY_DESIGN principle 4 (a setup that can be rebuilt from files,
`docs/design/ENGINE_REGISTRY_DESIGN.md:49`) and drops its means (the file as master, the split
`services.yml`/`services.local.yml` of §5.2). [MASTER] point 6: the Authentik and Caddy pages cited for principle 4
do not say the file is master. A per-key "pinned by file" mode (LiteLLM, Sentry `FLAG_PRIORITIZE_DISK`) is deferred
until an operator asks ([MASTER], "Operators who want GitOps").

---

## 7. API

Owner-only, under `/admin/api/config`. Auth: `_require_admin` plus the owner role, as the settings router does
(`gateway/settings_api.py:58-65`). The owner-capable admin key works for headless clients. Writes check the browser
session inside the transaction (decision D41). Preconditions: `If-Match: "<revision>"` on single-section routes; 428
`revision_required` when missing; 412 `revision_mismatch` on a mismatch (RFC 9110). This aligns with the registry
(`gateway/admin_api.py:2526-2541`). The settings routes' 409 (`gateway/admin_api.py:3183`) moves to 412 when C6 folds
them in.

| Route | Purpose |
|---|---|
| `GET /admin/api/config` | Sections: id, title, revision, override count, problem count, drift count, pending-restart count. Startup settings, read-only. |
| `GET /admin/api/config/sections/{section}` | Effective section and one row per entry (4.3). `ETag: "<revision>"`. |
| `PATCH /admin/api/config/changes` | Body `{items: [{section, pointer, op: set\|delete\|reset, value?}], if_match: {section: "N"}, note?}`. Every touched section must be in `if_match` (428 if not). `?dry_run=1` returns the effective diff and problems only. 200 `{change_id, revisions}`; 412; 422 `{problems: [{section, pointer, code, message}]}`. |
| `DELETE /admin/api/config/sections/{section}/entries?pointer=...` | Reset one entry to inherit (shortcut for one `reset` item). |
| `POST /admin/api/config/changes/{change_id}/revert` | Revert a change, as a new change. |
| `GET /admin/api/config/changes` | Change history, paged. |
| `GET /admin/api/config/drift` | Drift report (4.4). |
| `GET /admin/api/config/problems` | Load problems (3.5). |
| `GET /admin/api/config/export` | Export (6.1). Replaces the registry-only export at the same path (`docs/CONFIG_SOURCE_OF_TRUTH.md`, "read-only service-registry export"). |
| `POST /admin/api/config/import` | Import (6.2). |

`GET /admin/api/settings/effective` and `/effective/export` (`gateway/settings_api.py:383-401`) are removed in C10;
the sections routes replace them. Azizam is unreleased, so there are no redirects. The CLI (`python proxy.py config
get|set|reset|export|import|drift`) and an MCP tool call these routes; nothing has a second write path.

---

## 8. Migration

### 8.1 The generic one-time legacy import

When a section is registered for the first time on an install, at boot:

1. If the startup file has a block for that section, compute the overrides that make the effective section equal to
   that block, with the fewest rows: a differing entry → `set`; an entry only in the file → `set`; an entry only in the
   release defaults → `delete`. For a settings-shaped section (no collections), a key absent from the file means "code
   fallback", and the release default must equal that fallback (the slice moves the literal into the release file), so
   it gives no row.
   If any computed override is `invalid` against the section schema, the import does not run for that section. The
   step is not recorded, the file block stays in use as today, and the problem `legacy_import_blocked` names each bad
   pointer. Boot continues. The owner fixes the file or sets the value through the API, and the next boot retries.
2. Write them as one change of kind `legacy-import`, origin `legacy-import`, and record the step
   `config-homes-import:<section>` in `schema_migration_steps` in the same transaction (the pattern of
   `gateway/admin_store.py:4336`).
3. Write a report: one line per override, with the file value and the release value. Tombstones are a separate list
   headed "release entries this import removes". Show it in `GET .../changes`.
4. After the step is recorded, a block for that section in the startup file is ignored. Each boot logs one warning
   with the count of entries that differ from the effective section. The owner route
   `POST /admin/api/config/sections/{section}/legacy-import` (dry run first; C2 builds it, C3 uses it) imports the
   current file block again on purpose, as a new change that the dry run shows in full.
   This is the "refuse the losing write loudly" pattern ([MASTER] "Who should win": complaints cluster where the loser
   is silent).
5. Two importers write the same section rows. C6's fold (`gateway/config_fold.py`, step
   `config-homes-fold:<section>`) reads the settings documents that existed before the sections; this generic import
   reads the section's top-level startup-file block. They record different step names, so neither stops the other. The
   fold writes nothing when the section already has rows. The generic import writes the rows its plan names, so when
   both have data for a section, the startup-file block wins where the two differ. No release ships a startup-file block
   for a folded section today. The owner route resets owner rows that the file block does not name; the dry run lists
   each of those resets before anything is written.

Behaviour gate per section: the effective section after the import equals the legacy section before it, compared by
canonical digest (C0 goldens).

The section slices do **not** edit the tracked `proxy_config.yml`. So a `git pull` on a host with an uncommitted edit
does not conflict, and the edit is imported at the next boot. C10 deletes the blocks from the tracked file after the
live host has imported them.

### 8.2 The live Strata chat aliases (concrete)

State now: the live `proxy_config.yml` differs from the tracked file in `model_aliases.chat.stable` only: `default` and
`fast` point at Strata, and `default-fast` is new (live `git diff`, hunk at line 1238). Live runs main
(memory: live checkout runs main).

Steps, after C1, C2 and C3 are on main and through the gate:

1. Copy the live `proxy_config.yml` to a backup folder outside the checkout, as `live-proxy_config.pre-C3.yml`. Take an
   online copy of admin.db with `sqlite3 <db> ".backup <dest>"` into the same folder. Keep both until C10 is live.
2. Capture the before-golden on live: `GET /gateway/v1/aliases` and `GET /gateway/v1/aliases/resolve` for every
   stable name and every OpenAI pattern sample of every operation (`proxy_config.yml:1235-1236` names the routes).
   Save as JSON next to the backup.
3. `git -C ~/applications/azizam pull --ff-only`. C3 does not touch `proxy_config.yml`, so this does not conflict.
4. Restart with `scripts/safe-restart` (never a bare systemctl restart; wait for `--dry-run` to report idle first).
5. At boot, the `model_aliases` legacy import writes the overrides. With the neutral release default of C3, the
   rows include `/chat/stable/default`, `/chat/stable/fast`, `/chat/stable/default-fast` (the live edit) plus this
   deployment's other alias targets (for example `/chat/stable/coder`, `/chat/stable/rp`, `/images/stable/image`).
   Expect tens of rows, because the neutral release names no models. Before step 3, run the import as a dry run
   against a copy of the live file and a scratch database (the C3 test harness does this) and read the full row list.
6. Capture the after-golden the same way. It must equal the before-golden. Read the import report in
   `GET /admin/api/config/changes`; the three live-edit pointers must be in it.
7. Restore the tracked file: `git -C ~/applications/azizam checkout -- proxy_config.yml`. Check `git status` is clean
   for that file. The file block is now ignored (8.1 step 4); a safe-restart is not needed, but the next boot must
   still give the same golden.
8. Keep the backups until C10 is live (step 1).

Rollback, if step 6 fails or a fault shows later. The code before C3 never reads the new tables, so the new rows are
inert under it:

1. Copy the step-1 backup back to `~/applications/azizam/proxy_config.yml` (this restores the live edit).
2. `git -C ~/applications/azizam checkout <previous main SHA>`.
3. `scripts/safe-restart`. The gateway now reads aliases from the file again, as before.
4. To roll forward later: deploy main again. The step is already recorded, so the boot does not import again. If the
   file block changed while rolled back, the boot warning shows the count; call the legacy-import route (8.1 step 4)
   with a dry run, read it, then apply.

No database restore is needed for this rollback. The admin.db backup is for a fault in the new code that damages
other data.

### 8.3 Registry migration (C4)

One step, `config-homes-registry-layering`:

0. Take an online admin.db copy (`sqlite3 <db> ".backup <dest>"`) before the deploy. The code before C4 backfills on
   boot, so a rollback to it rebuilds the old copies by itself; the backup covers a fault in the new code.
1. For each stored record that has a release record with the same id: compute the field-level difference. Fields equal
   to the release are dropped. Store the rest as a patch. A record with no differing field becomes "inherit".
   (The 8 builtin frontends on live are expected to inherit; no owner edit path exists for them.)
2. Report each patch with its fields, and whether the audit log shows an owner edit of that record
   (`operator.service.updated` rows, target id). Patches with no owner edit are marked "probably a stale copy from an
   older release". They are kept, to preserve behaviour; the owner can reset them.
3. Records with no release record (the 8 `provisioned` from the old `backends` seed and the 15 made in the UI) are
   owner-added records and stay as they are. Dropping the seed removes no stored record.
4. Gate: the served route table, the frontend mount table, `/v1/models` and the alias table are equal before and
   after, on a copy of the live registry (values masked in test output).

### 8.4 Stored workflows beat bundled ones (C5)

`proxy.py:13246` skips a stored manifest whose id a bundled one has. C5 inverts it: a stored manifest with a bundled id
is an owner override of that entry, shown with source `owner` and drift against the bundled manifest. Saving under a
bundled id is a deliberate act: the API requires `"overrides": "<id>"` in the body, or it answers 409
`id_is_bundled` with the bundled manifest so a client can copy it first. Home Assistant separates copy, inherit and
take-ownership ([RB] "Home Assistant and Helm"). A collision of a workflow id with an alias or an OpenAPI name of a
*different* record stays a refusal at save, as CODE_TO_DATA S4 says. `GATEWAY_WORKFLOW_REGISTRY` (`proxy.py:13261`)
moves into the snapshot, so a stored workflow is live without a restart. The live database holds no stored workflow
today (read-only count), so the flip changes no live behaviour; C5 checks the count again before deploy.

### 8.5 Engine seed and backfill

Dropped in C4 (3.4 item 3). A fresh install already starts with `backends: {}` (`first-run.yml`). The 737-line
`backends` block of the tracked file is this deployment's engines, already in its database. C10 deletes it from the
tracked file and keeps two or three neutral engine examples as importable export files under `examples/config/`,
which an owner imports on purpose ([MASTER]: "ship engine examples as presets the owner adds on purpose").

---

## 9. Reconciliation with the other programs

| Program item | Overlap | Owner of what |
|---|---|---|
| CODE_TO_DATA S2 (execution envelope) | Snapshot per request (5.2). | CONFIG_HOMES supplies the snapshot and its generation; S2 pins it into the envelope. |
| CODE_TO_DATA S4 (live, versioned registry) | Live workflows, CAS, clash refusal. | CONFIG_HOMES C5 does the precedence flip and the snapshot rebuild only. S4 adds pinned execution revisions, inline graphs, update/disable/delete while queued, and the clash rules. S4 builds on C1, not beside it. |
| CODE_TO_DATA S5 (registry projections) | Hand-kept intent tables; `stable_aliases.OPERATIONS` (`gateway/stable_aliases.py:37-53`) is one. | S5 replaces the tables. C3 makes the alias schema accept any operation id that the capability catalog knows, so a new operation needs no edit to the alias section. |
| CODE_TO_DATA S8 (defaults and config) | "Literal default fallbacks leave with the slice that replaces their owner." | CONFIG_HOMES section slices move the literal fallbacks of config sections into release files. S8 keeps the per-family workflow config schema. |
| `videos.workflows`, `audio.workflows` | Workflow facts (graph file, size limits) that CODE_TO_DATA S3/S7 moves into manifests. | C8b moves them as-is into release files with an owner layer. When S3/S7 lands, they become workflow manifest fields and the sections retire. One home at each moment. |
| ENGINE_REGISTRY_DESIGN principle 4 and §5.2 | File as master; split file. | Superseded by this rule. C10 marks them so. Its goal (reproducible setup) is kept by export/import (6.4). Its Step 1 (hot reload) is delivered by C1 and C9. |
| `gateway/config_sources.py`, `docs/CONFIG_SOURCE_OF_TRUTH.md` | Hand-kept ownership table. | C10 generates the inventory from section schemas and the startup schema; the hand table and its pin test go. |

---

## 10. Slice plan

Rules for every slice: one worktree, one branch, through the gate in under ten minutes of affected tests, then merge
and deploy (memory: fast release flow). Goldens first where a reader moves. No slice edits the tracked
`proxy_config.yml` except C10.

| # | Slice | Depends on | Parallel with | Blast radius (expected) |
|---|---|---|---|---|
| C0 | Goldens and ratchets | none | none (small; lands first) | Tests only, plus the inventory script under `scripts/`. ~400 lines. |
| C1 | Core layer and snapshot | C0 | none | New `gateway/config_layers.py` (pure: schema annotations, entry index, merge, validate, sources, drift, export format) ~700 lines; `gateway/admin_store.py` tables and functions ~250; `proxy.py` load/reload/publish ~80; `registry/defaults/README.md`. |
| C2 | API, CLI, round trip (config sections only; the registry joins the export in C4) | C1 | C4, C7, C8 | New `gateway/config_api.py` ~450 (includes the legacy-import route); mount in proxy.py ~10; CLI ~150. |
| C3 | Aliases | C1, C2 | C4, C6, C7, C8 | `registry/defaults/model_aliases.{yml,schema.json}`; `gateway/stable_aliases.py` ~60 (unconfigured target, operation list); proxy.py `VIDEO_ALIASES`/`AUDIO_ALIASES` into derive ~20; `gateway/settings_api.py:350` `aliases_writable`. |
| C4 | Registry under the contract; registry part of export/import | C1 | C2, C3, C6, C7, C8 | `gateway/service_registry.py` −400/+250; `gateway/admin_runtime.py` ~60; registry GET views ~80; export ~60. Highest risk. |
| C5 | Stored workflows win; workflow registry live | C4 | C6, C7, C8, C9 | `proxy.py:13237-13261` ~60; workflow save route ~40. |
| C6 | Fold existing overrides (`instance`, `resource_admission`, `service_availability`) | C1, C2 | C3, C4, C7, C8 | `gateway/settings_api.py`, `gateway/admin_runtime.py:69-70,760-810`, `gateway/admin_api.py:2467-2500,3241-3260`, `proxy.py:17496-17582` ~250 net; the web client calls that send these settings (409 → 412). |
| C7 | Roles, models, llm, reasoning, model_routing (owner parts) | C1 | C3, C4, C6, C8 | Defaults and schemas; proxy.py readers of `ROLES` (`:3320`, `:13484`, `:25753`, `:26026`), `MODELS_CATALOG`, `PIN_LOADED_MODEL`, `SWITCH_TIMEOUT`, `UPSTREAM_READ_TIMEOUT` ~120. |
| C8a | Images and voices | C1 | C3, C4, C6, C7, C8b, C8c | proxy.py `:956-1007` and their readers ~150. |
| C8b | Videos and audio | C1 | others | proxy.py `:1015-1034`, `:25131-25138` and readers ~150. |
| C8c | model_compatibility and identity | C1 | others | proxy.py `:2705-2733`, `:15403` and readers ~100; `openai_model_aliases.generated.yml` to `registry/`. |
| C9 | Registry-derived constants into the snapshot | C1, C4 | C5, C6, C7, C8 | proxy.py class A and B (27 names) and route policies `:13226`, `:19840`, `:19846`, `:28695` ~200. |
| C10 | Startup file, shrink, inventory, docs | All section slices; live import done (8.2) | none | `proxy_config.yml` −1,600; delete `first-run.yml`; `gateway/bootstrap_config.py`; `gateway/config_sources.py` regenerated; remove non-startup env overrides; docs (`CONFIG_SOURCE_OF_TRUTH.md`, `FIRST_RUN_SETUP.md`, `CONFIG.md`, `ENGINE_REGISTRY_DESIGN.md` principle 4 marked superseded). |

Order and the aliases-first question. The handoff suggested aliases first, because aliases unblock the live edit.
Confirmed, with one change: the core (C1) and the API (C2) come before aliases, because aliases are the first user of
the generic layer, and building the layer inside the alias slice would bake alias shapes into it. C3 needs C2 for the
write path, the 422 refusal and the legacy-import route. C4 is next in risk, not in urgency; it runs in parallel.

Critical path: C0 → C1 → C2 → C3 → live import (8.2) → … → C10.

Parallel width:
- After C1: C2, C4, C7, C8a, C8b, C8c (six worktrees).
- After C2: add C3 and C6.
- After C4: add C5 and C9.

Builds run in parallel; merges go through the gate one at a time. C4, C5, C7, C8 and C9 all edit proxy.py, in
separate line ranges, so a rebase before each merge is expected to be clean. Each section slice owns its own ratchet
key (`import_time_config_reads.<section>` in `[ratchets]`, one line per section), so two slices never edit the same
ratchet line.

### 10.1 Acceptance tests per slice

**C0 Goldens and ratchets.**
- Effective-config digest for three inputs: the tracked `proxy_config.yml`, `first-run.yml`, and a live-like fixture
  (tracked file plus the Strata alias edit). Built through `_load_effective_runtime_config` on a temporary admin.db.
- Alias resolution table: every stable name and one sample per OpenAI pattern, for every operation, for the three
  inputs.
- `/v1/models` ids and role list for the three inputs.
- Ratchet `import_time_config_reads.<section>` in `[ratchets]` of `tests/budgets.toml`, one key per source section
  (sum 96), from the AST scan in a test (`tests/test_import_time_config_reads.py`, script under `scripts/`). Same
  rule as `domain_literals` (`tests/test_domain_literal_ratchet.py:182-190`): the count must equal the key.
- Startup-file readers: a test lists the modules that read the startup file directly today (`gateway/bootstrap_config.py`
  and any other module the scan finds, for example `gateway/service_registry.py` for the seed) and fails on a new one.
- A second count of `config.get(`/`config[` reads in `gateway/*.py` (grep finds about 400; most read a passed-in
  argument) is recorded for information only; it is not a gate, because a passed-in snapshot read is correct.

**C1 Core layer.**
- C0 goldens unchanged (no section registered yet).
- Pure tests on fixture sections: every merge rule of 4.2; tombstone survives a release that adds the entry; reset
  removes the row; owner-added entry; `set` of a literal `null`.
- Load path: an invalid override is rejected and reported; a fail-closed entry becomes absent and never takes the
  release value; an override that refers to a later override survives the greedy rebuild; a problem with no pointer
  still rejects only the bad override; the loop ends; a `delete` is never rejected; boot never raises for an override
  fault; the same input gives the same result twice.
- Reload failure: inject a snapshot build failure after a commit. The old snapshot serves, and the view shows
  `reload_failed` with the change id and both values.
- CAS: an item whose section is missing from `if_match` gives 428; a revert after a later edit of the same pointer
  gives 412 `changed_since`.
- Write path: CAS mismatch gives 412; a multi-section change is all or nothing (inject a failure after the first row);
  the audit row and the change row commit with the overrides or not at all.
- Drift: each kind in 4.4.
- Snapshot: a reload publishes one object; a request bound before a reload sees the old snapshot to its end.
- Positive control: replace the merge with Helm-style deep merge in a test double and prove the invalid-combination
  test fails.

**C2 API, CLI, round trip.**
- Each route: owner 200, admin non-owner 403 `owner_required`, no session 401, missing `If-Match` 428.
- `PATCH ?dry_run=1` writes nothing.
- Round trip: export the live-like fixture, import into an empty owner layer, same effective digest.
- Import with a missing secret reference: entry applied, `needs_secret` reported.
- The CLI uses HTTP only (a test runs it against a test server with the DB file read-only).

**C3 Aliases.**
- Fresh install (no file block): neutral defaults; a request for `default` answers `alias_not_configured`, not a
  crash.
- Tracked file: legacy import gives the C0 golden for the tracked input.
- Live-like fixture: the import report holds `/chat/stable/default`, `/chat/stable/fast`, `/chat/stable/default-fast`;
  the golden equals the live-like golden.
- A loop or an unknown engine in an override is refused on write (422) and skipped on load.
- An alias to a service that is gone is applied and shown as dangling, not reverted.
- An owner edit of `/chat/stable/default` takes effect on the next request with no restart.
- `VIDEO_ALIASES` and `AUDIO_ALIASES` follow an alias edit without restart.
- After the step is recorded, an edit to the file block is ignored and logged once.
- Rollback of 8.2: the previous main commit with the restored file serves the pre-import aliases from a migrated
  database (the new rows are inert); after rolling forward, the legacy-import route re-imports a changed file block.

**C4 Registry under the contract.**
- Migration on a copy of the live registry (structure only, values masked in test output): the gate of 8.3 step 4
  holds; the 8 builtin frontends inherit.
- A stored copy that differs from its release record in one field becomes a one-field patch; a later release change to
  a different field of that record reaches the install.
- A release change to a builtin frontend reaches a migrated install with no backfill.
- Deleting a builtin record writes a tombstone; it stays hidden after a release re-adds it.
- No code path reads `proxy_config.yml` `backends` (a source scan test).
- The LiteLLM patch keeps its name, enabled flag and note across a release change to its other fields.
- Rollback: the code before C4 boots on the migrated database and rebuilds its copies by backfill (run once in the
  test harness against the previous main commit).
- Registry generation `os-N` semantics are unchanged for existing UI clients (existing registry tests pass).

**C5 Stored workflows win.**
- Stored manifest with a bundled id serves instead of the bundled one, without a restart.
- Save under a bundled id without `overrides` gives 409 `id_is_bundled`.
- Reset to inherit brings the bundled manifest back.
- Before deploy: count stored workflows whose id equals a bundled id on live (read-only). If not zero, list them in the
  slice report, because the flip changes their behaviour.

**C6 Fold existing overrides.**
- Each existing route's responses are unchanged apart from the 409 → 412 move (goldens of current responses first).
- Current `operator_settings` values migrate to override rows once. The old keys stay, inert, until C10 deletes
  them, so a rollback to the previous commit still finds them.
- The web client calls for these settings send the new precondition and handle 412 (the slice changes them).

**C7, C8a, C8b, C8c Section slices.**
- Per section: legacy import gives the C0 golden; the release file holds no absolute host path and no id the release
  does not ship (neutrality lint, 10.2); each moved reader uses `runtime()`; the section's ratchet key falls by the
  number of constants moved; an owner edit takes effect live (or shows "pending restart" for class R).

**C9 Registry-derived constants.**
- After a registry edit and no restart: every class A and B value equals what a fresh boot computes (property test:
  reload result == boot result for random registry edits from the existing fixtures).
- Route policies follow a frontend change without restart.

**C10 Startup file and shrink.** See 10.2.

### 10.2 The "done" gate

All must hold:

1. The tracked `proxy_config.yml` keys are a subset of `registry/startup.schema.json`. A test fails on any other key.
   `first-run.yml` is gone.
2. Every owner-changeable section has `registry/defaults/<section>.yml` and `.schema.json`, and the release defaults
   validate.
3. Neutrality lint over `registry/defaults/`: no absolute host path (`/media/...`, `/home/<user>/...`, `/mnt/...`), no IP address, no
   service id or workflow id the release does not ship.
4. The ratchet keys `import_time_config_reads.*` sum to the class S count (16) plus any class R constant still read at
   import, each listed with its reason.
5. No production code reads a section key from the startup file or from an env var that is not a startup key
   (source scan).
6. Round trip passes for every section and for the registry.
7. Fresh third-party boot (empty data dir, the tracked startup file only): the gateway starts, `/health` answers, the
   effective view lists every section as `release`, and no request crashes on an unconfigured alias.
8. Live: the effective digest after C10 equals the digest captured before C3, except for entries changed on purpose
   by the owner since then (listed from `config_changes`).
9. Every top-level key the code reads from the effective config (today's 17 file sections plus the code-only ones
   such as `gateway`, `limits` and `mcp`) belongs to exactly one section schema or to the startup schema. The dead
   `routing` section is deleted. A test computes the list from the code and compares.
10. Only `gateway/bootstrap_config.py` reads the startup file (C0's reader test, at its final value).
11. Independent review passes each slice (a different model from the maker).

Progress is reported as "N of 13 slices" (C0 to C10, with C8 as three).

---

## 11. Red team

Method: Donella Meadows' leverage points, highest first (paradigm, goals, self-organization, rules, information flows),
then failure cases. Each finding has the design's answer.

### Paradigm

**F1. The paradigm moves from "the file is the config" to "the database is the config; files are release data or
startup".** Risk: operators who keep config in Git lose their workflow. Answer: the export file is a reviewable,
committable artifact with a dry-run import (6.4). A per-key file pin is deferred until an operator asks. [MASTER]
names this as the case that would change the answer ("If Azizam's real users are mostly operators who keep config in
Git and run several hosts"). The target users are households first, then teams and self-hosters (memory: target users).

**F2. The design could become a config framework instead of a setting home.** Signs: annotations grow, merge ops grow.
Answer: four annotations and three ops; three-way merge, per-key pins, presets subscriptions and storage unification
are all deferred with the condition that would bring each back.

### Goals

**F3. Goal displacement: "proxy_config.yml shrinks" can be met by moving this deployment's values into release
files.** A third party would then inherit `/media/windows` paths and this household's model choices. Answer: gate item
3 (neutrality lint) and gate item 7 (fresh third-party boot). The legacy import (8.1) is the only path for this
deployment's values.

### Second sources of truth (rules)

**F4. Env overrides of non-startup keys are second writable masters** (1.2, last paragraph). Answer: C10 removes them.
An install that sets one gets a boot warning that names the key and the API call that sets it. Azizam is unreleased,
so no compatibility path (memory: Azizam unreleased).

**F5. The startup file keeps legacy section blocks until C10.** After a section's import they are ignored. That is a
file that looks live and is not, which [MASTER] calls the worst shape. Answer: it is bounded in time (C10 deletes the
blocks), loud (one warning per boot with the count of differing entries), and the effective view shows the database
as the source. Residual risk: an operator edits the block between C3 and C10 and misses the warning. Low, because the
only host with edits is this one.

**F6. OIDC provider config has a file fallback and a DB value; the file loses silently once the DB has a value**
(`gateway/config_sources.py:150-163`). Answer: C10 shows source `startup` vs `admin.db` for it in the view and logs
when a file block is shadowed. Moving the fallback out entirely is a login-boot decision (rule 3) and stays as is.

**F7. Stored copies of release records in the registry are a second copy of release data.** Answer: C4 removes equal
copies and reports the rest (8.3).

**F8. Workflow facts live in two kinds of release data (`videos.workflows`/`audio.workflows` and workflow
manifests).** Answer: at each moment one entry has one home (section 9 row). CODE_TO_DATA S3/S7 retires the sections.

**F9. `gateway/config_sources.py` CONFIG_SPECS is a hand-kept copy of ownership facts.** Answer: generated from
schemas in C10.

### Self-organization (can the system grow without code?)

**F10. Does any section still need code to add a new kind of setting?** Storage, API, export, structural validation,
the effective view and drift need none (3.1). Two things still need code: the reader of the setting (by nature), and a
semantic validator for cross-entry rules (optional, reviewed). That is the line CODE_TO_DATA draws: data selects
reviewed primitives, it does not redefine them (`docs/design/CODE_TO_DATA.md`, "The rule").

**F11. A new engine category tomorrow (a smell model).** What it needs from this layer: (a) its engines are registry
records (owner data, no code); (b) its alias operation must be accepted by the alias section: today
`stable_aliases.OPERATIONS` is a code tuple (`gateway/stable_aliases.py:37-53`) and `ENGINE_OPERATIONS` another
(`:54`). C3 opens the alias schema to any operation id the capability catalog knows; CODE_TO_DATA S5 makes that catalog
data. Until S5, a new operation id still needs a reviewed one-line catalog entry. (c) If the category needs its own
settings, it ships a section file pair, no code for storage. (d) Its readers are its adapter's business (ENGINE_CONTRACT).
So this layer adds no blocker; the remaining blocker is S5's intent tables, already planned.

**F12. Can a release add an entry the owner never asked for?** Yes, by design: inheriting means new defaults arrive.
Does that widen access? An alias only gives a new name to a target; who may call a model is decided by plans and
categories, not by alias presence (an "all, including future" plan admits new items on purpose). New protocol
frontends come with new code in the same release. So a release-added entry does not grant access by itself. Residual
risk, ungrounded: a future section where entry presence is a grant. Such a section must mark its collection
`x-azizam-fail: closed` and ship additions disabled. [RB] warns that restrict-only and grant-never rules depend on local
policy, not on the word.

### Information flows

**F13. Can the owner end up with a setup they cannot see?** Sources today that are invisible: code literal fallbacks
(for example `proxy.py:807`, `:25132`). Answer: each section slice moves its literals into the release file, so the
effective view shows them; the neutrality lint and the reviewer check for `.get("key", <literal>)` on a section key.
Residual: literals in modules outside proxy.py that read sections. C0 records modules that read the startup file;
done-gate items 9 and 10 cover `gateway/`.

**F14. Can the owner end up with a setup they cannot reproduce?** The export carries no secret values and no host
facts (paths in engine start commands, startup values). Answer: the export says which secrets and which startup keys
it depends on (6.1); the database backup stays the full restore path ([MASTER] point 4). This is stated in the export
file header, not hidden.

**F15. Drift fatigue.** A report that always has items stops being read. Answer: the report holds only
`release_changed` and `release_removed`, both about entries the owner chose to override. No prompt blocks anything.

### Failure cases

**F16. Skipping a bad override can widen access** (an owner restriction becomes the looser release default).
[RB] "never silently widen". Answer: `x-azizam-fail: closed` for policy and security entries makes the entry
unconfigured instead (3.5 step 5); a release that changes a section schema ships a migration step that transforms
overrides and keeps the old snapshot until the new one validates ([RB] B: "schema migration should produce a
reviewable transformation").

**F17. A deleted service silently moves an owner's alias.** Answer: references that dangle are warnings, not skips
(3.5).

**F18. A reload mid-request mixes snapshots.** Answer: one snapshot per request for moved readers; the global `config`
reads remain until their slice (5.2). Stated as residual risk.

**F19. Concurrent owner edits from two tabs or an agent.** Answer: per-section revisions and 412; a batch names every
section it touches.

**F20. A partial write before a crash.** Answer: one transaction per change; the reload reads committed state only.

**F21. The one-time import runs twice (two boots at once).** Answer: the step row and the overrides commit in one
`BEGIN IMMEDIATE` transaction; the second boot sees the step and skips, as the registry seed does
(`gateway/service_registry.py:80-133`, CAS retry with a recorded step).

**F22. A third party who installs fresh.** Gets neutral defaults, no legacy import, and the full effective view. Gate
item 7 tests it. They never see this deployment's values.

---

## 12. Open questions for the owner

1. **Per-key "pinned by file" mode.** This design defers it until an operator asks ([MASTER]). Confirm that no
   current use needs config held in Git with the UI locked for those keys.

Nothing else needs the owner. The neutral alias default (names with no target, answering `alias_not_configured`), the
owner-only read of the effective view, and the removal of non-startup env overrides are design decisions taken here
with their reasons.

## 13. Confidence

- Store, layering, tombstones, reset, effective view, export: moderate to high. Grounded in [MASTER] and [RB]; both
  rest on documented product behaviour, not measured outcomes.
- Load-path failure algorithm and fail-closed annotation: moderate. Ungrounded in prior art beyond [RB]'s "never
  silently widen"; it needs the C1 tests to prove termination and determinism.
- Snapshot per request: high for the mechanism; low for how fast the global `config` reads retire.
- Slice sizes and blast radius: rough. proxy.py has 32,143 lines; line counts are estimates from the inventory.
- Inventory: the AST scan cannot see config reads inside functions called at import; three such constants were added
  by hand, and there may be more. The scan is in appendix A so C0 can commit it.

---

## Appendix A. The inventory scan

Run as `python3 scan.py proxy.py rows.json`. It prints the count of module-level statements in the closure (159 on
810b57a25) and writes one row per statement with its targets and the sections it reads. C0 commits it under `scripts/`
and builds the ratchet on it.

```python
import ast, sys, json, collections
src = open(sys.argv[1]).read()
tree = ast.parse(src)
ROOTS = {"config", "auth_cfg", "admin_cfg", "_bootstrap_config"}
derived = set(ROOTS)
rows = []
def module_stmts(body):
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # decorators and default args run at import, but skip
            continue
        if isinstance(st, (ast.If, ast.Try, ast.With)):
            yield st, True
            for attr in ("body", "orelse", "finalbody"):
                yield from module_stmts(getattr(st, attr, []) or [])
            for h in getattr(st, "handlers", []) or []:
                yield from module_stmts(h.body)
            continue
        yield st, False
def names_loaded(node):
    out=set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
    return out
def sections(node):
    secs=set()
    for n in ast.walk(node):
        # X.get("sec") or X["sec"] where X is a root/derived name
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr=="get" and isinstance(n.func.value, ast.Name) and n.func.value.id in derived and n.args and isinstance(n.args[0], ast.Constant):
            secs.add(f"{n.func.value.id}.{n.args[0].value}")
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in derived and isinstance(n.slice, ast.Constant):
            secs.add(f"{n.value.id}[{n.slice.value}]")
    return secs
for st, is_header in module_stmts(tree.body):
    node = st.test if (is_header and isinstance(st, ast.If)) else (None if is_header else st)
    if node is None: continue
    loads = names_loaded(node)
    hit = loads & derived
    if not hit: continue
    targets=[]
    if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        tg = st.targets if isinstance(st, ast.Assign) else [st.target]
        for t in tg:
            for n in ast.walk(t):
                if isinstance(n, ast.Name): targets.append(n.id)
    for t in targets: derived.add(t)
    rows.append({"line": st.lineno, "targets": targets, "reads": sorted(hit), "sections": sorted(sections(node)), "src": ast.get_source_segment(src, node)[:160].replace("\n"," ")})
json.dump(rows, open(sys.argv[2],"w"), indent=1)
print(len(rows))
```
