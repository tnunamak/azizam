# Prompt-memory review fixes

Implemented on `feat/prompt-memory` in the prompt-memory worktree. No live
checkout changes, deployments, service restarts or pushes.

## Findings

| Finding | Result | Evidence or remaining limit |
| --- | --- | --- |
| P0-1: App keys inherit another person’s identity | Fixed | Browser user or credential creator is the person. Owner-created member-App keys cannot read member prompts, full workflows or acquire that member's Library ownership. Unknown legacy creators fail closed. |
| P1-1: Deleted/orphan prompts survive | Fixed | Library/video deletion, cache eviction, member removal and video failure remove rows. Independent maintenance deletes expired, missing-asset and 14-day unfetched-video rows. |
| P1-2: Legacy embedded prompts survive | Fixed | Resumable throttled migration strips cached image/audio/video metadata. Delete and kill paths strip synchronously in a thread; unfinished files return 202 and durable retries. Corrupt/unsupported files stay honestly pending. |
| P1-3: Responses scoped by App name | Fixed | Person plus immutable App ID, or credential hash fallback. Old unscoped rows are inaccessible. Same-name Try Apps cannot share chats. |
| P1-4: Default flip silently enables members | Fixed | Preferences materialize at rollout and user creation, and missing rows freeze before a default change. The kill switch still overrides them. |
| P1-5: Normal service/LAN jobs lose restart durability | Fixed | Normal jobs retain private execution spools. No-person prompts remain there only because inference cannot resume with redacted input; terminal cleanup removes them. Explicit privacy controls use memory only. |
| P2-1: Member purge misses chats and spools | Fixed | Deletes person-scoped Responses rows, moves active spools to memory and fences late media/Responses writes. Original request timestamps survive image retries. |
| P2-2: No-person Responses defaults on | Fixed | Defaults off. Explicit store:true requires a stable authenticated scope; keyless LAN never stores. |
| P2-3: Raw prompt scratch under cache | Fixed | ffmpeg/ffprobe use anonymous memfd handles; legacy scratch files are swept. Named replacement files contain only clean bytes. |
| P2-4: Blocking/failing prompt write | Fixed for media; partial for chats | Media writes run in threads and safe_put refuses storage on failure while returning generation output. Responses persistence retains its existing synchronous, persist-before-completion contract; moving that separate conversation transaction is deferred. |
| P2-5: Kill purge blocks and lacks retries | Fixed | Purges run in threads. Saved kill/off settings and limits reconcile on startup and every minute, independently of queue insertion. Active payloads move to memory. Failed one-time erasure intent returns 503/retry_required instead of claiming a scheduled retry. |
| P2-6: Prompt pruner depends on Responses | Fixed | Dedicated maintenance loop; individual cleanup operations cannot starve each other. |
| P2-7: Forget stores prompt digest | Fixed for explicit privacy controls; partial | Forced-off seeded requests use random request hashes; explicit Idempotency-Key remains caller-scoped and prompt-free. Normal no-person seeded jobs retain the existing body HMAC for automatic deduplication. Removing that lasting digest changes their replay contract and is deferred with P2-15. |
| P2-8: Workflow/extend controls absent | Fixed | Video extensions start delivery context. Manifest multipart accepts the forget control separately from workflow inputs and applies it before dispatch. Catalog lists both routes. |
| P2-9: Backend errors echo prompts | Fixed | Stored execution summaries keep node type/ID and a fixed reason, without exception text. |
| P2-10: Upgrade drops old video prompts/retains forensic bytes | Partial | Secure deletion enabled for prompt and video-record updates. Old video creator cannot be inferred safely from App ownership, so old prompts are discarded. Prior WAL copies, snapshots and backups have no forensic erasure guarantee. |
| P2-11: Redaction narrow and overbroad | Fixed | Exact prompt field names include prompt_preview/snippet and audio/text/workflow fields; prompt_id survives. Metadata forbidden keys expanded. |
| P2-12: Speech dependency/weak tests | Fixed for common formats; partial | Clean WAV/MP3/FLAC and PCM need no ffmpeg stripping; speech skips embedded-workflow capture. Exact speech byte assertions restored. Other supported containers retain ffmpeg for metadata removal because they can carry arbitrary tags. |
| P2-13: Lossy JPEG/WebP full exports | Fixed | Metadata injection preserves compressed image data; pixel/byte regressions cover it. |
| P2-14: Unnecessary create-response change | Fixed | No-person POST echoes the just-submitted prompt unless forced off. Later full exports still require a retained maker row. |
| P2-15: Replay/cancellation edges | Partial | Cancel removes durable/ephemeral spools and current video placeholder; terminal payload moves cannot resurrect them. Replaying a kept Idempotency-Key with forget, retrying a lost forgotten job with the same key, and aborting already-running inference remain deferred: they need explicit replay/abort semantics rather than changing the existing idempotent job contract here. |
| P2-16: Library per-item prompt queries | Fixed for new prompt work; partial | One batch query loads the viewer's prompts. Existing full historical job scans and attribution lookups remain; changing those indexes/query contracts is outside this privacy repair. |
| P2-17: Shared-key identity | Fixed in backend contract; partial UI | Creator identity and delegation documented in CORE_CONTRACTS and the implementation explainer. Key-minting UI copy is deferred to the frontend task. |
| P2-18: Title time format | Fixed format; partial timezone | Includes month/day and time in server local time. Browser-specific timezone rendering needs frontend timezone context, absent from these backend calls. |

