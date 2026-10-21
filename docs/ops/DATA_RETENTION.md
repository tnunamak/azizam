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

| Store | What was deleted or replaced before this audit | When | Class | Decision |
|---|---|---|---|---|
| Media jobs database (`media_jobs.init_db`) | Previously moved the only database on any SQLite error and could unlink it if moving failed | Startup | (a) | Fixed. Retry non-corruption errors three times with SQLite busy waits, then raise. Quarantine only failed integrity checks or corruption errors; preserve the database and journals. |
| Receipts, admin and Responses databases (`receipt_store`, `admin_store`, `responses_store`) | Same unsafe startup recovery | Startup | (a) | Fixed with the shared corruption-only check. No delete fallback. Admin state remains private. |
| Metrics database (`metrics_store.init_db`, `verify_integrity`) | Previously unlinked the database on a failed move and always removed WAL/SHM | Startup after corruption or a failure marker | (a) | Fixed. Preserve database and journals; recheck a previous failure marker before quarantine. Keep the normal startup header/schema check inexpensive. |
| Jobs and events (`media_jobs`) | Jobs and their append-only events older than 14 days, including queued/running jobs | Every gateway startup | (a) | Removed the pruning API and startup call. Keep all states and events. Job writes update by job ID; a request ID held by another job fails the write instead of deleting that job, and a stale copy cannot restore a released ID. The deletion also ended request-ID replay; `REPLAY_WINDOW_S` keeps that 14-day replay limit. A finished job older than that gives its request ID to the new job, so a repeated seeded or idempotent request runs again. Active jobs always replay. The released ID is cleared, not copied: a seeded ID is a prompt derivative that erasure must be able to remove. Jobs keep the creating request's receipt ID in `parent_request_id`; Library and Usage fall back to it when the job's own request ID is cleared or derived. |
| Video records (`video_job_records`, proxy hydration and GC) | Ownership, extension lineage and job metadata after 14 days | Startup and runtime GC | (a) | Removed both age-based deletion paths and the age-filtered load option. Explicit DELETE still removes its tombstone after five minutes. |
| Receipt schema upgrade (`receipt_store._exec_schema`) | All receipts without the newer owning-App column | First opening of an older schema | (a) | Preserve original rows and JSON with additive columns. A missing App ID alone does not make a receipt invisible. Library and Usage may still admit it through a trusted owner-key or principal match. Rows without any trusted ownership match stay hidden from scoped users. |
| Receipts / Usage / request cost history (`receipt_store.maintain`) | Rows before the owner's selected cutoff | Startup and hourly, only after an explicit finite setting | (a) | Keep existing forever default and preview-checked owner setting. History floor and cost coverage rules remain; no new expiry. |
| Library assets and ownership sidecars (`proxy` media storage) | Image, video and audio bytes, metadata and prompts after a default 24 hours | Background cache eviction | (a) | Removed eviction, its settings and expiry UI. These files can be the only copy of generated output. Signed-link expiry still limits a link, not the stored asset. Existing authorized Library DELETE remains. |
| Saved prompts inferred to be orphaned (`prompt_memory.prune`) | Video placeholders after 14 days; prompts missing from a supplied asset inventory | Prompt maintenance | (a) | Removed both inferred deletions. A partial inventory, absent disk or old video does not authorize erasure. Explicit privacy deadlines still apply. |
| Saved Responses conversations (`responses_store`, proxy lifespan) | Stored request/response chains after seven days | Hourly background pruning | (a) | Removed the age-based API and maintenance task. Existing authenticated DELETE and privacy erasure remain. Save/forget admission rules are unchanged. |
| Prompt-memory privacy (`prompt_memory`, `me_prompt_memory`, proxy purge queue, account removal) | Prompt text/workflow, saved chats, prompt derivatives and embedded prompt copies | Member/owner privacy deadline; explicit forget, delete, memory-off, kill switch or permanent member removal; retry of unfinished erasure | (a) | Retain deliberate privacy behavior and its default of no expiry. Account disable alone preserves content. Video failure/cancel removes its private pending prompt; media output is retained. Prompt moves transfer metadata before removing the source row. A move never replaces a different prompt already saved for the destination (both rows stay) and keeps the stricter of two deadlines. |
| Custom voices and consent recordings (`custom_voices`) | Voice/consent files, explicitly selected owner folders | Explicit delete/purge only | (a) | No automatic expiry found. Keep quotas that refuse a new upload rather than evict existing voices. `purge_orphans` remains an explicit helper, never a maintenance task. |
| Admin accounts, profiles, services, transforms, provider configuration and secrets (`admin_store`, `admin_runtime`, `secret_store`, operator APIs) | Selected records or credentials | Explicit delete, replacement, reset or revoke; no time-based cleanup | (a) | Keep existing actions. Apps/accounts/credentials generally use disable/revoke records; audit history has no automatic expiry. Profile deletion and secret replacement are deliberate mutations. |
| Provider spend ledger (`providers`) | No automatic row deletion; expired reservations change state | Crash recovery / reconciliation | (a) | Keep ledger rows, unknown charges and settlement history. Releasing capacity does not delete the record. |
| Category policy snapshots and audit (`category_store`) | No automatic deletion; SQL triggers reject deletes | Policy publication and review | (a) | Preserve append-only history. |
| Hardware and backend telemetry rollups (`metrics_store.prune_old`) | Hourly aggregates after 365 days | Daily maintenance | (a) | Fixed. Hourly rollups are the only long-term copy of these measurements, so prune keeps them. No owner setting for a finite window exists yet; see open question 1. |
| Hardware and backend raw samples and minute rollups (`metrics_store.prune_old`, `metrics_writer`) | Raw samples after 7 days, minute aggregates after 30 days, by age alone, even when no rollup had summarized them | Daily maintenance | (b) | Fixed. Each rollup marks the rows it summarizes (`rolled = 1`) in the same transaction, and catches up every unsummarized row from earlier buckets, so downtime or a failed rollup is repaired at the next run. Prune deletes only marked rows past the window, so expiry lowers resolution and never loses a measurement. On upgrade, an old row counts as summarized only if the coarser table has a row for its bucket, metric and tags. |
| Published media metadata migration (`prompt_metadata_migration`, proxy startup) | Embedded prompt/workflow/EXIF/XMP copies in pre-upgrade media | Automatic startup privacy migration replaces each file with stripped bytes | (c) | Unchanged; left for the owner (question 2). It prevents cross-member disclosure, but an original may hold the only workflow/prompt copy. No archive or import exists. Explicit erasure must still remove these copies. |
| Pre-upgrade video prompt fields (`video_job_records.load_all`) | Inline `record_json.prompt` | Automatic read-time privacy migration | (c) | Unchanged; left for the owner (question 2). Unscoped prompt fields are removed without a maker-scoped transfer. |
| Sessions, session revocation fences and login throttles (`accounts`) | Expired sessions/fences and obsolete rate-limit buckets | Login/session operations; password reset, disable, removal or logout | (b) | Keep security expiry and explicit invalidation. No saved content is stored in these rows. |
| Derived identity claims and access caches (`admin_store`, `accounts`) | Cached claims or superseded claim-derived grants | Claim refresh, schema conversion, explicit profile change/removal | (b) | Keep refresh/invalidation. Schema conversion freezes effective grants before removing ambiguous cached claims; it does not delete users or audit records. |
| Video progress, workflow execution maps and DELETE tombstones (proxy GC) | Transient progress maps and records already explicitly deleted | Progress TTL or terminal grace period; five minutes after explicit DELETE | (b) | Keep. These caches do not determine durable job retention. |
| Execution payload spools (proxy media jobs) | Temporary request payloads | Job terminal; explicit privacy erasure | (b) | Keep finished-job cleanup; preserve absent-job spools because their history may be quarantined. Removed age-based deletion of queued/running payloads, including in-memory payloads. Active private work can still finish during an explicit erasure using memory-only payloads. |
| Speech/transcription processing files (`stt_chunking`, proxy audio routes) | Input spools, normalized WAV copies, segment files and workspaces | Processing completion/failure or controlled cleanup | (b) | Keep temporary-copy cleanup. These are execution workspaces, not Library output. They live in the gateway's private temporary directory (`gateway/private_tmp.py`, `AZIZAM_PRIVATE_TMPDIR`), which `start.sh` empties at every start. |
| Multipart upload spill over 1 MiB, ffmpeg/ffprobe scratch files, LiteLLM and engine temporary files | Request bodies and media while a request runs | Request end (`gateway/request_forms.py` closes spilled parts); start-up sweep | (b) | Same private temporary directory. A deleted file can stay in free disk blocks. |
| Asset write scratch files and failed voice staging (`proxy.store_asset_bytes`, `custom_voices._commit`) | Incomplete or already-published temporary files | Write failure or successful atomic publication | (b) | Keep. Published media/voice records are separate. |
| Prompt metadata migration scratch (`prompt_metadata_migration`) | Recognizable old helper files, stripped scratch copies and abandoned staging files older than one hour | Privacy migration/cleanup pass | (b) | Keep scratch cleanup. Published media bytes remain after metadata stripping; the existing privacy migration removes embedded prompt copies to prevent cross-member disclosure. |
| Privacy purge coordination (`prompt_memory_erasure_intents`, proxy `prompt_purge_queue`) | Completed retry intents | Successful erasure/metadata stripping | (b) | Keep. Durable cutoffs remain to prevent late writes from restoring erased prompts. |
| Extension package source cache (`extension_packages`) | Temporary fetched clone | Source refresh or cleanup | (b) | Keep replaceable clone cleanup; installed package/configuration deletion remains an explicit owner action. |
| Atomic config/signing writes and lifecycle overrides (`local_owner`, `lifecycle_drivers`) | Temporary replacement files; runtime-generated service drop-in | Atomic write completion/failure or explicit lifecycle change | (b) | Keep. These are derived temporary copies and runtime overrides. |
| In-memory capability, status, model, signing-key, cancellation and drain state | Cache entries, expired tombstones or drain flags | TTL, refresh or resource release | (b) | Keep. No durable owner/member records are removed. Live cancellation entries retain their holders. |
| Firehose activity log (`firehose_metrics.compact_log`) | Recent activity projection before the 24-hour display window | Periodic log compaction | (b) | Keep this bounded diagnostic projection; durable request/usage facts remain in receipts. Replay age filters only affect the in-memory view. |
| Database maintenance markers (`sqlite_startup`, `metrics_store`) | Completed quarantine marker or rechecked integrity marker | Successful recovery/check | (b) | Keep marker cleanup only. A partial quarantine leaves a marker that blocks the next startup; it never falls back to deleting database or journal files. |

