# Data retention

Owner principle P19: keep valuable data; deletion is the owner's choice.
Jobs, job events, video records, generated media and saved conversations have
no automatic age limit. Prompt-memory privacy choices still apply. Existing
receipt expiry remains an explicit owner setting with a deletion preview; see
[Receipt retention](RECEIPT_RETENTION.md).

This inventory covers `gateway/`, `proxy.py`, and the imported root stores
`responses_store.py` and `metrics_store.py`. The audit searched `DELETE FROM`,
`DROP TABLE`, `unlink`, `prune`, `retain`, `TTL`/`ttl`, `shutil.move`, `os.remove`
and `rmtree`, then traced the callers. Rows group paths with the same purpose;
counts describe inventory rows, not individual SQL statements.

Classes: **(a)** valuable owner/member data; **(b)** disposable operational
state, temporary copies or expired credentials; **(c)** value or policy unclear.
Explicit deletion of class (a) data remains possible through existing authorized
actions. This change adds no bulk cleanup command or hidden disk-pressure expiry.

| Store | What is deleted or replaced | When | Class | Decision |
|---|---|---|---|---|
| Media jobs database (`media_jobs.init_db`) | Previously moved the only database on any SQLite error and could unlink it if moving failed | Startup | (a) | Fixed. Retry non-corruption errors three times with SQLite busy waits, then raise. Quarantine only failed integrity checks or corruption errors; preserve the database and journals. |
| Receipts, admin and Responses databases (`receipt_store`, `admin_store`, `responses_store`) | Same unsafe startup recovery | Startup | (a) | Fixed with the shared corruption-only check. No delete fallback. Admin state remains private. |
| Metrics database (`metrics_store.init_db`, `verify_integrity`) | Previously unlinked the database on a failed move and always removed WAL/SHM | Startup after corruption or a failure marker | (a) | Fixed. Preserve database and journals; recheck a previous failure marker before quarantine. Keep the normal startup header/schema check inexpensive. |
| Jobs and events (`media_jobs`) | Jobs and their append-only events older than 14 days, including queued/running jobs | Every gateway startup | (a) | Removed the pruning API and startup call. Keep all states and events. |
| Video records (`video_job_records`, proxy hydration and GC) | Ownership, extension lineage and job metadata after 14 days | Startup and runtime GC | (a) | Removed both age-based deletion paths and the age-filtered load option. Explicit DELETE still removes its tombstone after five minutes. |
| Receipt schema upgrade (`receipt_store._exec_schema`) | All receipts without the newer owning-App column | First opening of an older schema | (a) | Replace table recreation with additive columns. Preserve original rows and JSON; NULL ownership excludes them from member feeds. |
| Receipts / Usage / request cost history (`receipt_store.maintain`) | Rows before the owner's selected cutoff | Startup and hourly, only after an explicit finite setting | (a) | Keep existing forever default and preview-checked owner setting. History floor and cost coverage rules remain; no new expiry. |
| Library assets and ownership sidecars (`proxy` media storage) | Image, video and audio bytes, metadata and prompts after a default 24 hours | Background cache eviction | (a) | Removed eviction, its settings and expiry UI. These files can be the only copy of generated output. Signed-link expiry still limits a link, not the stored asset. Existing authorized Library DELETE remains. |
| Saved prompts inferred to be orphaned (`prompt_memory.prune`) | Video placeholders after 14 days; prompts missing from a supplied asset inventory | Prompt maintenance | (a) | Removed both inferred deletions. A partial inventory, absent disk or old video does not authorize erasure. Explicit privacy deadlines still apply. |
| Saved Responses conversations (`responses_store`, proxy lifespan) | Stored request/response chains after seven days | Hourly background pruning | (a) | Removed the age-based API and maintenance task. Existing authenticated DELETE and privacy erasure remain. Save/forget admission rules are unchanged. |
| Prompt-memory privacy (`prompt_memory`, `me_prompt_memory`, proxy purge queue, account removal) | Prompt text/workflow, saved chats, prompt derivatives and embedded prompt copies | Member/owner privacy deadline; explicit forget, delete, memory-off, kill switch or permanent member removal; retry of unfinished erasure | (a) | Retain deliberate privacy behavior and its default of no expiry. Account disable alone preserves content. Video failure/cancel removes its private pending prompt; media output is retained. Prompt moves transfer metadata before removing the source row. |
| Custom voices and consent recordings (`custom_voices`) | Voice/consent files, explicitly selected owner folders | Explicit delete/purge only | (a) | No automatic expiry found. Keep quotas that refuse a new upload rather than evict existing voices. `purge_orphans` remains an explicit helper, never a maintenance task. |
| Admin accounts, profiles, services, transforms, provider configuration and secrets (`admin_store`, `admin_runtime`, `secret_store`, operator APIs) | Selected records or credentials | Explicit delete, replacement, reset or revoke; no time-based cleanup | (a) | Keep existing actions. Apps/accounts/credentials generally use disable/revoke records; audit history has no automatic expiry. Profile deletion and secret replacement are deliberate mutations. |
| Provider spend ledger (`providers`) | No automatic row deletion; expired reservations change state | Crash recovery / reconciliation | (a) | Keep ledger rows, unknown charges and settlement history. Releasing capacity does not delete the record. |
| Category policy snapshots and audit (`category_store`) | No automatic deletion; SQL triggers reject deletes | Policy publication and review | (a) | Preserve append-only history. |
| Hardware and backend telemetry (`metrics_store.prune_old`, `metrics_sampler`, `metrics_writer`) | Raw samples after 7 days, minute aggregates after 30 days, hourly aggregates after 365 days | Daily maintenance | (c) | Existing rolling telemetry policy left unchanged. These are resource/health measurements, separate from receipt Usage and spend; whether the owner values indefinite telemetry is UNVERIFIED. Database recovery is protected regardless. |
| Published media metadata migration (`prompt_metadata_migration`, proxy startup) | Embedded prompt/workflow/EXIF/XMP copies in pre-upgrade media | Automatic startup privacy migration replaces each file with stripped bytes | (c) | UNVERIFIED policy conflict: this prevents cross-member disclosure, but an original may contain the only workflow/prompt copy. No archive/import is implemented. Existing privacy migration remains unchanged; explicit erasure must still remove these copies. |
| Pre-upgrade video prompt fields (`video_job_records.load_all`) | Inline `record_json.prompt` | Automatic read-time privacy migration | (c) | UNVERIFIED policy conflict: unscoped prompt fields are removed without a maker-scoped transfer. This existing privacy behavior remains unchanged. |
| Sessions, session revocation fences and login throttles (`accounts`) | Expired sessions/fences and obsolete rate-limit buckets | Login/session operations; password reset, disable, removal or logout | (b) | Keep security expiry and explicit invalidation. No saved content is stored in these rows. |
| Derived identity claims and access caches (`admin_store`, `accounts`) | Cached claims or superseded claim-derived grants | Claim refresh, schema conversion, explicit profile change/removal | (b) | Keep refresh/invalidation. Schema conversion freezes effective grants before removing ambiguous cached claims; it does not delete users or audit records. |
| Video progress, workflow execution maps and DELETE tombstones (proxy GC) | Transient progress maps and records already explicitly deleted | Progress TTL or terminal grace period; five minutes after explicit DELETE | (b) | Keep. These caches do not determine durable job retention. |
| Execution payload spools (proxy media jobs) | Temporary request payloads | Job terminal; explicit privacy erasure | (b) | Keep finished-job cleanup; preserve absent-job spools because their history may be quarantined. Removed age-based deletion of queued/running payloads, including in-memory payloads. Active private work can still finish during an explicit erasure using memory-only payloads. |
| Speech/transcription processing files (`stt_chunking`, proxy audio routes) | Input spools, normalized WAV copies, segment files and workspaces | Processing completion/failure or controlled cleanup | (b) | Keep temporary-copy cleanup. These are execution workspaces, not Library output. |
| Asset write scratch files and failed voice staging (`proxy.store_asset_bytes`, `custom_voices._commit`) | Incomplete or already-published temporary files | Write failure or successful atomic publication | (b) | Keep. Published media/voice records are separate. |
| Prompt metadata migration scratch (`prompt_metadata_migration`) | Recognizable old helper files, stripped scratch copies and abandoned staging files older than one hour | Privacy migration/cleanup pass | (b) | Keep scratch cleanup. Published media bytes remain after metadata stripping; the existing privacy migration removes embedded prompt copies to prevent cross-member disclosure. |
| Privacy purge coordination (`prompt_memory_erasure_intents`, proxy `prompt_purge_queue`) | Completed retry intents | Successful erasure/metadata stripping | (b) | Keep. Durable cutoffs remain to prevent late writes from restoring erased prompts. |
| Extension package source cache (`extension_packages`) | Temporary fetched clone | Source refresh or cleanup | (b) | Keep replaceable clone cleanup; installed package/configuration deletion remains an explicit owner action. |
| Atomic config/signing writes and lifecycle overrides (`local_owner`, `lifecycle_drivers`) | Temporary replacement files; runtime-generated service drop-in | Atomic write completion/failure or explicit lifecycle change | (b) | Keep. These are derived temporary copies and runtime overrides. |
| In-memory capability, status, model, signing-key, cancellation and drain state | Cache entries, expired tombstones or drain flags | TTL, refresh or resource release | (b) | Keep. No durable owner/member records are removed. Live cancellation entries retain their holders. |
| Firehose activity log (`firehose_metrics.compact_log`) | Recent activity projection before the 24-hour display window | Periodic log compaction | (b) | Keep this bounded diagnostic projection; durable request/usage facts remain in receipts. Replay age filters only affect the in-memory view. |
| Database maintenance markers (`sqlite_startup`, `metrics_store`) | Completed quarantine marker or rechecked integrity marker | Successful recovery/check | (b) | Keep marker cleanup only. A partial quarantine leaves a marker that blocks the next startup; it never falls back to deleting database or journal files. |

Inventory totals: **15 class (a), 13 class (b), 3 class (c)**.

A quarantine uses a new private `<database>.broken-<time>-<unique-id>/`
directory on the same filesystem. If any rename fails, startup raises and the
`<database>.quarantine-pending` marker remains. The original files may be split
between the original location and that directory. Stop users of the store,
inspect both locations, and restore or finish the move before removing the
marker. No automatic attempt deletes the files to make room for a fresh store.

Verification uses temporary stores and an isolated checkout. No live databases
were inspected or changed. This inventory covers gateway-owned retention;
external engine history, filesystem cleanup jobs and backups are UNVERIFIED.

One behavioral follow-up remains UNVERIFIED: seeded image requests use a stable
request ID for deduplication. Retaining history indefinitely also retains that
mapping, so repeating a request after explicitly deleting its output, or after
a terminal failure, can return the old job. No change to deduplication policy is
included here. A future fix must separate reusable-result lifetime from history.
