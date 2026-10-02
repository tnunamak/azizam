# Long video: ideal design and where the first pass stands

This note says what the best long-video path for this gateway looks like, what
the first pass (this branch) does, and what is still missing. Request shapes
are in `docs/LOCAL_CLIENT_RECIPES.md`, section "Long video".

## What the first pass is

One request makes one ComfyUI job. `gateway/video_extend.py` plans segments and
builds the graph in code; `proxy.py` routes, validates and records.

- API: `POST /v1/videos/extensions` (Sora's shape), `shots[]` and long `seconds`
  on `POST /v1/videos`. All three use the same planner and graph builder.
- Continuity engine `ltx25_chain` (frame window): the last `context_seconds`
  (default 1 s, 25 frames) of the video so far are VAE-encoded and pinned to the
  start of the next segment with a latent guide (`LTXVAddLatentGuide`, strength
  0.9). The pinned part is sampled with the segment, then cut away. So the model
  sees about 1 s of real motion, not one frame.
- Per-segment prompt and seed; optional `state_ledger` text in front of every
  segment prompt.
- Audio: each segment makes its own audio with its video (joint AV latent); the
  new part is cut to the new frames, so audio and video stay the same length.
  Audio is not conditioned on the previous segment's audio.
- Safety: a total-length cap (`videos.max_total_seconds`, 40), a clear
  `video_total_length_exceeded` error, ownership on every route, persisted
  records (so the source of an extension survives a restart).
- Failure: the job fails as a whole with the failed segment and the count of
  finished segments. No partial video is returned.

## What the spike found (LTX-2.5 distilled, int8, 960x512)

- `LTXVExtendSampler` works on the 2.5 weights, video-only (it needs a plain
  video latent, so no audio). A core-node loop with the same method (latent
  guide at index 0, sample, crop guide, drop first latent, linear blend of the
  overlap) works with audio latents too.
- 24 s, three prompts, 310 s total, joins show no frame-difference spike, motion
  continues across joins and the scene follows each prompt. See the report and
  `~/media-refresh-samples/long-video/`.
- `LTXVLoopingSampler` was not tested: it tiles one prompt-per-tile pass over a
  guiding latent and has no audio path. It is the right tool for video-to-video
  detail work, not for open-ended extension.

## The ideal end state

1. A continuity backend per engine, chosen by workflow (`builder:` key; see
   `video_extend.ENGINES`):
   - `ltx25_chain` (now): frame window.
   - `ltx25_latent` (next): keep the latents between segments in one graph or in
     saved latents (`SaveLatent`/`LoadLatent`), and use `LTXVExtendSampler`'s
     overlap blend. No decode and re-encode of the context; best joins. Needs a
     latent store that survives a ComfyUI restart if it must serve extensions of
     old videos. Extending an arbitrary mp4 would still fall back to the frame
     window.
   - `h3_chain` (built, see below): MiniMax H3 with the H3 Motion Context nodes.
2. Per-segment retry and resume: store each finished segment (mp4 or latents) so
   a failure at segment 5 keeps segments 1 to 4 and a retry continues from there.
   The first pass keeps one job per request and reports where it stopped; the
   client can extend the part it kept.
3. Audio continuity: condition each segment's audio on the end of the previous
   audio (audio latent guide or the `LTXVAudioVideoMask` approach the old graph
   used), plus a short crossfade.
4. Better join control: a short pixel crossfade at the cut, a motion-match check
   (frame-difference at the join) with an automatic re-sample of a bad segment.
5. Measured state ledger: a JSON ledger (character, pose, direction, props, light,
   camera, next action) built from the last frames by a vision model, compared
   against no ledger on a fixed test set. The first pass only prepends text.
6. A planner that splits on rest beats (a vision or text model finds the
   pauses), instead of the client doing it.
7. Cap policy from data: 40 s is a judgment (hosted extend limits and community
   reports), not a measured LTX-2.5 threshold. Measure drift over 8 segments and
   set the cap from that.

## How far the first pass gets

| Ideal item | First pass |
|---|---|
| Extend endpoint, overlap context | Done (frame window, 1 s default) |
| Storyboard and long-seconds sugar | Done |
| Total-length cap, clear error | Done, config key |
| Persisted records, ownership | Done |
| Mid-chain failure info | Done (segment, counts); no partial video, no resume |
| Pluggable engines | Registry and routes done; one engine |
| Latent continuity | Not built (spike shows it works video-only; with audio via core nodes) |
| H3 engine | Built (`h3_chain`); see "H3 engine" |
| Audio continuity | Not built |
| Ledger beyond text, auto rest-beat splitting | Not built |
| Cap from measurement | Not done |

## H3 engine (`h3_chain`)

`gateway/video_extend_h3.py`. Same routes, planner shape, cap, record and failure info as the LTX
engine; the engine interface (`video_extend.ChainEngine`) carries what differs per model: frame
grid, context window, accepted options, minimum shot length, source checks.

Why this method (research 2026-10-02, `ai/research/video-generation/long-video-api-should-be-explicit-extend-primitive-...`
and the r/StableDiffusion and r/comfyui threads of September and October 2026):

- H3 has no native extend. The community pins the tail of the previous clip at the head of the
  next one (never denoised) and cuts it away. ComfyUI 0.34+ has `MiniMaxH3AddGuide` for this, but
  it takes decoded frames, so every join costs a decode, a resize and a VAE encode, and the sound
  restarts.
- The widely used fix is to slice the tail straight out of the previous latent (picture and
  sound), which skips the round trip, and to put the pinned sound on the new clip's own timeline
  so it ends at the join. Several packs do this (Motion Context, Latent Continuation, the H3
  Multishot packs, obvpm timeline). `ComfyUI-H3-Multishot` is 42,000 lines with GGUF patches and a
  remote-encode module; it was not installed. `ComfyUI-H3-Motion-Context` is about 1,500 lines of
  node code, a small API surface, and checks ComfyUI's layout arithmetic before the first render.
- Decoded-frame chaining degrades quickly ("photocopy" effect, 5 to 7 clips); the community also
  reports the latent path "is not a world model": identity needs references and prompts.
  So: one second of context, a prompt that holds the closing framing, and a cap.

Pinned: `NikoDemon80/ComfyUI-H3-Motion-Context` 0.6.2, commit
`5335715abe54c1a9bfbe3494da29aae3e8635ce3` (GPL-3.0; personal use), in
`ComfyUI/custom_nodes/ComfyUI-H3-Motion-Context` at that commit. No new Python packages (uses
torch, torchaudio, safetensors and aiohttp from ComfyUI's venv). It adds two HTTP routes
(`/h3_motion_context/slot_exists` and `/clear_latents`, same-origin checked, limited to numbered
`.safetensors` files under ComfyUI's output folder) that the gateway never calls; the graphs use
only the `MiniMaxH3MotionContext` and `MiniMaxH3MotionContextTrim` nodes.

Graph per segment: `MiniMaxH3ImageToVideo` (prompt, empty AV latent of `context + new` frames) ->
`MiniMaxH3MotionContext` (`context_latent` = previous `SamplerCustomAdvanced` output; for the first
added segment of an extension, `context_frames` + `context_audio` from the source video) ->
`BasicGuider` -> `SamplerCustomAdvanced` (8-step turbo LoRA, `res_multistep`, the settings
validated for single clips) -> `VAEDecode` + `VAEDecodeAudio` -> `MiniMaxH3MotionContextTrim`
(cuts the pinned head from picture and sound, makes the sound exactly as long as the picture).

Not built: latents persisted per video, so an extension of an old video also takes the lossless
path (a latent is about 5 MB per clip; needs a store that survives a ComfyUI restart);
re-rolling one segment without re-rendering the later ones; reference images re-injected every
segment for identity.
