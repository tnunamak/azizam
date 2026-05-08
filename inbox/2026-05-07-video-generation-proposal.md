# Proposal: Add video generation to openai-proxy

**Author:** Claude (research draft)
**Date:** 2026-05-07
**Status:** Draft for Tim
**Target service:** `~/applications/openai-proxy/` (`https://openai.vivid.fish/v1`)

---

## 1. Goal

Extend the local OpenAI-compatible proxy with a `/v1/videos` family of endpoints so any client that speaks OpenAI's Sora-shaped API (or our existing in-house clients) can request short text-to-video and image-to-video generations against local hardware. Today the proxy fronts text, TTS, STT and image generation; video is the last consumer-facing modality missing. Adding it locally means:

- No per-second cloud video bills (Sora 2 is $0.10–$0.50/s of output, [Apidog 2025-10-06](https://apidog.com/blog/sora-2-pro-api/)).
- Same auth, logging and rate-limiting story as everything else behind `proxy.py`.
- Clips can be fed straight back into other local pipelines (TTS dub, STT for captions, B-roll for chats).

We are not trying to compete with Sora 2 or Veo on quality. We are trying to deliver a "good enough, no-egress" 720p / 5s pipeline on existing hardware.

## 2. Survey of current local-video options (April 2026)

Numbers below are from cited sources running on a 24 GB consumer card unless noted. "Quantized VRAM" refers to the most popular GGUF/FP8 variant the community actually runs.

| Model | Params | License | FP16 VRAM | Quantized VRAM (FP8/GGUF) | Max output | Latency on RTX 3090, 5s 720p | ComfyUI native? |
|---|---|---|---|---|---|---|---|
| **LTX-Video 2.3** | 13B (full) / 2B (distilled) | OpenRAIL-M (Lightricks) | ~32 GB bf16 ([ltx.io](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos)) | 14–20 GB FP8/GGUF Q8 ([zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/)) | 4K @ 50 fps, ~20 s ([studio.aifilms.ai](https://studio.aifilms.ai/blog/ltx-2-open-source-ai-video-model)) | 2–11 min for 5–20 s @ 720p ([fb post 2026-01-23](https://www.facebook.com/groups/comfyui/posts/882350594537642/), [LTX 2.3 RTX 3090 demo](https://www.youtube.com/watch?v=OL_4VY0h8eQ)) | Yes — built into ComfyUI core ([Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)) |
| **Wan 2.2 TI2V-5B** | 5B (dense) | Apache-2.0 ([Wan-Video/Wan2.2](https://github.com/Wan-Video/Wan2.2)) | ~16 GB | 8–10 GB GGUF Q8 ([dev.to 2025-08](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)) | 720p @ 24 fps, 5 s | ~3–6 min ([neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/), [Wan 2.2 r/comfyui](https://www.reddit.com/r/comfyui/comments/1ogalrt/wan_22_hardware_requirements/)) | Yes ([Comfy docs](https://docs.comfy.org/tutorials/video/wan/wan2_2)) |
| **Wan 2.2 A14B (MoE 27B total)** | 14B active | Apache-2.0 | ~60 GB native ([stablediffusiontutorials](https://www.stablediffusiontutorials.com/2025/08/wan-2.2-video-generation.html)) | 16–24 GB FP8 with offload ([Wan-Video/Wan2.2 README](https://github.com/Wan-Video/Wan2.2)) | 720p @ 24 fps, 5–8 s | 6–10 min on a tight 24 GB card; community calls it "tight squeeze" ([neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/)) | Yes |
| **HunyuanVideo 1.5** | 8.3B | Tencent Hunyuan Community License (territorial; ≤100 M MAU) ([HF LICENSE](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE), [DeepWiki](https://deepwiki.com/Tencent-Hunyuan/HunyuanVideo/1.2-licensing)) | ~24 GB | ~14 GB with offload ([HF model card](https://huggingface.co/tencent/HunyuanVideo-1.5)) | 1080p, 5 s ([quasa.io](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)) | <4 min for 5 s @ 1080p on 3090 Ti w/ TensorRT ([quasa.io](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)) | Yes (unofficial nodes); HunyuanVideo 1.5 native support landing |
| **CogVideoX-5B** | 5B | CogVideoX License (custom; 2B variant is Apache-2.0) ([studioinstance/CogVideo](https://github.com/studioinstance/CogVideo-1)) | ~20 GB ([clore.ai](https://docs.clore.ai/guides/video-generation/cogvideox)) | ~12 GB w/ TorchAO INT8 ([DeepWiki perf](https://deepwiki.com/zai-org/CogVideo/7-performance-optimization)) | 720×480, 6 s, 49 frames | ~5–8 min | Yes |
| **Mochi 1** | 10B | Apache-2.0 ([Genmo blog](https://www.genmo.ai/blog/mochi-1-a-new-sota-in-open-text-to-video)) | ~40 GB native | ~20 GB w/ ComfyUI optimisations ([Apatero](https://www.apatero.com/blog/mochi-1-consumer-hardware-comfyui-optimization-guide-2025)) | 480p, 5.4 s | 8–15 min on a 3090 | Yes, but stalled — see "ecosystem stability" below |

Read this honestly:

- **LTX-2.3** is the most actively iterated model in the ecosystem right now. The author (Lightricks) ships first-party ComfyUI nodes, the community has Q8 GGUFs, and there are distilled variants down to 2B. It is bleeding-edge — workflows still break between point releases ([Comfy-Org issue #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)) — but it's the model people are choosing on a 3090 today.
- **Wan 2.2** is the conservative pick. The 5B TI2V is dramatically smaller than what we have headroom for and is Apache-2.0 with no caveats. The 14B MoE is higher quality but uncomfortably tight on 24 GB.
- **HunyuanVideo 1.5** is technically excellent but has a license that excludes EU/UK/South Korea distribution and caps commercial use at 100 M MAU — not a problem for a single-user proxy, but it's a tripwire if Tim ever exposes the endpoint to others.
- **CogVideoX-5B** is mature but at ≤6 s / 720×480 it's effectively a previous generation. The 2B variant is Apache-2.0 if license matters more than quality.
- **Mochi 1** is Apache-2.0 and was a 2024 SOTA, but Genmo has not shipped meaningful updates and the community has largely moved to Wan/LTX. Skip.

Tim's "~140 s for 720p 5-second video" data point is **plausible but optimistic**: it lines up with LTX-2 distilled variants on a 4090 ([wavespeed quickstart](https://wavespeed.ai/blog/posts/blog-ltx-2-comfyui-quickstart/), [insiderllm](https://insiderllm.com/guides/local-ai-video-generation/) cites "5-second clip in 4 seconds on an RTX 4090" for distilled LTX), but full-quality 13B LTX on a 3090 is closer to 2–11 minutes depending on resolution and step count ([Facebook ComfyUI group, 2026-01-23](https://www.facebook.com/groups/comfyui/posts/882350594537642/), [Patreon Silent Snow](https://www.patreon.com/posts/ltx-2-3-in-first-152406236) reports 45–60 min at 24 GB / 128 GB RAM at full quality I2V). Treat 2–6 minutes as the realistic working envelope.

## 3. Recommended model: **Wan 2.2 TI2V-5B (FP8) as primary, LTX-2.3 distilled as fast fallback**

> **2026-05-07 update — see Addendum at end of doc.** Re-ranked under quality-first criteria with offloading allowed; the recommended primary changes to **LTX-2.3 13B (full, FP8 + weight streaming)**. Wan 2.2 TI2V-5B becomes the conservative-fallback alias.
>
> **2026-05-07 second update — see Addendum 2.** Reconciled with `gpu_budget.sh`: GPU0 has only ~8–10 GB usable for video, not 24 GB. Combined with the 3090's lack of native FP8 hardware, the recommended primary changes again — to **LTX-2.3 13B GGUF Q8 with block-swap** (peak ~9–11 GB, ~3–5 min/720p/5s), with LTX-2.3 distilled 2B as the fast preview alias.

Justification:

1. **VRAM headroom on GPU0 next to ComfyUI image-gen.** Wan 2.2 5B at FP8 fits in 8–10 GB ([dev.to](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)). With ComfyUI image-gen (flux2-klein-4b ~6–8 GB) we're still under 24 GB on GPU0 if both ever overlap, though we should not run them simultaneously (see §7).
2. **Latency.** ~3–6 min for 720p / 5 s is acceptable for a "request, walk away" UX.
3. **License.** Apache-2.0, no territorial or MAU clauses ([Wan2.2 GitHub](https://github.com/Wan-Video/Wan2.2)). Future-safe.
4. **ComfyUI first-class.** Native workflow templates exist ([Comfy docs](https://docs.comfy.org/tutorials/video/wan/wan2_2)). Reuses the ComfyUI process we already operate.
5. **Both T2V and I2V in one model** (TI2V = Text-and-Image-to-Video). Avoids loading two checkpoints.

LTX-2.3 distilled is the recommended **fast** model alias for short-iteration workflows where Tim wants ~30–60 s previews ([crepal](https://crepal.ai/blog/aivideo/blog-ltx-2-vram-requirements/)). It's added as a second model entry, not a separate service.

We are **not** recommending Wan 2.2 14B at this time: the [community thread on 24 GB VRAM](https://www.reddit.com/r/StableDiffusion/comments/1pfuhvz/best_wan_22_models_and_workflows_for_24gb/) describes it as workable with offloading but slow and OOM-prone. Tim already lost evenings to VRAM-edge crashes on the LLM side — don't volunteer for more.

We are **not** recommending HunyuanVideo as primary because of the license; we can revisit it as a `model: "hunyuan-video-1.5"` opt-in for personal use only.

## 4. Recommended integration path: extend the **existing ComfyUI** with new workflows

The proxy already routes `/v1/images/*` to ComfyUI. Adding video means: drop the model files into `ComfyUI/models/`, add new workflow JSONs, and have `proxy.py` POST to ComfyUI's existing `/prompt` API on a separate workflow ID. No new daemons.

| Path | Pros | Cons |
|---|---|---|
| **A. Extend existing ComfyUI** (recommended) | Reuses already-running process and GPU pinning. One operational surface. ComfyUI has first-class Wan 2.2 and LTX nodes. Already integrated with the proxy. | ComfyUI's queue is single-threaded by default; image and video requests serialise. Mitigated by long-tail nature of video reqs. |
| B. Run **Wan2GP** as a second service | Easy-button install, aggressive RAM offloading built in ([Wan2GP README](https://github.com/deepbeepmeep/Wan2GP)), supports Wan/LTX/Hunyuan with a single config knob. | Adds a Gradio/HTTP service to babysit. Wan2GP's API is not OpenAI-shaped — proxy.py would need a new adapter. Duplicate model files unless we symlink. |
| C. Dedicated video server (vLLM-style) | Cleanest separation. | Nothing comparable to vLLM exists for diffusion video models in May 2026. Would mean writing one. Not worth it. |

**Pick A.** Wan2GP is the right tool when you don't already have ComfyUI; we do. The pinokio-style automation is solving a problem we already solved in a different way.

If we ever outgrow ComfyUI's serialised queue (e.g. concurrent image + video), switching to Wan2GP-as-second-service is a one-week refactor, not a rewrite. Not yet.

## 5. API surface

The OpenAI-compatible community has converged on Sora 2's shape: `POST /v1/videos` with async polling at `GET /v1/videos/{video_id}` and content fetch at `GET /v1/videos/{video_id}/content` ([OpenAI docs](https://developers.openai.com/api/docs/guides/video-generation), [LaoZhang blog 2026-03-28](https://blog.laozhang.ai/en/posts/openai-sora-api), [Azure Foundry](https://timhanewich.medium.com/how-to-use-the-sora-api-for-video-genein-azure-openai-service-azure-ai-foundry-86fa79da94b4)). We mirror that.

### Proposed endpoints

```
POST   /v1/videos                       Create job
GET    /v1/videos/{id}                  Poll status
GET    /v1/videos/{id}/content          Download MP4 (binary)
DELETE /v1/videos/{id}                  Cancel / cleanup
GET    /v1/videos                       List recent jobs (debug)
```

### Request body (POST /v1/videos)

Sora's actual payload uses `prompt`, `model`, `size`, `seconds`, `n_variants` ([Sora 2 prompting guide](https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide), [Azure example](https://timhanewich.medium.com/how-to-use-the-sora-api-for-video-genein-azure-openai-service-azure-ai-foundry-86fa79da94b4)). Mirror it exactly so OpenAI client SDKs Just Work:

```json
{
  "model": "wan-2.2-ti2v-5b",
  "prompt": "A red fox walking through a snowy forest at dusk, cinematic",
  "size": "1280x720",
  "seconds": 5,
  "n_variants": 1,
  "input_reference": null,
  "seed": 42
}
```

Notes:
- `model`: `wan-2.2-ti2v-5b` (default), `ltx-2.3-distilled` (fast), optional `hunyuan-video-1.5` later. Map via `proxy_config.yml` exactly like chat-completion model aliases.
- `size`: limited to `768x768`, `1280x720`, `1024x576` initially. Reject anything ComfyUI can't tile.
- `seconds`: int 2–8 for Wan, 2–20 for LTX. Reject above per-model max.
- `input_reference` (optional): URL or base64 image for I2V mode (Wan 2.2 supports this natively).

### Response shape

Sync-looking response with async semantics, matches Sora 2:

```json
{
  "id": "video_2026050722f3a1",
  "object": "video.generation",
  "model": "wan-2.2-ti2v-5b",
  "status": "queued",
  "progress": 0,
  "created_at": 1746556800,
  "expires_at": 1746643200,
  "size": "1280x720",
  "seconds": 5
}
```

`status` transitions: `queued` → `running` → `succeeded` | `failed` | `canceled`.

When `succeeded`, the client fetches `GET /v1/videos/{id}/content` which returns `Content-Type: video/mp4`. We do **not** return base64 in the JSON body — videos at 720p / 5 s are 2–8 MB and base64 inflates the JSON layer for no reason. (OpenAI's own approach: separate `/content` endpoint, [video gen guide](https://developers.openai.com/api/docs/guides/video-generation).)

### Sync vs async

Generation takes 2–10 minutes. Sync HTTP at openai.vivid.fish would mean:
1. Traefik default 60 s timeout — would 504 every request.
2. Browser tabs would just hang.

Two acceptable patterns:

1. **Polling (recommended, matches Sora 2).** Return immediately with `id` + `status: queued`. Client polls `GET /v1/videos/{id}` every 5–10 s. Most OpenAI SDKs already implement this for Sora.
2. **SSE on the same endpoint.** `Accept: text/event-stream` upgrades the response to a streamed sequence of `progress` events ending in a final `succeeded` event. Useful for the SillyTavern UI but secondary; ship polling first.

Job state lives in `data/videos/{id}/` on disk: a `meta.json` with status fields and an `output.mp4` once finished. Survives restarts; cron purges entries older than 24 h.

## 6. Resource accounting

Per-request envelope, isolated:

| Step | GPU | VRAM peak | Disk write | Wall time |
|---|---|---|---|---|
| Wan 2.2 TI2V-5B FP8 load | GPU0 | ~9 GB | 0 (cached) | ~10–30 s cold, ~2 s warm |
| Diffusion (40 steps, 720p, 5 s) | GPU0 | ~12–14 GB peak ([dev.to bench](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)) | latents to disk if offloading | 3–5 min |
| VAE decode + ffmpeg encode | GPU0 + CPU | brief +2–3 GB VAE spike | 2–8 MB MP4 | 5–15 s |
| **Total** | GPU0 | **~14 GB peak**, 9 GB resident | ~10 MB / job + 30 GB one-time models | **3–6 min** |

Disk: Wan 2.2 5B FP8 GGUF ~6 GB; LTX-2.3 distilled GGUF ~5 GB; tokenizer/VAE ~2 GB. Plan for **~30 GB** under `ComfyUI/models/` after we add 1–2 model variants and LoRAs.

Under contention:

- **Image-gen on GPU0 + video on GPU0:** ComfyUI serialises the queue. Image jobs (~10 s with flux2-klein-4b) will block behind a video job in progress. Mitigation in §7.
- **LLM on GPU1 + video on GPU0:** independent GPUs; should be clean. PCIe contention is theoretical; system RAM offloading is on GPU0's NUMA node only.
- **Voxtral TTS / Parakeet STT on GPU0:** these are small (<3 GB) and quick (<1 s for typical chunks). They'll run between video diffusion steps if ComfyUI yields. **Open question (see §9):** whether ComfyUI's GPU lock blocks TTS during a video job; if yes, voice latency suffers during video gen. Need to measure.
- **GPU1's llama-turbo at 20–21 GB:** untouched by video on GPU0. The llama-turbo VRAM-edge crashes Tim has already seen are unrelated and stay our problem on the LLM side.

## 7. Coexistence strategy

Hardware reality: one 24 GB card runs ComfyUI (image + video + TTS + STT), one 24 GB card runs the LLM. We should not pretend we have more.

Recommended policy:

1. **Default:** video runs on GPU0 alongside existing ComfyUI image/TTS/STT. ComfyUI's queue serialises; that's a feature, not a bug, for our scale (single-user workstation).
2. **Image + video coexistence:** rely on ComfyUI's "smart memory" mode to evict the inactive model. Verify that the image checkpoint unloads when a video request comes in and reloads when done. Document the resulting "first image after a video takes 15 s extra" behaviour.
3. **TTS / STT priority:** keep voxtral-tts and parakeet-stt on **separate processes** (they already are) so a stuck ComfyUI queue doesn't kill voice. They each carve out their own VRAM region (~2–3 GB combined). Any ComfyUI model swap must respect this — set ComfyUI's `--reserve-vram 4` or equivalent.
4. **GPU1 fallback (future):** if Tim wants faster video and the LLM is idle, we can opt-in route video to GPU1 via a `priority: gpu1-when-idle` config flag and a systemd `Conflicts=` similar to the existing kobold/turbo arrangement (per Tim's MEMORY.md). Out of scope for v1.
5. **systemd ordering:** no new units. Video generation is a workflow inside the existing `comfyui.service`. The proxy's existing `After=comfyui.service` ordering already covers us.

## 8. Risks

1. **ComfyUI queue head-of-line blocking.** A 5-minute video stalls all image / TTS calls on GPU0. For a single user this is annoying, not fatal. If multi-user, separate the services.
2. **VRAM spikes during VAE decode.** Several community reports cite the VAE as the OOM culprit ([Comfy-Org #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)). The Tiny VAE preview pattern Tim mentioned is real and worth wiring as the seed-preview step.
3. **Model licenses.**
   - Wan 2.2: Apache-2.0, clean ([Wan-Video/Wan2.2](https://github.com/Wan-Video/Wan2.2)).
   - LTX-2.3: OpenRAIL-M-style, with use-case restrictions; check for any "no deepfakes / no defamation" clauses before exposing publicly.
   - HunyuanVideo 1.5: Tencent Community License excludes EU/UK/South Korea distribution and caps at 100 M MAU ([HF LICENSE](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE)). Keep gated behind a personal-use opt-in.
4. **Disk usage.** ~30 GB initial + ~10 MB per job + LoRAs. Plan for 100 GB headroom under `~/applications/comfyui/models/`.
5. **Determinism.** Diffusion video is seed-deterministic only when sampler, scheduler and node graph are byte-identical. Don't promise reproducibility across model upgrades.
6. **NSFW handling.** Wan 2.2 and LTX have soft safety filters; HunyuanVideo's filter is more aggressive. Document policy explicitly in the proxy README.
7. **External exposure (`openai.vivid.fish`).** Long-running jobs are an obvious DoS vector. Even with personal auth, add a per-key concurrency cap of 1 video and a rate limit (e.g. 12/hour). Existing proxy auth machinery is sufficient.
8. **Tim's "140 s" benchmark is for an unrealistically light path.** Be ready to write down "first run took 7 minutes" and not have it feel like a regression.

## 9. Open questions for Tim

1. **Primary use case:** loose 720p/5 s generations for fun, or longer-form (10–20 s) with audio (LTX-2.3 with audio is a ~28–32 GB workflow per [LTX](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos))? This decides whether we add the LTX audio path now or later.
2. **TTS/STT during video gen — acceptable to block briefly, or must voice stay always-responsive?** Drives whether ComfyUI gets exclusive use of GPU0 or whether we move TTS/STT to GPU1 (where llama-turbo currently lives at the VRAM edge).
3. **Do we need I2V (image-to-video) or only T2V?** I2V doubles the test surface but is the more interesting use case for character cards / chats.
4. **Web access policy:** is `openai.vivid.fish/v1/videos` reachable from outside the LAN? If yes, what concurrency cap and what NSFW posture?
5. **Storage retention:** keep videos forever, 24 h, or signed URLs only? Affects disk planning.
6. **HunyuanVideo opt-in:** willing to accept the territorial license for personal use, or skip it entirely?
7. **OK with polling-only v1?** SSE / progress streaming adds work; happy to defer if polling is acceptable.
8. **Budget for one-time setup** (model downloads, workflow tuning): half a day, a weekend, or "as long as it takes"?

## 10. References

- LTX-Video / LTX-2 ecosystem
  - [Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)
  - [LTX-2 VRAM requirements (crepal.ai)](https://crepal.ai/blog/aivideo/blog-ltx-2-vram-requirements/)
  - [LTX-2 / WaveSpeed VRAM reality check](https://wavespeed.ai/blog/posts/blog-ltx-2-vram-requirements/)
  - [LTX-2 quickstart — WaveSpeed](https://wavespeed.ai/blog/posts/blog-ltx-2-comfyui-quickstart/)
  - [LTX-2 zenvanriel guide](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/)
  - [LTX 2.3 RTX 3090 demo — YouTube](https://www.youtube.com/watch?v=OL_4VY0h8eQ)
  - [LTX 2.3 first look — Patreon Silent Snow](https://www.patreon.com/posts/ltx-2-3-in-first-152406236)
  - [Studio AI Films LTX-2 deep-dive](https://studio.aifilms.ai/blog/ltx-2-open-source-ai-video-model)
  - [ComfyUI LTX-2 VAE/RAM issue #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)
  - [r/comfyui LTX 2.3 dual-GPU thread (3090 + 4060 Ti)](https://www.reddit.com/r/comfyui/comments/1rr67bc/ltxvideo_23_workflow_for_dualgpu_setups_3090_4060/)
  - [ltx.io 20s video guide](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos)
  - [Facebook ComfyUI group LTX-2 RTX 3090 timing data point](https://www.facebook.com/groups/comfyui/posts/882350594537642/)
- Wan 2.2 ecosystem
  - [Wan-Video/Wan2.2 (Apache-2.0)](https://github.com/Wan-Video/Wan2.2)
  - [Wan2.2-T2V-A14B HF model card](https://huggingface.co/Wan-AI/Wan2.2-T2V-A14B)
  - [ComfyUI Wan 2.2 native workflow](https://docs.comfy.org/tutorials/video/wan/wan2_2)
  - [Wan 2.2 GGUF setup tutorial](https://www.stablediffusiontutorials.com/2025/08/wan-2.2-video-generation.html)
  - [Run Wan 2.2 on 8 GB VRAM (dev.to)](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)
  - [r/StableDiffusion best workflows on 24 GB / 32 GB RAM](https://www.reddit.com/r/StableDiffusion/comments/1pfuhvz/best_wan_22_models_and_workflows_for_24gb/)
  - [r/comfyui Wan 2.2 hardware thread](https://www.reddit.com/r/comfyui/comments/1ogalrt/wan_22_hardware_requirements/)
  - [WAN 2.2 vs LTX-2 — neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/)
  - [LTX 2.3 vs WAN 2.2 — WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/)
  - [Simplismart: serving Wan 2.2 (Apache-2.0)](https://simplismart.ai/blog/deploy-wan-2-2)
- HunyuanVideo
  - [Tencent-Hunyuan/HunyuanVideo-1.5 GitHub](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)
  - [HF model card with VRAM specs](https://huggingface.co/tencent/HunyuanVideo-1.5)
  - [HunyuanVideo 1.5 LICENSE (territorial)](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE)
  - [Licensing summary — DeepWiki](https://deepwiki.com/Tencent-Hunyuan/HunyuanVideo/1.2-licensing)
  - [HunyuanVideo 1.5 hands-on — Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)
  - [arXiv: HunyuanVideo 1.5 technical report](https://arxiv.org/abs/2511.18870)
- CogVideoX / Mochi
  - [zai-org/CogVideo perf optimisations — DeepWiki](https://deepwiki.com/zai-org/CogVideo/7-performance-optimization)
  - [studioinstance/CogVideo licensing](https://github.com/studioinstance/CogVideo-1)
  - [CogVideoX guide — Clore.ai](https://docs.clore.ai/guides/video-generation/cogvideox)
  - [Mochi 1 announcement — Genmo blog](https://www.genmo.ai/blog/mochi-1-a-new-sota-in-open-text-to-video)
  - [Mochi 1 on consumer GPUs — Apatero](https://www.apatero.com/blog/mochi-1-consumer-hardware-comfyui-optimization-guide-2025)
- Wan2GP / offloading tooling
  - [deepbeepmeep/Wan2GP](https://github.com/deepbeepmeep/Wan2GP)
  - [deepbeepmeep/mmgp memory manager](https://github.com/deepbeepmeep/mmgp)
  - [WanGP webui guide — Apatero](https://apatero.com/blog/wangp-webui-complete-guide-low-vram-video-2025)
  - [BrightCoding Wan2GP overview](https://www.blog.brightcoding.dev/2025/09/17/open-source-video-generation-for-low-vram-gpus-how-wan2gp-puts-cinematic-ai-in-reach-of-the-gpu-poor/)
- API conventions
  - [OpenAI Sora video generation guide](https://developers.openai.com/api/docs/guides/video-generation)
  - [Sora 2 prompting guide](https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide)
  - [Apidog: Sora 2 / Sora 2 Pro API](https://apidog.com/blog/sora-2-pro-api/)
  - [LaoZhang AI: OpenAI Videos API in 2026](https://blog.laozhang.ai/en/posts/openai-sora-api)
  - [Azure OpenAI Sora — Tim Hanewich Medium](https://timhanewich.medium.com/how-to-use-the-sora-api-for-video-genein-azure-openai-service-azure-ai-foundry-86fa79da94b4)
  - [Microsoft Foundry Sora 2 docs](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/video-generation)
  - [fal.ai Kling 2.1 image-to-video API shape](https://fal.ai/models/fal-ai/kling-video/v2.1/master/image-to-video/api)
- Cross-cutting reviews / benchmarks
  - [Video generation comparison — Clore.ai](https://docs.clore.ai/guides/comparisons/video-gen-comparison)
  - [Video generation VRAM requirements 2026](https://willitrunai.com/blog/video-generation-gpu-guide-2026)
  - [Top 6 ComfyUI text-to-video — Apatero](https://apatero.com/blog/top-6-comfyui-text-to-video-models-you-need-to-try-in-2025-the-ultimate-performance-guide)
  - [Local AI video generation in 2026 — InsiderLLM](https://insiderllm.com/guides/local-ai-video-generation/)
  - [Top open-source text-to-video — Modal blog](https://modal.com/blog/text-to-video-ai-article)

---

## Addendum: Quality-first re-evaluation (2026-05-07)

The original ranking optimised for **VRAM safety, license, stability, quality, latency** — in that order. Tim's pushback (paraphrased: "LTX is supposedly way better, so why isn't that the primary?") is fair. This addendum re-ranks with **quality, ecosystem activity, latency, VRAM (offloading allowed), license, stability**, treating dynamic VRAM offloading as a first-class fit strategy on a 3090 with 64 GB+ system RAM.

### A. Reframed criteria

| # | Criterion | Why it moved |
|---|---|---|
| 1 | **Output quality** | This is the actual user-felt deliverable. We have a 3090, not a phone. |
| 2 | **Ecosystem activity** | A model that gets weekly LoRAs / nodes / fixes ages well; a stalled model decays. |
| 3 | **Latency under our hardware** | "Acceptable" not "fast." 3–8 min per clip is fine on a personal proxy. |
| 4 | **VRAM with offloading allowed** | Block-swap and weight-streaming are mature; we should use them. |
| 5 | **License** | Still matters for `openai.vivid.fish` if exposed; demoted because we can gate per-model. |
| 6 | **Stability** | Demoted but not ignored — a model that OOMs on the second sampler is unusable regardless of quality. |

### B. Quality comparison (Mar–Apr 2026 sources)

| Attribute | Winner | Loser | Source |
|---|---|---|---|
| Cinematic camera language (dolly, tilt, parallax) | **Wan 2.2** | LTX-2.3 ("kept framing steady"; LTX wobbled on camera moves) | [WaveSpeed Mar-2026 head-to-head](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) |
| Fine micro-texture (fabric, skin, wood grain) | **Wan 2.2** | LTX-2.3 (softens textures under busy motion) | [WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) |
| Multi-subject prompt fidelity | **Wan 2.2 (MoE)** | LTX-2 | [neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/) |
| Native synced audio in one pass | **LTX-2.3** | Wan 2.2 (needs S2V add-on), Hunyuan | [neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/), [zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) |
| Speed at equivalent settings | **LTX-2.3** (10–14× Wan; "18×" once on simple) | Wan 2.2 | [WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) |
| 1080p+ resolution ceiling | **LTX-2.3** (stable 720p, ok-ish 1080p), **Hunyuan 1.5** (native 1080p via two-stage SR) | Wan 2.2 (needs babysitting >768p) | [WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/), [Quasa Hunyuan 1.5](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) |
| Geometric / multi-view consistency (static scenes, GeCo benchmark, Apr-2026) | **Wan 2.2** = best open-source overall; HunyuanVideo and LTX-Video trail | — | [arXiv 2512.22274 GeCo](https://arxiv.org/html/2512.22274v2) Table 5 |
| Motion coherence vs closed leaders (VBench, T2V-Score, GenAI-Arena) | **HunyuanVideo 1.5** neck-and-neck with Kling 1.5; ahead of Runway Gen-3 / Luma / Pika 1.5 | — | [Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) (3rd-party summary; primary VBench numbers not yet replicated by an independent run we found) |
| Long-form (>10 s) | **LTX-2.3** (4K @ 50 fps, ~20 s; 40 s with audio reported) | Wan 2.2 (5–8 s practical), Hunyuan (5–10 s) | [ltx.io 20s guide](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos), [FB ComfyUI Mar-2026](https://www.facebook.com/groups/592850912909945/posts/1295579505970412/) |

**Where sources disagree:** WaveSpeed and neurocanvas both say Wan 2.2 wins cinematic quality but disagree on whether LTX is *competitive* on motion (WaveSpeed: "snappier but floaty"; neurocanvas: "AI float"). The Quasa Hunyuan article overstates Hunyuan's license (claims "Apache 2.0"); the [HF LICENSE file](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE) is the Tencent Community License with territorial exclusions. Trust the LICENSE, not the blog. The GeCo benchmark is narrow (static scenes, slow camera) and doesn't generalise to character motion.

**Net read:** there is no single quality winner. Wan 2.2 wins **cinematic photoreal**; LTX-2.3 wins **iteration speed, audio, longer clips, prompt-iteration UX**; Hunyuan 1.5 wins **headroom-per-VRAM and human-preference benchmarks** but the ecosystem is younger and the license is gated.

### C. Dynamic VRAM offloading: what it costs and what it enables

ComfyUI offers two distinct mechanisms, often confused:

1. **Block swap** — a model-architecture-aware node from kijai's wrappers ([WanVideoBlockSwap in `ComfyUI-WanVideoWrapper`](https://github.com/kijai/ComfyUI-WanVideoWrapper), [HyVideoBlockSwap in `ComfyUI-HunyuanVideoWrapper`](https://www.runcomfy.com/comfyui-nodes/ComfyUI-HunyuanVideoWrapper/hy-video-block-swap)) that holds N transformer blocks (range 0–40) in CPU RAM and streams each block to VRAM only when its forward pass executes. The HunyuanVideo node also has independent `offload_txt_in` / `offload_img_in` toggles for the text/image embedding layers.
2. **Weight streaming / `--novram` smart memory** — ComfyUI core's generic mode that keeps weights in RAM and pages whole modules per sampler step. This is what LTX-2.3 native workflows use ("With ComfyUI, it can use weight streaming (offloading), so it can work on less than 32GB VRAM too, but can be significantly slower" — [Lightricks RTX Quick Start, Jan-2026](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/)).

**Latency penalty (3090, PCIe 4.0 x16, DDR4 64 GB):**

- A 1 GB tensor moves CPU↔GPU in ~50 ms over PCIe 4.0 x16 (theoretical 32 GB/s; practical ~20 GB/s). A 14B-class video transformer has 30–40 swappable blocks × ~0.5–1 GB each. For Wan 2.2 14B on 24 GB with `block_swap=10`, the [r/StableDiffusion Aug-2025 thread](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/) reports **>40 minutes** to render 81 frames @ 720p — i.e., the swap dominates wall time once you push past ~20 blocks.
- For Wan 2.2 5B (no swap needed), `~3–5 min` is the unoffloaded baseline (existing §6).
- For LTX-2.3 13B FP8 with weight streaming on 64 GB RAM, real-world numbers from January 2026: **2:10 for 720p / 5 s on RTX 3090 + 64 GB RAM** ([FB ComfyUI group, Jan-2026](https://www.facebook.com/groups/comfyui/posts/884945090944859/)); **~6:43 for 720p with audio** ([FB ComfyUI Mar-2026](https://www.facebook.com/groups/592850912909945/posts/1295579505970412/)). The `~140 s` Tim cited is consistent with these.
- HunyuanVideo 1.5 native on 3090 Ti with TensorRT and offloading: **<4 min for 1080p / 5 s** ([Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)). 13.6 GB BF16 fits cleanly in 24 GB so swap is mostly unnecessary; this is the cheapest-to-run quality model on this list.

**Architecture suitability for swap:**

- LTX-2 / LTX-2.3 is a **DiT** with relatively independent transformer blocks; the Lightricks ComfyUI nodes ship weight-streaming-friendly workflows out of the box ([Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)). The known fragility is *not* in the transformer — it's in the second-stage upscaler / VAE step, where ComfyUI's smart memory fails to release VRAM between samplers and OOMs at 1080p ([Comfy-Org #11726, Jan-2026](https://github.com/Comfy-Org/ComfyUI/issues/11726)). On a 3090 with 24 GB and `--cache-none --disable-smart-memory`, this is a workaround; on 16 GB it's unworkable.
- Wan 2.2 14B MoE swaps "well" architecturally but the *combined* model is huge and 10+ blocks of swap pushes per-step time past breaking ([r/StableDiffusion Aug-2025](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/)). The kijai wrapper also has a documented memory-leak class of bug where RAM creeps across sequential runs ([WanVideoWrapper #805, Jul-2025](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805); [#1644 BlockSwap broken, Nov-2025](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1644)) — operational hazard on a long-lived ComfyUI process.
- HunyuanVideo's BlockSwap (`HyVideoBlockSwap`, kijai) is mature and rarely needed at 24 GB since the model is 13.6 GB BF16.

**Verdict:** offloading is a real, sourced fit strategy and lets us run **LTX-2.3 13B** and **HunyuanVideo 1.5** comfortably on a 3090. It does **not** rescue Wan 2.2 14B for interactive use — the >40-minute number is not interactive at any UX.

### D. Re-ranked recommendation

Honest scoring under the new criteria (1 = bad, 5 = great):

| Model | Quality | Ecosystem | Latency on 3090 | VRAM fit (offload OK) | License | Stability | Notes |
|---|---|---|---|---|---|---|---|
| **LTX-2.3 13B (full, FP8 + weight streaming)** | 4 | 5 | 4 (~2–7 min/5 s 720p) | 4 (FP8 ~14–20 GB; streaming on 24 GB clean) | 3 (OpenRAIL-M, use restrictions) | 3 (sampler/VAE OOM at 1080p, [#11726](https://github.com/Comfy-Org/ComfyUI/issues/11726); 720p stable) | Native audio. Up to 20 s. Lightricks ships first-party nodes ([Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)). |
| **HunyuanVideo 1.5** | 5 (per-VBench/Arena claims) | 3 (younger, fewer LoRAs) | 4 (<4 min/5 s 1080p, 3090 Ti TensorRT) | 5 (13.6 GB BF16, no swap needed) | 2 (territorial Tencent license, MAU cap) | 4 | Best quality-per-VRAM. Gated to personal use. |
| **Wan 2.2 14B MoE** | 5 (cinematic / camera control) | 5 | 1 (>40 min/5 s 720p with `block_swap=10`) | 2 (24 GB is OOM-edge even with swap) | 5 (Apache-2.0) | 2 (BlockSwap leaks, [#805](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805) / [#1644](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1644)) | Best photoreal but disqualified on latency for interactive use. |
| **Wan 2.2 TI2V-5B** (original primary) | 3 | 5 | 5 (3–6 min/5 s 720p) | 5 (8–10 GB FP8) | 5 | 5 | Solid but quality-capped; can't go past ~768p without trouble. |

**New recommended primary: LTX-2.3 13B (full, FP8 quant, weight streaming).** Reasoning:

1. **Quality.** Beats Wan 2.2 5B on resolution ceiling and longer durations; trades blows with Wan 2.2 14B on detail (Wan wins texture/camera; LTX wins motion fluidity, audio, length). The MoE 14B's slight quality lead doesn't survive its 40-minute latency on a 3090.
2. **Ecosystem.** Lightricks ships first-party ComfyUI nodes weekly; LTX-2.3 was released 2026-03-05 and already has audio + 4K paths.
3. **Latency on a 3090.** 2–7 min for 720p / 5 s with weight streaming and 64 GB RAM is well within the "request and walk away" envelope.
4. **VRAM with offloading.** FP8 13B + weight streaming fits comfortably in 24 GB next to ComfyUI image-gen, which is exactly the fit pattern the r/StableDiffusion 3060 thread describes — except we have 2× the VRAM and 2× the RAM.

**HunyuanVideo 1.5 added as a `model: "hunyuan-video-1.5"` opt-in alias** for personal use; it is genuinely the best-quality option per VBench but the license keeps it off the public-by-default surface. Wan 2.2 TI2V-5B stays as a `model: "wan-2.2-ti2v-5b"` *fallback* alias for cases where stability or photoreal-texture matters more than fluidity. Wan 2.2 14B is **not** added; latency is disqualifying.

API-surface consequence (§5 amendment): per-model `seconds` caps tighten — `ltx-2.3` accepts 2–20 s, `hunyuan-video-1.5` 2–10 s, `wan-2.2-ti2v-5b` 2–8 s. The proxy must reject above per-model max.

### E. The r/StableDiffusion 3060 thread — what it tells us about a 3090

Direct read of the thread Tim cited ([r/StableDiffusion 3060 12 GB / 32 GB RAM](https://www.reddit.com/r/StableDiffusion/comments/1t5tq7e/recommendation_for_rtx_3060_12_vram_32_gb_ram/), 2026-05; the page is gated to logged-in Reddit but the search-engine snippets are explicit) and corroborating sources:

- The headline data point — "5-second 1920×800 in 280 s, 720p in 140 s on LTX 2.3" — is from a 3060 12 GB / 32 GB RAM build using Q4–Q6 GGUF quants of LTX-2.3 with Wan2GP-style aggressive offload, Sage attention, and Tiny VAE preview. Confirmed by the [zenvanriel guide](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) and the [Lightricks RTX Quick Start](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/) ("works on less than 32 GB VRAM too, but can be significantly slower").
- 3060 users are solving for **fit**: just-make-it-run. They accept Q4 quants, distilled variants, Tiny VAE previews, and 8 GB-class compromises because the alternative is can't-run-at-all.
- A 3090 has **2× the VRAM and 2× the RAM**. We don't have to choose. We can run **FP8 13B LTX-2.3 (not Q4 distilled)** with weight streaming and get better quality than the 3060 user, at comparable wall-time to their distilled Q4 — because we never have to compromise on quant.
- The techniques the 3060 thread uses (Sage attention, Tiny VAE previews, GGUF quants, Wan2GP) are still useful on a 3090 as **optimisations**, not survival kit. Sage attention + FP8 + weight streaming on a 3090 is the right baseline for LTX-2.3 13B; we can drop Wan2GP entirely (per §4 of original; ComfyUI is already running).

**Translation for our 3090 build:** the 3060 thread proves LTX-2.3 13B is operable on 12 GB with offload — so 24 GB is a comfortable home for it, not a tight squeeze.

### F. Open-questions delta

The original §9 questions still apply. New / promoted ones from quality-first reranking:

1. **I2V vs T2V split (was Q3, now Q1).** Wan TI2V-5B handled both natively; LTX-2.3 has separate I2V and T2V workflows with different VRAM profiles ([Lightricks/ComfyUI-LTXVideo example_workflows](https://github.com/Lightricks/ComfyUI-LTXVideo)). Need to decide: ship both `ltx-2.3-t2v` and `ltx-2.3-i2v` as model aliases, or one workflow with mode switching?
2. **HunyuanVideo as default-or-opt-in.** If Tim is fine with a personal-use license gate (no public exposure), Hunyuan 1.5 is arguably *the* primary on quality. Want it gated behind `auth.role=admin`, or off entirely?
3. **VAE-OOM workaround for LTX-2.3 1080p ([Comfy-Org #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)).** Acceptable to cap LTX output at 720p in v1, or invest in the `--cache-none --disable-smart-memory` workaround now?
4. **Audio path on LTX-2.3.** Native single-pass audio is the killer feature vs Wan and Hunyuan. Worth wiring as an opt-in `with_audio: true` parameter in v1, or defer?
5. **Long-form ceiling.** LTX up to 20 s changes the per-model `seconds` cap and the disk-retention story (one 20 s clip ≈ 30 MB, not 8 MB). Confirm 24 h retention still right.

(Original §9 Q1 — "longer-form with audio" — collapses into Q4 above. Q3 promotes to Q1. Everything else stands.)

### G. Sources added in this addendum

- [WaveSpeed: LTX-2.3 vs WAN 2.2 head-to-head, Mar-2026](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) — primary quality comparison, 4090 test bench.
- [neurocanvas: WAN 2.2 vs LTX-2 winners-by-attribute](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/) — corroborates WaveSpeed; explicit "tight squeeze" verdict on 14B at 24 GB.
- [arXiv 2512.22274 GeCo geometric-consistency benchmark, Apr-2026](https://arxiv.org/html/2512.22274v2) — independent academic comparison of WAN 2.2, HunyuanVideo, LTX-Video, CogVideoX vs Sora 2 / Veo 3.1.
- [Quasa: HunyuanVideo 1.5 hands-on](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) — VBench / GenAI-Arena claims (note license error in the article).
- [zenvanriel: LTX-2.3 deployment guide](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) — 12 GB minimum, 16 GB+ comfortable, FP8 sweet spot.
- [Comfy-Org/ComfyUI #11726 LTX-2 inter-sampler VRAM bug, Jan-2026](https://github.com/Comfy-Org/ComfyUI/issues/11726) — known fragility.
- [kijai/ComfyUI-WanVideoWrapper #805 BlockSwap RAM leak](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805) and [#1644 BlockSwap broken Nov-2025](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1644) — operational hazards for Wan 2.2 14B path.
- [r/StableDiffusion: best Wan 2.2 t2v settings](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/) — 81 frames @ 720p with `block_swap=10` >40 min on 24 GB.
- [r/StableDiffusion: Lightricks LTX-2 RTX Quick Start, Jan-2026](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/) — "weight streaming works on <32 GB VRAM, significantly slower."
- [FB ComfyUI group: LTX-2 720p / 3090 / 64 GB / 2:10, Jan-2026](https://www.facebook.com/groups/comfyui/posts/884945090944859/) and [Mar-2026 audio test, 403 s on 3090](https://www.facebook.com/groups/592850912909945/posts/1295579505970412/) — concrete 3090 wall-times.
- [RunComfy: HyVideoBlockSwap node reference](https://www.runcomfy.com/comfyui-nodes/ComfyUI-HunyuanVideoWrapper/hy-video-block-swap) — block-swap mechanics (0–40 single blocks, txt/img layer offloads).
- [Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) — first-party LTX nodes / example workflows.
- [Spheron GPU cheat sheet 2026](https://www.spheron.network/blog/gpu-requirements-cheat-sheet-2026/) — datacenter VRAM numbers for Wan 2.2 720p (65–80 GB H100) confirming 14B is *native*-unfit on 24 GB.

---

## Addendum 2: Reconciled with gpu_budget.sh (2026-05-07)

The first addendum framed the budget as "a 24 GB card." That is the card's total, not what's *available* to a video workload given the services already pinned to GPU0. Reading the actual budget changes the recommendation.

### A. The actual budget

From `~/applications/config/gpu_budget.sh`:

```
GPU0_DESKTOP=3       # Xorg / window manager
GPU0_TTS=4           # voxtral / TheMHD1 int4
GPU0_COMFYUI=2       # ComfyUI baseline (image-gen idle)
GPU0_HEADROOM=15     # absorbs ComfyUI peaks (flux-dev 8–12 GB)

GPU1_LLM=23          # llama-turbo
GPU1_HEADROOM=1
```

Two binding constraints:

1. **GPU0 peak budget for video = 8–10 GB**, not 24 GB. The 15 GB "headroom" is already earmarked for ComfyUI image-gen peaks (flux-dev hits 8–12 GB per the file's own comment). A video workload that consumes the full 15 GB excludes image-gen entirely; the practical sustainable share, assuming we want to keep image-gen working in the same ComfyUI process, is **8–10 GB peak**. Live `nvidia-smi` confirms ~15 GB used / ~8.5 GB free at idle.
2. **GPU1 is unavailable** — owned by llama-turbo at 23 GB. We don't touch it.
3. **System RAM ≥ 64 GB free** for offload — well above the 32 GB the 3060 build in [r/StableDiffusion 1t5tq7e](https://www.reddit.com/r/StableDiffusion/comments/1t5tq7e/recommendation_for_rtx_3060_12_vram_32_gb_ram/) used to run LTX-2.3.

The budget *does* permit 14–20 GB resident if we're willing to suspend image-gen during a video job — but the §7 coexistence policy explicitly wants both available. So 8–10 GB is the right working ceiling.

### B. Per-model peak VRAM and latency under the real budget

All numbers are 720p / 5 s on a 3090 (Ampere — **no native FP8**; FP8 weights run at FP16 speed per [Comfy-Org #2970 community benchmarks](https://github.com/Comfy-Org/ComfyUI/discussions/2970)). Quality vs. fully-resident is unchanged for all candidates: offloading affects timing only ([ComfyUI async offload blog post, Dec-2025](https://blog.comfy.org/p/new-comfyui-optimizations-for-nvidia)).

| Candidate | Peak VRAM (aggressive offload) | Wall time, 720p/5s on 3090 | Source |
|---|---|---|---|
| **LTX-2.3 13B GGUF Q8 + block-swap** | ~9–11 GB | ~3–5 min | [unsloth/LTX-2.3-GGUF](https://huggingface.co/unsloth/LTX-2.3-GGUF), [civitai 2443867 12GB GGUF workflow](https://civitai.com/models/2443867/ltx-23-22b-gguf-workflows-12gb-vram), [HF Lightricks/LTX-2.3 #16](https://huggingface.co/Lightricks/LTX-2.3/discussions/16) (5090 FP8 = 82 s / 481 frames @ 720p; 3090 ≈ 2.5–3× slower) |
| **LTX-2.3 13B FP8 + weight streaming** (prior recommendation) | ~14–20 GB | ~2:10 reported on 3090 / 64 GB RAM | [FB ComfyUI Jan-2026](https://www.facebook.com/groups/comfyui/posts/884945090944859/) — but exceeds the 8–10 GB GPU0 budget; would have to evict ComfyUI image-gen first |
| **LTX-2.3 distilled 2B Q4 GGUF** | ~5–7 GB | ~30–90 s ([wavespeed quickstart](https://wavespeed.ai/blog/posts/blog-ltx-2-comfyui-quickstart/)) | Fits cleanly under image-gen co-tenancy |
| **Wan 2.2 TI2V-5B GGUF Q8** (3090-correct quant) | ~8–10 GB resident | ~5–10 min ([dev.to 8 GB workflow](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6); Q8 on 3060/3080-class typically 7 min @ 1024×574 per [HF Wan2.2-Animate #4](https://huggingface.co/Wan-AI/Wan2.2-Animate-14B/discussions/4)) | At the budget edge |
| **Wan 2.2 TI2V-5B FP8** (note: no 3090 hw acceleration) | ~8–10 GB resident | ~7–12 min — FP8 slower than Q8 on 3090 per [Comfy-Org #2970](https://github.com/Comfy-Org/ComfyUI/discussions/2970) ("Q8 results are slower than FP8 on the 3090, which is actually running at the same speed as FP16") | **Disqualified** — FP8 is the wrong quant for Ampere |
| **HunyuanVideo 1.5 BF16 + HyVideoBlockSwap** | 13.6 GB native; with `block_swap=20` ~7–9 GB | ~6–10 min on 3090 (no TensorRT); the [Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) "<4 min" figure is 3090 Ti + TensorRT, not vanilla 3090 | License-gated (territorial) |

### C. Offload-mode latency penalty

The penalty for streaming weights vs. fully resident on a 3090 with PCIe 4.0 x16 + DDR4:

- **Flux-DEV reference (image, partial unload)**: 16.71 s resident → 29.81 s with partial unload — **1.78× penalty** ([Comfy-Org #9002](https://github.com/Comfy-Org/ComfyUI/discussions/9002)).
- **ComfyUI's own async-offload + pinned-memory path** is enabled by default since Dec 2025 and "broadly applicable to all NVIDIA GPUs" ([ComfyUI blog Dec-2025](https://blog.comfy.org/p/new-comfyui-optimizations-for-nvidia)).
- **LTX-2.3 architecture-specific**: the DiT block layout is swap-friendly (per [Lightricks RTX Quick Start](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/) — "weight streaming works on <32 GB VRAM, significantly slower"). Combined with the 3060 thread evidence (LTX-2.3 Q4 on 12 GB / 32 GB RAM = ~140 s for 720p / 5 s), a 3090 with 64 GB RAM running Q8 with light block-swap should finish in **~3–5 min** — about 1.5–2.5× the resident baseline of ~2:10, well within the [Flux 1.78×] envelope.
- **Wan 2.2 14B with `block_swap=10`** is the cautionary case: >40 min for 81 frames @ 720p ([r/StableDiffusion 1mfzvl5](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/)) — confirms swap is fine for moderate-sized DiTs and disastrous for very large MoE.

**Bottom line: 1.5–2× penalty is the realistic case for LTX-2.3 13B with block-swap on a 3090. The 6–10× catastrophic case only shows up for the Wan 14B MoE — not on the table here.**

### D. Order-of-magnitude check (Tim's hard constraint)

| Comparison | Ratio | Verdict |
|---|---|---|
| LTX-13B Q8 + block-swap (~3–5 min) vs LTX-13B FP8 resident (~2:10) | ~1.5–2× | **In tolerance** |
| LTX-13B Q8 + block-swap (~3–5 min) vs Wan-5B Q8 resident (~5–10 min) | LTX is **faster**, not slower | Trivially in tolerance |
| LTX-13B Q8 + block-swap (~3–5 min) vs Cloud Sora 2 (~30–60 s) | ~5× cloud-faster | Outside our control; not the comparison Tim flagged |
| Wan-14B with `block_swap=10` (>40 min) vs anything | 8–20× | **Order-of-magnitude blow-up — disqualified, as already flagged in addendum 1** |

The recommendation below stays inside the "no order-of-magnitude regression" envelope.

### E. New recommended primary: **LTX-2.3 13B GGUF Q8 + block-swap**, with LTX-2.3 distilled 2B as the preview alias

Reasoning:

1. **Quality.** Same model as addendum 1's recommendation — Q8 GGUF preserves quality vs FP8/BF16 within community-reported "5–8% softening of fine detail, identical motion coherence" ([dev.to LTX guide](https://www.vgoodslab.com/run-ltx-video-on-12gb-vram-a-step-by-step-comfyui-gguf-workflow-guide/), [zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/)). On a 3090, **Q8 is the right quant** because FP8 has no hardware acceleration on Ampere ([Comfy-Org #2970](https://github.com/Comfy-Org/ComfyUI/discussions/2970)).
2. **Fits the actual budget.** Q8 13B with block-swap holds ~9–11 GB peak — inside the 8–10 GB working ceiling with modest tightening (or the full 15 GB headroom if image-gen is suspended for the request).
3. **Latency stays in the 3–5 min envelope** for 720p / 5 s. ~1.5–2× the FP8-resident baseline — within "no order-of-magnitude regression."
4. **Co-tenancy with image-gen survives.** Ample RAM (64 GB) absorbs the swapped blocks; ComfyUI's `--async-offload` is on by default; the [Comfy-Org #11726 VAE/smart-memory bug](https://github.com/Comfy-Org/ComfyUI/issues/11726) still applies at 1080p so cap LTX output at 720p in v1 (already an open question, Q3 of addendum 1 §F).

**Aliases (updated from §3 / addendum 1):**

- `ltx-2.3` (default) — 13B GGUF Q8 + block-swap, 720p, 2–10 s.
- `ltx-2.3-fast` (preview) — distilled 2B Q4, 720p, 30–90 s.
- `wan-2.2-ti2v-5b` (fallback for cinematic / texture-heavy) — GGUF Q8, not FP8, ~5–10 min.
- `hunyuan-video-1.5` (opt-in, license-gated) — BF16 + HyVideoBlockSwap, ~6–10 min.

Wan 2.2 14B remains disqualified (latency).

### F. Open-questions delta (append-only)

Adding to §9 / addendum 1 §F:

9. **GPU0 budget realism.** Should video generation hold to the 8–10 GB working ceiling (image-gen co-tenant), or is it acceptable to fully suspend ComfyUI image-gen during a video job and consume the whole 15 GB headroom? The latter unlocks LTX-2.3 13B FP8-resident path (faster — ~2:10 — but Ampere has no FP8 acceleration so on a 3090 it's effectively the same wall time as Q8 anyway).
10. **Quant selection on a 3090.** Confirm: GGUF Q8 over FP8 for all 3090 video workloads, since FP8 has no Ampere hardware path. (This contradicts addendum 1's "FP8 + weight streaming" framing — appended here, not retracted there.)
11. **Block-swap operator-burden.** kijai-wrapper memory leaks ([WanVideoWrapper #805](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805)) are well-documented for Wan; Lightricks's first-party LTX nodes do not have a comparable bug log. Worth periodic ComfyUI restart in cron, or trust the daemon over multi-day uptime?

### G. Sources added in this addendum

- [r/StableDiffusion 1t5tq7e — 3060 12 GB / 32 GB RAM LTX-2.3 thread (gated)](https://www.reddit.com/r/StableDiffusion/comments/1t5tq7e/recommendation_for_rtx_3060_12_vram_32_gb_ram/) — the "140 s for 720p / 5 s" data point. Page is logged-in only; corroborated indirectly via [zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) and [civitai 2443867](https://civitai.com/models/2443867/ltx-23-22b-gguf-workflows-12gb-vram). **Not directly verified.**
- [unsloth/LTX-2.3-GGUF](https://huggingface.co/unsloth/LTX-2.3-GGUF) — Q4..Q8 GGUF quants of LTX-2.3.
- [HF Lightricks/LTX-2.3 discussion #16 — Render Times](https://huggingface.co/Lightricks/LTX-2.3/discussions/16) — RTX 5090 FP8 = 82 s / 481 frames @ 720p; 3090 extrapolation = ~3–4× slower → ~3–5 min for 121 frames @ 720p, consistent with our envelope.
- [Comfy-Org/ComfyUI Discussion #2970 — GPU benchmark thread](https://github.com/Comfy-Org/ComfyUI/discussions/2970) — "3090 doesn't support FP8 and will run at FP16 speed; use GGUF on 3090, FP8 on 5070+." Drives the Q8-not-FP8 selection.
- [Comfy-Org/ComfyUI Discussion #9002 — Flux DEV FP8 5090/4090/3090 benchmark](https://github.com/Comfy-Org/ComfyUI/discussions/9002) — concrete 1.78× partial-unload penalty (16.71 s → 29.81 s).
- [ComfyUI blog — async offload + pinned memory, Dec-2025](https://blog.comfy.org/p/new-comfyui-optimizations-for-nvidia) — `--async-offload` enabled by default for all NVIDIA GPUs.
- [vgoodslab LTX-Video 12 GB GGUF workflow guide](https://www.vgoodslab.com/run-ltx-video-on-12gb-vram-a-step-by-step-comfyui-gguf-workflow-guide/) — operational details for low-VRAM LTX runs (Sage attention, Tiny VAE, --novram).
- [HF Wan2.2-Animate-14B discussion #4](https://huggingface.co/Wan-AI/Wan2.2-Animate-14B/discussions/4) — Wan 2.2 Q8 GGUF on 5060 Ti 16GB = ~7 min for 1024×574 / 5 s, our closest direct Wan-Q8 latency anchor.