Inventory totals: **16 class (a), 14 class (b), 2 class (c)**. Totals count audited paths; most class (a) rows no longer delete anything.

Library deletion is scoped to the signed-in person's own assets, including for
an owner. The response's `deleted` field records the person's deletion.
`physical_removed` separately reports whether the deleter removed files.
The gateway's asset deleter (`me_media.conservative_asset_deleter`) preserves
bytes and sidecars shared by multiple owner-key hashes or maker people. The API
also deletes that person's saved prompt for the asset.
The API records an audit entry that hides the item from that person's Library.
The everyone view does not apply those per-person hiding entries.
This endpoint does not itself delete job or receipt rows.

Until 2026-10-11 11:59:31 CDT the gateway deleted every generated file 24 hours
after its last write (the cache eviction loop in `proxy.py`). A file made before
2026-10-10 11:59:31 CDT (24 hours earlier) is the newest one that loop could have
deleted. Library items made before then often have a record and no file. The Library says so, with the
cutoff in the viewer's time zone and "can't be recovered". Remove missing items
(`POST /me/api/media/remove-missing`, after a confirmation in the Library)
clears the signed-in person's own entries whose file is gone:

- It refuses, and changes nothing, unless the media folder is a readable
  directory. A folder that is missing, not a directory or unreadable looks the
  same as every file being gone. A readable folder with no media file (empty,
  or only sidecars or scratch files) looks the same as an unmounted volume, so
  the removal refuses it whenever it would drop an entry. When nothing is
  missing the removal succeeds and removes nothing, whatever the folder's state.