## Contract decisions

- App media authorization and maker prompt ownership are separate. Sharing a key
  delegates its recorded creator's identity. Existing unknown key creators are
  never guessed; replacement keys establish the identity.
- No-person execution spools may temporarily retain prompts to preserve normal
  restart durability. Their existing seeded deduplication HMAC can outlive the
  spool. Explicit forget/off/kill jobs use random hashes and cannot survive
  restart.
- Deletion creates a durable inclusive cutoff. Late writes and retries from
  older requests cannot restore content. Whole-second clocks can also suppress
  storage for a new request in the same second; generation still completes.
- Legacy media IDs remain opaque handles after metadata stripping. Unfinished
  cleanup is pending, never reported as completed erasure.
- Filesystem snapshots, backend histories, earlier WAL copies and downloads are
  outside the erasure guarantee. The owner can read server databases or use
  account recovery; API ownership does not defend against that server authority.

## Verification

Independent review used a different model and inspected the final P0/P1 paths.
The four supplied scratch scenarios became route/identity/Responses/preference
regressions. Original implementations were exercised to prove failures before
fixes; kept controls prove the negative privacy checks can detect real storage.
Added coverage includes deletion during inference/retry, failed erasure intent,
legacy stripping/retry/concurrency, durable spools, maker-only exports, original
video attribution, orphan retention and fail-open media storage.

- Full Python suite: **5,927 passed, 7 skipped, 1 xfailed**, plus **2,351
  subtests passed**, in **807.60 seconds**. The namespace run executed
  `python -m pytest -p no:cacheprovider -q` against this worktree.
- Original-code probes produced five expected failures across the four supplied
  scenarios; kept controls confirmed storage was reachable.
- The ordered Responses/privacy and routing run passed **120 tests plus 31
  subtests** after reproducing and fixing a fixture teardown leak.
- Parsed all **45 changed Python files**; `git diff --check` passed.

## Temporary-directory cleanup

The final runs mounted this worktree directly and reused one uv-hardlinked
venv. No additional Git worktree was created. All owned temporary directory
roots below were removed, including their nested checkout snapshots, venv,
per-run states, logs and helper scripts:

- `/home/tnunamak/.tmp/azizam-pytest.iaKKwNgJ`
- `/home/tnunamak/.tmp/azizam-pytest.7s7ZYzgv`
- `/home/tnunamak/.tmp/azizam-prompt-memory/cache/pm-verification`

No directory was created under `/tmp`. The existing reviewer worktree and
shared Python/uv caches were not created by this task and were left intact.
