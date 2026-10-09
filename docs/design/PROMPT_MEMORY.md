# Prompt memory backend

Status: implementation record, 2026-10-09. Product choices come from
`~/Documents/azizam/briefs/prompt-memory-spec.md`; the local prior-art entry is
`~/code/divan/product-design/consumer-ai-products-keep-prompts-by-default-with-a-per-session-off-switch-while-family-products-show-organisers-metadata-not-content-and-embedded-prompts-leak-in-shared-files.md`.

## Code challenges and decisions

The brief says an off request writes no prompt bytes under Azizam's data and
cache directories. That conflicts with the durable async image-job payload,
which currently writes the entire request before execution, and with the
Responses `store: true` conversation contract. For an off request, the async
job payload must live only in process memory; it cannot resume after a gateway
restart. A forced-off Responses request behaves as `store: false`: the response
is returned, but GET and `previous_response_id` cannot retrieve it. This is a
deliberate compatibility change required by the privacy control.

The brief also asks for one prompt write path while ComfyUI puts a second copy
of the prompt in PNG and container metadata. The asset cache must store only
metadata-stripped bytes. The prompt record is the sole durable gateway copy;
when kept, that record may include the original workflow metadata needed to
construct a maker-only full download. Deleting or expiring the record removes
both text and that workflow copy. The generated media itself remains.

OpenAI image URLs are bearer capabilities and are commonly fetched without the
original API key. A transferable signed URL cannot prove who is reading it.
Therefore signed URLs are `metadata=none`; a full metadata fetch requires an
authenticated maker on an API content route. Old signed URLs with `md=full`
must fail closed. The `full` profile default still governs the direct response
to the maker's own authenticated API request.

No-key LAN calls and service Apps have no person. Their prompts are not saved:
there is no defensible maker-only viewer or retention setting for them. They
still receive their generated media. Existing owner instance views retain
metadata about these calls, without prompt-derived content.

Azizam cannot promise prompt erasure from an inference engine's own history,
external backend output directory, filesystem snapshots, or a recipient's
download. The no-bytes test covers the gateway's configured data and cache
directories after a request. The UI policy must state this boundary plainly.

## Data and identity

`asset_prompts` is an additive table in the media-jobs SQLite database. Its
key is `(asset_id, person_id)`, because a content-addressed asset can be made
by more than one person. Fields are `prompt_text`, `created_at`, `kept_until`
(NULL means never), and optional workflow metadata for maker-only export. It
is separate from receipts, media-job metadata, asset sidecars, and video-job
records; their prompt redaction guards remain strict. A record is read only
if its `person_id` equals the current authenticated person and it has not
expired. An absent row means `prompt: null` and a non-prompt title.

The request maker is resolved from the authenticated key's App to
`apps.owner_user_id`. This is the same durable attribution used by Library
scope. A service App or no-key request resolves to no person. A browser member
is `users.id`. The owner is a maker only for the owner's own personal Apps;
the owner role does not grant prompt visibility for any other person.

Member preferences live in a new `admin.db` table keyed by `users.id`:
`remember` (default: household default) and `retention` (`never`, `3_months`,
`1_year`). The owner setting is `operator_settings.prompt_memory`, with
`default_remember=true`, `never_keep=false`, and `retention_cap=never` by
default. The effective expiry is the shorter of the member choice and owner
cap, measured from request time. The kill switch wins over all other choices.

| Viewer | Own asset prompt/title | Another person's prompt/title | Full workflow download |
| --- | --- | --- | --- |
| Maker signed in | Kept and unexpired only | Never | Own kept asset only |
| Other member | Never | Never | Never |
| Owner in instance Library | Own only | Never | Own kept asset only |
| App key | Own request output under profile mode | Never | Authenticated maker key only |
| Signed URL holder | Never | Never | Never |
| Service App / no-key LAN | Never | Never | Never |

## Request and API contracts

The request control is `extensions["com.azizam.prompts"].mode = "forget"`.
The value is validated before dispatch, does not reach the backend, and only
its name and decision are eligible for receipts. This is a gateway privacy
control under the extension rules in `docs/EXTENSION_TARGETS.md`; it is not a
workflow input. OpenAI Responses `store:false` has the same prompt-memory
effect without changing the meaning of the standard `store` field.

Member API: `GET/PUT /me/api/prompt-memory`, `DELETE
/me/api/prompt-memory/prompts`. Owner API: `GET/PUT
/admin/api/settings/prompt-memory`, using `If-Match` revision semantics. The
member reads their effective settings but cannot override the owner kill
switch or cap. Delete-all removes prompt records, not media, receipts, or
other household members' records.

Library `prompt` and prompt-based title fields are projected only after a
maker-id equality check. Search over prompt text uses this same projection.
`workflow_download_url` appears only to the maker while the prompt record
exists. `/v1/videos` keeps its OpenAI response shape and returns `prompt:null`
when the prompt is not kept or the caller is not the maker. Status and
firehose carry no prompt snippets. Default signed URLs carry `md=none`.

## Retention and upgrade

A periodic pruner removes expired prompt rows. Delete-all removes one person's
rows immediately at the database level; SQLite's normal page/WAL behavior and
backups mean physical byte erasure is not promised. Media and its stable asset
ID remain. New asset bytes are stripped on store. Historical asset files lack
reliable maker attribution, so no historical prompt is imported; migration
scrubs their embedded prompt/workflow before further full delivery. Historical
video records are purged of the prompt field on load/save, and no Library read
falls back to embedded metadata. Existing Responses rows keep their original
seven-day expiry; they are not retroactively reclassified as media prompts.