- A nonempty folder alone is not proof of the right disk: a write onto a bare
  mount point can leave one new file while every older file is on an absent disk.
  The root marker `.azizam-media` holds a random volume ID and creation time;
  `media_volume_identity` beside the Library index records the expected ID.
  Missing, malformed or mismatched markers refuse removal without deleting
  entries. The core rechecks the identity inside the deletion transaction.
- The first Library read on a fresh install (nothing recorded, no entry in
  either store, and no media file in the folder) records the marker. Sidecars,
  scratch files and the signing secret do not count as media. Any other folder
  state needs the owner. Ordinary media writes never create the marker, and a
  recorded identity is never regenerated, even after every entry is removed.
  The marker is created with `O_EXCL`, so a filesystem without hard links (FAT,
  exFAT, some SMB mounts) works.
- Existing installs and intentional moves need the owner, once, and only when
  they remove something. What a folder holds never confirms it: a partial copy
  or an old backup can hold most of the Library while the rest sits on the
  original disk, and Remove missing would then delete those entries. The
  Library read carries `media_folder`: `confirmed`, `unconfirmed` or `mismatch`.
  Nothing asks about it while browsing; missing items stay listed as
  unavailable. When the owner chooses **Remove missing items** and the folder
  is not confirmed, the confirmation dialog for that action asks: it names the
  folder, says how many of the Library's checked items have no file there, and
  offers **Remove N items**. That one choice calls
  `POST /me/api/media/confirm-folder` and then removes. A folder that holds none
  of a non-empty Library cannot be confirmed (409 `media_folder_no_matches`);
  the dialog tells the owner to check the media disk instead. The Library
  offers Remove missing only when the viewer-wide `removal_plan.missing_items`
  from `GET /me/api/media` is positive; filters and whole-Library folder evidence
  do not decide this count. The removal selects again and reports the actual
  count. A `mismatch` also shows a warning in the
  owner's list of unavailable items. Members see no folder detail; if they
  choose Remove missing they are told Azizam can't remove missing items until
  the owner confirms where the Library's files are kept. So the owner does not
  have to wait for their own missing items, Settings shows **Confirm the media
  folder** (owner only, only while the folder is unconfirmed or changed). It
  gives the same plain explanation with the whole-Library counts and calls the
  same `confirm-folder` route, with the same refusal when the folder holds none
  of the Library's checked items. With only uncheckable entries, Settings warns
  that Azizam cannot identify the folder from them; an empty Library asks nothing.
  Confirmation writes a new marker into the folder it checked (the marker is
  written relative to the opened directory, and refused if that directory is
  not the one that was listed), records its ID and is audited as
  `media.folder.confirm`. It removes nothing by itself. Back up the marker with
  the media and the identity with the database. Never confirm when the expected
  disk is absent.
