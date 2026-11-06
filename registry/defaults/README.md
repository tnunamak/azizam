# Release defaults for config sections

This directory holds the release defaults of the config sections (design: `docs/design/CONFIG_HOMES.md`, section 3.1).
Every file here is read-only data that ships with a release. An upgrade replaces it. The owner never edits it: an
owner's change is a sparse override in `admin.db` (tables `config_overrides` and `config_changes`).

Fourteen sections ship. Three are the owner settings that existed before the layer, one holds the model aliases, and
ten hold the settings the startup file held:

| Section | Holds | Routes that write it |
|---|---|---|
| `instance` | the instance name, public base URL and default theme | `PUT /admin/api/settings/instance` |
| `resource_admission` | the media job admission limits (`media_jobs`) | `PUT /admin/api/operator/media-job-admission` |
| `service_availability` | per-service availability overrides, keyed by service id (no release entries) | `PUT /admin/api/operator/service-availability` |
| `model_aliases` | the stable names, engines and voice aliases of each operation | the config API (`/admin/api/config`) |
| `llm` | whether the loaded model is pinned, and the swap drain of a model switch | the config API |
| `model_routing` | the model switch and upstream read timeouts | the config API |
| `reasoning` | the reasoning settings (no reader yet: an edit is stored and changes nothing) | the config API |
| `roles` | the model roles (no roles in the release) | the config API |
| `model_compatibility` | OpenAI model-name recognition and the default model of each operation (no model id in the release) | the config API |
| `identity` | the face sidecar of the likeness check and how long the identity scene waits (no face service in the release) | the config API |
| `images` | the signed URL lifetime, VRAM and batch limits, background removal, variations and edit workflows, and the media job limits (`concurrency` and `resume_on_start` apply at restart) | the config API |
| `voices` | the custom voice limits, the sample rules, and the quality and design engines (the release names none) | the config API |
| `videos` | the video workflows (graph file and limits), the default, remix and extend workflows, the length caps, the older exact workflow names | the config API |
| `audio` | the audio workflows (graph file and length limits), the default workflow, the older exact workflow names | the config API |

Each settings route keeps its request and response shape. A write is a revision-checked change of the section: a missing
`If-Match` is 428 and a stale one is 412. On first boot after the upgrade, the values in the old `operator_settings`
keys (`instance_presentation`, `media_job_admission_overrides`, `service_availability_overrides`) become override rows
once. The old keys stay in the database, unread, until a later release deletes them, so a rollback still finds them.

The `model_aliases` release defaults are neutral: they name stable aliases with no target and name no model, engine or
service. A name with no target answers `alias_not_configured` until the owner sets it. The semantic rule for the section
is in `gateway/stable_aliases.py` (`alias_problems`) and is registered in `gateway/config_rules.py`.

A section that an older release read from the startup file is imported once at the first boot (design 8.1, step 1;
`gateway/config_legacy_import.py`). To read what the import would write for a copy of a startup file, without touching a
live database: `python -m gateway.config_legacy_import --config <copy of the file>`. A schema may list, at its root, the
keys of the old block that the section does not own (`x-azizam-startup-only`); the import leaves them in the startup
file. `model_routing` lists `gguf_dir`, a storage root a later slice moves. The audio workflow directory is a storage root and also stays in the startup file (`audio` lists `workflows_dir` the same way); `images` lists `cache_dir`,
`vram_estimates_file` and `signed_url_secret`; `voices` lists `dir`. `model_compatibility` lists `generated_aliases_file`: the generated OpenAI model list is the release file `registry/openai_model_aliases.generated.yml`, so its path is not a setting. `models` is not a section yet: its startup
block is a list, and a section root must be a mapping.

## A section is two files

```
registry/defaults/<section>.yml           release defaults
registry/defaults/<section>.schema.json   closed JSON Schema, Draft 2020-12, with x-azizam-* annotations
```

The section id is the file stem. It uses lower-case words joined by `_`. Both files must exist. If one does not, or if
the defaults break the schema, or if an annotation is wrong, the gateway stops at start with the list of broken rules
(`gateway/config_runtime.py`, `load_sections`). A release that ships broken defaults does not boot with a guess.

Adding a section needs no code for storage, export, validation of structure, the effective-source view or the drift
report. Code is still needed for whatever reads the setting, and for an optional semantic rule.

## Schema annotations

| Annotation | Values | Meaning |
|---|---|---|
| `x-azizam-entry` | `collection` | Each member of this object is one entry. The owner may add members only if the schema allows them (`additionalProperties` or `patternProperties`). |
| | `atomic` | This node is one entry. Its inner structure is replaced whole. Lists are always atomic. |
| (none) | | An object recurses into its properties. A scalar or a list is an entry. |
| `x-azizam-apply` | `live` (default), `restart` | When a change takes effect. A reload keeps the value that runs for a `restart` entry; the effective view shows the stored value as pending until the next start. |
| `x-azizam-fail` | `inherit` (default), `closed` | What an invalid override does. `inherit` keeps the release value. `closed` leaves the entry absent. A `closed` entry must be optional in the schema, so that its absence is valid. |
| `x-azizam-secret` | `true` | The entry holds only a secret reference (`sref_...`). A plain value is refused. |
| `x-azizam-startup-only` | list of key names, root only | Keys of the old startup-file block that the section does not own. The one-time import leaves them in the startup file instead of refusing the block. |

`apply`, `fail` and `secret` are inherited from the nearest annotated ancestor.

Entries never nest. An object is closed (`additionalProperties: false`), or it is a `collection`, or it is `atomic`; the
lint refuses any other object. A `delete` (tombstone) is allowed only on a collection member or an optional property.

## Rules for the files

- No absolute host path and no id that the release does not ship.
- The unit of change is the entry. `set` replaces an entry whole; there is no deep merge, and a list is replaced, never
  appended to.
- A value in `<section>.yml` is a default, not a secret. A secret lives behind a secret reference.
- Change a default in a release on purpose: owners who overrode that entry see a drift report, not a silent change.
