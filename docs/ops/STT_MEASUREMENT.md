# Measure long-audio settings

The owner can edit target seconds, search width (including zero), the hard
maximum, and native long-audio handling for offline and streaming requests
beside **Check now** on the engine page. A record with no saved or measured hard
maximum has no known engine limit: Azizam sends requests it cannot cut whole,
and the page offers measurement. A saved maximum protects even uncut requests,
unless the record permits native long audio for that request mode.
Saving sends only changed fields, so effective defaults and derived values stay
unwritten. A target that reaches the old cut threshold raises that threshold
above the target (or above the effective hard maximum for exact cuts). An
explicit threshold supplied in the REST patch is preserved and validated. An
explicitly saved hard maximum still constrains the target.
Saving these fields does not run a measurement.

**Measure long-audio settings** fetches public, human-transcribed samples from
`registry/stt_samples.yml`. Downloads are checksummed. Audio stays in a private
job workspace and is deleted when the job finishes. Trial lengths, widths,
download/request budgets and the deadline are owner-editable data in
`registry/stt_measurement.yml`. Conversion runs in an owned process group;
cancel and deadline expiry kill that group, including ffmpeg. Cancellation
also interrupts gateway transcription and GPU admission waits. On Linux the
worker arms a kernel parent-death notification before fetching: gateway death
kills the worker's owned group, including conversion children. A workspace lease
is inherited by the worker. Startup removes abandoned scratch directories only
when that lease is free; live workers' files are preserved. Before downloading
samples, the gateway checks the selected service alias. When the record declares
a health endpoint, it also makes a bounded health check. This check does not
start the service.

Declare `resources.gpu_required` on the service record before starting. For GPU
engines, also declare a positive `resources.vram_gb_estimate`. Before each
transcription the job checks measured GPU memory and utilization, and the shared
media queue's queued/running work and in-flight live STT. It waits for headroom
and idle live STT;
unknown GPU or queue facts do not admit work. Set `resources.gpu.devices` to
bind the check to specific GPU indices. Without a binding, it conservatively
requires headroom on every reported GPU.
Declared VRAM is total engine demand. Admission subtracts only the current VRAM
attributed to this exact service, then requires the additional demand plus the
registry's free-memory margin. Unknown attribution earns no credit; allocations
of other services remain counted. This is an admission check, not an exclusive
GPU reservation. CPU declarations
skip GPU probes. All requests still use the normal gateway admission and disk
limits. The tool never starts a stopped engine.

SQLite stores bounded job history, progress and aggregate reports beside the
admin database. A page reload resumes polling. After a gateway restart,
unfinished jobs are marked interrupted and can be run again. Reports include
raw deletion rates by piece length, paired comparisons and their bootstrap CIs.
Running jobs stop when the owner's role is removed or the MCP connection that
started them is revoked.
The recommendation is a **gross-harm screen**, not proof of no harm or a claim
that widths are equivalent. The owner-editable default noninferiority margins
are 1 percentage point for length and 3 points for seam-minus-noise comparisons.
Trials are bounded to 120 seconds and to the current planner's safe piece limit;
the default length choices are filtered to that limit. If fewer than two fit,
the page explains what must change before measurement can start.

Length trials compare the same reference words inside pieces at each length
against the shortest successful length. Each recording contributes separate
30-second blocks; bootstrap resampling uses blocks, not individual words. The
longest length whose paired upper CI is within the margin is eligible only when
every attempted shorter length also passes. A failed, sparse, or inconclusive
shorter trial prevents recommending a longer maximum. At least
two lengths must have 12 blocks and 500 common interior words each. Shared engine
deletions cancel in this comparison. Raw deletion rates and their intervals stay
in `deletions_by_length`; its `paired_deletion`, `baseline_length`, `state` and
`reason` fields explain selection.

Seam trials compare cut versus no-cut errors on the same reference words. The
`difference` interval bootstraps seam excess minus noise excess directly, rather
than subtracting marginal CI endpoints. Each width requires at least 20 seam
pairs and 20 noise pairs, with 200 words in each set. A width qualifies when the
upper difference CI is within the margin. Among qualifying widths, choose the
lowest upper interval bound. Keep the current width when it ties for that bound;
otherwise use the narrower width to break a tie. Requested widths are tested
unchanged within the safe range of the current target and the smallest possible
trial target. Widths outside that range are rejected before a job starts.

Reports include `recommendation_state` and per-trial `state`/`reason`: recommended
means the screen passed; inconclusive means counts are insufficient or the CI
crosses the margin; confidently_worse means the lower CI exceeds the margin
(or the engine returned a confirmed size refusal). Sparse or uncertain trials
abstain. Reference word timing may be interpolated; reports preserve that label.
Seam times use the gateway planner and are checked against executed attempts.

**Apply recommended settings** is a separate action. It requires the current
registry revision and refuses if the engine changed since measurement. Applying
enables cutting and sets its threshold to the recommended maximum. No trial
changes saved settings.

REST paths under `/admin/api/engines/{service_id}`:

- `PATCH /stt-settings`: edit target, search, hard maximum, and native long-audio fields independently; requires `If-Match`.
- `GET /stt-measurements`: sample choices, policy, latest job, revision and shell commands.
- `POST /stt-measurements`: start; optional `sample_ids`, `lengths`, `search_widths`, `model`.
- `GET /stt-measurements/{job_id}`: progress, report and generated owner REST commands.
- `POST /stt-measurements/{job_id}/cancel` or `/apply`; apply requires `If-Match`.

The engine page generates curl commands using `AZIZAM_BASE` and
`AZIZAM_OWNER_KEY` placeholders. Browser writes use the existing CSRF check.
MCP exposes `stt_measurement` with `options`, `configure`, `start`, `status`,
`cancel`, and `apply`. Configure/apply take `revision`. MCP operator access
requires owner OAuth; ordinary App keys do not grant management access or see
the operator tool in `tools/list`.

The bundled GPU transcription service declares its resource estimate in
`proxy_config.yml`; no engine-specific requirement is in code. Existing registry
records keep their saved configuration. To declare resources on a legacy record,
use the existing owner endpoint with its current registry ETag:

```sh
curl -sS -X PATCH "$AZIZAM_BASE/admin/api/operator/services/$SERVICE_ID" \
  -H "Authorization: Bearer $AZIZAM_OWNER_KEY" \
  -H "If-Match: $REGISTRY_ETAG" -H 'Content-Type: application/json' \
  --data '{"resources":{"gpu_required":true,"vram_gb_estimate":3.2}}'
```

Replace `3.2` with that engine's declared demand in GB. CPU services use
`{"resources":{"gpu_required":false}}`. This goes through the normal service
record validation, owner authorization, compare-and-swap and hot reload.

The sample manifest includes AMI, ICSI and Earnings21. Each default trial uses
up to 300 seconds of its recording, within the request and 512 MB download caps.
Synthetic noisy-engine
tests with 10% shared deletions and about 3% run-to-run disagreement show this
grid can meet the paired evidence gates. This is a reproducible feasibility
check, not real-engine validation. Real speech may still
abstain because of sparse speech, uncertain intervals or excess seam errors.
All recommendation limits are under `recommendation` in the policy registry;
reports retain the limits used. Gateway failures and timeouts stop measurement;
they do not imply an unsafe engine length. Only an observed upstream response
whose status is in `length_refusal_statuses` supplies a size-refusal boundary.