- A file is gone only when nothing in the folder holds its digest under any
  suffix (ComfyUI `gifs` and `images` outputs keep their own suffix). The check
  reads the folder, not the Library listing.
- A finished job entry is deleted only when every asset its record names is
  gone. Otherwise each gone asset is hidden as that job made it
  (`media_hidden_assets`), so the same bytes made again by a later job are listed.
  When every one of those assets is already gone from the viewer's Library, the
  entry still goes, so `removal_plan.missing_items` counts it: the removal never
  deletes more entries than the caller confirmed.
- A directory replacement after the final identity check but before the SQLite commit can still go undetected.
- Prompts, hides and job deletions commit together in the media-jobs database or
  not at all. It never deletes a stored file or a receipt.
- Removing a person's account deletes their hide rows with their prompts, in one
  transaction.

A quarantine uses a new private `<database>.broken-<time>-<unique-id>/`
directory on the same filesystem. If any rename fails, startup raises and the
`<database>.quarantine-pending` marker remains. The original files may be split
between the original location and that directory. Stop users of the store,
inspect both locations, and restore or finish the move before removing the
marker. No automatic attempt deletes the files to make room for a fresh store.

Each of the media-jobs, receipt, admin, Responses and metrics stores has a
`<database>.lock` file next to it. Do not delete it while the gateway runs.
The guarantee is limited to connections opened through `sqlite_startup`:

- Every such connection, writable or read-only, holds a shared `flock` on the
  lock file while it is open, and refuses to open while the marker exists.
  This includes the store modules, prompt memory, video records, accounts
  setup at import, secrets and the Library, Usage, activity, home and
  availability readers.
- Store startup (`init_db`) takes the exclusive lock and holds it from the
  integrity check through quarantine and schema setup. A connection that asks
  to open during that span waits for it to finish.
