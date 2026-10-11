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
metadata-stripped bytes. The prompt record is the sole retained media prompt copy;
when kept, that record may include the original workflow metadata needed to
construct a maker-only full download. Deleting or expiring the record removes
both text and that workflow copy. Generated media remains until explicitly
deleted.

OpenAI image URLs are bearer capabilities and are commonly fetched without the
original API key. A transferable signed URL cannot prove who is reading it.
Therefore signed URLs in generation responses are `metadata=none`, even when
the direct base64 response follows a `full` profile. A full metadata fetch requires an
authenticated maker on an API content route. Old signed URLs with `md=full`
must fail closed. The `full` profile default still governs the direct response
to the maker's own authenticated API request.

No-key LAN calls, service-App keys and keys minted for someone else have no person. Their media prompts are not saved:
there is no defensible maker-only viewer or retention setting for them. They
still receive their generated media. Existing owner instance views retain
metadata about these calls, without prompt-derived content.
Their direct API response follows the caller's metadata profile because it is
the caller's own output; the gateway cache and later signed URLs remain clean.
The OpenAI Responses store defaults to off for no-person requests. A caller
with a stable authenticated App or credential can explicitly opt into saved
conversation state with `store:true`. Saved conversations remain until
explicitly deleted. Keyless LAN requests have no private
conversation scope and are not stored. Rows require matching person and App ID
or the fallback credential hash; App display names do not grant access. The
request-level forget control, `store:false`, and the owner kill switch disable
that storage.

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

The request maker is the authenticated session/OIDC person, or the owner of a
personal App when its key was created by that same person. Keys on service
Apps and keys an administrator creates on someone else's App have no person.
Their media remains App-attributed in the Library and receipts, with no saved
prompt or maker-only workflow export. Library visibility uses App access,
independent of creator metadata.

After the audit backfill, unresolved personal-App keys with NULL
`credentials.created_by` have no person. They retain App access, but cannot
read the owner’s saved prompts or full workflow downloads. Legacy service keys
also have no person. Sharing a key whose recorded creator is its personal-App
owner delegates that owner’s identity and prompt access.
The owner can read databases or issue a reset link to sign in as a member.
Privacy in the UI/API does not protect against that server authority.

Both admin and self-service key-minting APIs return `prompt_memory.person_id` and
`prompt_memory.description`. For service Apps or someone else's App the copy
is: "Prompts sent with this key are not remembered. Generated items belong to
the App. Chat requests can explicitly ask to save a conversation until it is
explicitly deleted." For an own personal-App key it says prompts follow your settings
and anyone holding the key can access your saved prompts. The web key form
must show this description when its frontend work ships.

Member preferences live in a new `admin.db` table keyed by `users.id`:
`remember` (explicit snapshot of the household default at rollout or user creation) and `retention` (`never`, `3_months`,
`1_year`). The owner setting is `operator_settings.prompt_memory`, with
`default_remember=true`, `never_keep=false`, and `retention_cap=never` by
default. The effective expiry is the shorter of the member choice and owner
cap, measured from request time. The kill switch wins over all other choices.
Enabling the kill switch also purges previously kept prompt records for every
member. Turning it off later cannot restore them.
Changing a member's retention or the owner cap shortens existing deadlines;
it does not lengthen an earlier deadline. Prompt-bearing writes recheck these
settings under an admin database write lock, so an in-flight generation cannot
restore a prompt after the owner switches memory off.

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
switch or cap. Delete-all removes retained prompts and conversations and moves
active execution payloads to memory; media and body-free receipts remain.
Erasure also removes media-job body hashes and seeded dedupe IDs, with durable
per-App cutoffs to prevent stale worker writes from restoring them. Legacy
credential ownership is included. Unseeded image and new video job tokens are
random rather than hashes of the body.
A durable deletion cutoff also blocks late writes and image retries from
requests that began before deletion. A same-second new request can be refused
storage. A failed cutoff write returns 503 with retry_required.

Library `prompt` and prompt-based title fields are projected only after a
maker-id equality check. Search over prompt text uses this same projection.
`workflow_download_url` appears only to the maker while the prompt record
exists. `/v1/videos` keeps its OpenAI response shape and returns `prompt:null`
when the prompt is not kept or the caller is not the maker. Status and
firehose carry no prompt snippets. Default signed URLs carry `md=none`.

## Retention and upgrade

A dedicated maintenance loop expires prompt records according to the member's
chosen retention and retries pending erasure. Generated media and saved
conversations remain until explicitly deleted. Library/video deletion, video
failure and member removal remove associated prompt rows. Disabling an account
suspends access but retains prompts until their chosen expiry or explicit
deletion, and retains conversations until explicit deletion; re-enabling
restores access. Only permanent member removal creates an account erasure
cutoff. Cancellation clears execution spools and the current video placeholder.
An in-flight backend generation can still finish and store its result.
Database deletion uses SQLite secure_delete. Backups and earlier WAL snapshots
can retain bytes; no forensic erasure guarantee is made.

A resumable worker strips legacy PNG/JPEG/WebP text, EXIF, XMP, workflow chunks
and audio/video metadata in place. It uses one low-priority thread, bounded
batches and a delay between files. Progress stores fingerprints, never prompt
text. Files keep their IDs and filenames as opaque handles. Atomic replacement
and a shared cache mutation lock protect concurrent gateway writes/deletes.
External filesystem writers must coordinate separately. Invalid or unsupported
files remain pending. ffmpeg/ffprobe use anonymous memory-backed file handles;
legacy crash scratch files are removed. Lossless JPEG/WebP metadata insertion
preserves compressed image data, and clean WAV/MP3/FLAC stripping requires no
ffmpeg. Input-reference resolution and MCP reads strip metadata on delivery.

Switching memory off records a durable erasure intent and late-write cutoff
in the same transaction as the preference. A later re-enable cannot cancel
that intent. Failed database deletion stays pending and maintenance retries
until it succeeds, preserving rows created after the cutoff. If recording
intent fails, the API returns 503 and leaves the preference unchanged.
Delete-all and the kill switch normally remove database rows immediately and queue
file stripping for background work. HTTP 202 reports queued cleanup; the
settings GET keeps queued/retrying status visible until the persisted queue
is drained. Startup and periodic maintenance retry unfinished work. Cleanup
uses recorded setting-change cutoffs, preserving prompts and chats saved after
a member re-enables memory. The kill switch takes effect before cleanup and blocks
new prompt writes. Settings report database deletion as pending until it has
completed; queued file cleanup remains visible separately. Recent video prompts with unknown creators are removed at
upgrade rather than assigned to an App's owner.

Normal async jobs survive restarts. No-person service/LAN jobs keep the original
prompt in a private execution spool because a redacted inference request cannot
resume. Completion, failure and cancellation delete that spool; orphan pruning
is the fallback. Explicit forget, member-off and kill-switch requests are
memory-only and cannot resume after restart. Delete-all and the kill switch
move already queued payloads to memory so the current process can finish them.
Enabling the kill switch deletes stored Responses conversations, so earlier
conversation chains become unavailable. Disabling it cannot restore deleted
prompts or conversations.

Legacy App keys recover their creator from creation audit events at upgrade.
The migration reads audit facts outside write transactions and commits no more
than 100 key updates at a time. Unknown creators have no person and cannot access the App owner’s prompts. Startup reports the unresolved key count; see CORE_CONTRACTS for
the counting query and the access consequence. Policy reads take no write lock
when the member preference already exists. Contention during legacy preference
initialization degrades to no prompt storage, while inference proceeds.