- If another such connection is open, in this process or a maintenance
  script, startup cannot take the exclusive lock. It checks under a shared
  lock instead: a healthy store initializes; a corrupt store stays in place
  and startup raises "Corrupt database is in use".
- Startup records the file's device and inode before the check and refuses to
  move a file that changed before the rename.

Not covered: processes that open the files directly (the `sqlite3` shell,
backup tools, copies of old code), and filesystems where `flock` is not
honored between hosts, such as some network mounts. Stop those before
recovery. The provider spend ledger and category store are outside this
scheme: they have no corruption quarantine and no destructive recovery.

Verification uses temporary stores and an isolated checkout. No live databases
were inspected or changed. This inventory covers gateway-owned retention;
external engine history, filesystem cleanup jobs and backups are UNVERIFIED.

## Execution substrate accounting

The pinned LiteLLM launcher prevents new request-content, error and tool-inventory
writes. Numeric budget counters and aggregate cost metadata remain; the
`litellm-spend-cost-metadata` fact in `registry/retention.yml` discloses them.
Existing spend, error and tool rows remain until the owner chooses to purge them.
The proposed seven-day legacy spend cleanup is not enabled. Private file modes
and missing-output checks do not erase those records or prove their absence.
See [the slice 3 verification](../verification/RETENTION_S3.md).

## Private files and deployment

New content files, database files and atomic replacement stages use mode `0600`.
Content directories use mode `0700`. Existing databases keep their current modes
until the operator migrates them. Symlinked ancestor directories are supported;
a symlink at the private directory's final component is refused. Staging files
use `.<filename>.<random>.tmp`. Job deletion, prompt erasure and orphan pruning
recognize these names as well as legacy `<filename>.tmp` stages. The metadata
migration removes abandoned media stages after one hour. Firehose compaction
removes abandoned `.key_activity.jsonl.*.tmp` stages under its writer lock.

Run `scripts/check-private-modes --root /path/to/checkout` to report permissions
below `data/` and `cache/`. It reports counts, without content filenames. Add
`--fix` to set regular files to `0600` and directories to `0700`; symlinks are
refused. Schedule this repair during an operator maintenance window.

Run this check against a stable, checkpointed SQLite snapshot:

```sh
scripts/check-missing-outputs --database /path/to/checkpointed/media_jobs.db \
  --media-dir /path/to/media
```

The command reports missing successful-job output counts by media kind without
identifiers or store changes. It refuses a nonempty WAL or a database that changes
during inspection. Historical job success remains recorded if bytes are gone.
Content requests return `410` for missing output and `503` with `Retry-After`
for temporary storage errors.

The unit sets `UMask=0077` for private file creation by the service and its
children, and `LimitCORE=0` to prevent core dumps from retaining request content.
**Deployment requires reinstalling the unit:** the installed systemd unit is a
copy, so editing the checkout alone does not update these settings. From the
deployed checkout, run:

```sh
cp azizam.service ~/.config/systemd/user/azizam.service
systemctl --user daemon-reload
scripts/safe-restart
```

Voice reference files (`voice_ref`) are private too. A backend that opens their
paths must run under the gateway's same Unix UID; a different UID cannot read
mode-`0600` references or traverse mode-`0700` directories.

## Open owner questions

These are owner choices. Until the owner decides, the gateway keeps the data
where it can do so without a new disclosure.

1. **Telemetry history.** Hourly hardware and backend rollups are now kept
   with no limit. Their size grows by a few bytes per metric per hour. Should
   the owner be able to set a finite window, with a deletion preview, like
   receipts?
2. **Prompts removed by privacy migrations.** Two existing migrations remove
   prompt copies that other members could read: embedded media metadata and
   inline video-record prompts. The originals can be the only copy of a
   maker's prompt or workflow. Neither was changed. Turning them off would
   expose prompts again. A private, maker-scoped archive needs a design and a
   red-team first (P20). The live gateway has probably already run both
   migrations (not inspected). Pre-release backups such as
   `data/media_jobs.db.bak.pre-r15` may still hold the original video
   prompts; keep them until the owner decides.
3. **Replay window.** A request ID replays its finished job for 14 days, the
   lifetime the old job pruning gave. A failed seeded image request inside that
   window still returns the failed job, as before. Should a failed or canceled
   job stop replaying at once?
