"""Long video with MiniMax H3: the ``h3_chain`` continuity engine.

Same contract as the LTX engine (see ``video_extend``): one request makes one
ComfyUI prompt that renders segment after segment, and each added segment is
pinned to the end of the video so far. What differs is the model arithmetic and
how the end of the video is carried over.

Frame arithmetic (H3, 24 fps, video latent steps of 1, 4, 4, 4, 4 frames):

* a clip has ``17k + 5`` frames;
* the context window is one of 5, 22, 39 or 56 frames (``17m + 5``), which is a
  whole number of latent steps (2, 7, 12, 17);
* an added segment is sampled as ``context + new`` frames. The context part is
  pinned at the head of the segment (never denoised) and cut away after
  decoding, so ``new`` is a multiple of 17.

Continuity (this is the part the community converged on, see the long-video
design note):

* inside one request the context is sliced straight out of the previous
  segment's latent, picture and sound, with no decode and no re-encode. That
  is the lossless path; only a re-roll of an earlier segment would break it,
  and one request never re-rolls;
* when the source is an existing video (an extension) there is no latent, so
  the decoded last frames and the last second of its audio are encoded and
  pinned. This is the lossy path, taken once per extension.

The pinning nodes are ``H3 Motion Context`` (NikoDemon80/ComfyUI-H3-Motion-Context,
pinned commit in docs/long-video-design.md) and ``H3 Motion Context Trim``;
everything else is a core ComfyUI node.
"""

from __future__ import annotations

from typing import Any, Sequence

from gateway.video_extend import (
    BuiltChain,
    ChainEngine,
    ChainSpec,
    PlanError,
    Segment,
    _ref,
)

FPS = 24.0
FRAME_STEP = 17  # frames per block: added segments grow in steps of 17
FRAME_OFFSET = 5  # a clip is FRAME_STEP * k + FRAME_OFFSET frames
CONTEXT_WINDOWS = (56, 39, 22, 5)  # frames Motion Context can pin, longest first
DEFAULT_CONTEXT_FRAMES = 22
AUDIO_CONTEXT_FRAMES = 24  # one second of sound is pinned with the picture
MIN_SHOT_SECONDS = 2.0  # the template workflows' own minimum

UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
TURBO_LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_int8_convrot.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
STEPS = 8
SAMPLER = "res_multistep"
SCHEDULER = "simple"


def context_window(context_seconds: float, fps: float = FPS) -> int:
    """Largest pinnable window (5, 22, 39 or 56 frames) that fits ``context_seconds``."""
    wanted = round(context_seconds * fps)
    for frames in CONTEXT_WINDOWS:
        if frames <= wanted:
            return frames
    return CONTEXT_WINDOWS[-1]


def first_segment_frames(seconds: float, fps: float = FPS) -> int:
    """Frames of a first segment: ``17a + 5`` nearest to ``seconds``."""
    a = max(1, round((seconds * fps - FRAME_OFFSET) / FRAME_STEP))
    return FRAME_STEP * a + FRAME_OFFSET


def _added_blocks(seconds: float, fps: float) -> int:
    return max(1, round(seconds * fps / FRAME_STEP))


def _split_blocks(blocks: int, max_blocks: int) -> list[int]:
    parts = max(1, -(-blocks // max_blocks))
    base, extra = divmod(blocks, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def plan_segments(
    shots: Sequence[tuple[str, float]],
    *,
    fps: float,
    has_source: bool,
    max_segment_seconds: float,
    state_ledger: str | None = None,
) -> list[Segment]:
    """Turn ``[(prompt, seconds), ...]`` into segments (same rules as the LTX planner).

    The first segment of a new video is ``17a + 5`` frames; every added segment
    adds a multiple of 17. A shot longer than ``max_segment_seconds`` is split
    into near-equal segments that share the shot prompt. ``state_ledger`` text
    goes in front of every segment prompt.
    """
    if not shots:
        raise PlanError("at least one shot is required", code="invalid_shots", param="shots")
    max_blocks = max(1, int(max_segment_seconds * fps // FRAME_STEP))
    ledger = (state_ledger or "").strip()
    out: list[Segment] = []
    for index, (prompt, seconds) in enumerate(shots):
        text = f"{ledger}\n\n{prompt}" if ledger else prompt
        if index == 0 and not has_source:
            first = first_segment_frames(min(seconds, max_segment_seconds), fps)
            out.append(Segment(text, first, 0))
            remaining = seconds - first / fps
            if remaining * fps >= FRAME_STEP / 2:
                for blocks in _split_blocks(_added_blocks(remaining, fps), max_blocks):
                    out.append(Segment(text, blocks * FRAME_STEP, 0))
            continue
        for blocks in _split_blocks(_added_blocks(seconds, fps), max_blocks):
            out.append(Segment(text, blocks * FRAME_STEP, index))
    return out


def build_chain_graph(spec: ChainSpec) -> BuiltChain:
    """Build the API-format ComfyUI graph for ``spec`` (an H3 chain)."""
    g: dict[str, Any] = {}

    def add(node_id: str, class_type: str, **inputs: Any) -> list[Any]:
        g[node_id] = {"class_type": class_type, "inputs": inputs}
        return _ref(node_id)

    fps = float(spec.fps)
    width, height = spec.width, spec.height
    context = spec.context_frames or DEFAULT_CONTEXT_FRAMES
    if context not in CONTEXT_WINDOWS:
        raise PlanError(f"context window of {context} frames is not one of {sorted(CONTEXT_WINDOWS)}",
                        code="invalid_value", param="context_seconds")

    unet = add("unet", "UNETLoader", unet_name=UNET, weight_dtype="default")
    model = add("model", "LoraLoaderModelOnly", model=unet, lora_name=TURBO_LORA, strength_model=1.0)
    vae = add("vae", "VAELoader", vae_name=VIDEO_VAE)
    avae = add("avae", "VAELoader", vae_name=AUDIO_VAE)
    clip = add("clip", "CLIPLoader", clip_name=TEXT_ENCODER, type="minimax", device="default")
    sampler = add("sampler", "KSamplerSelect", sampler_name=SAMPLER)
    sigmas = add("sigmas", "BasicScheduler", model=model, scheduler=SCHEDULER, steps=STEPS, denoise=1.0)

    units: list[dict[str, str]] = []
    audio_parts: list[list[Any]] = []
    segments = list(spec.segments)
    keep_audio = True
    previous_sample: list[Any] | None = None  # latent of the segment before, for the lossless path

    if spec.source is not None:
        src = spec.source
        keep_audio = src.has_audio
        loaded = add("src_file", "LoadVideo", file=src.filename)
        add("src_parts", "GetVideoComponents", video=loaded)
        frames = _ref("src_parts", 0)
        if (src.width, src.height) != (width, height):
            frames = add("src_scale", "ImageScale", image=frames, upscale_method="lanczos",
                         width=width, height=height, crop="center")
        have = src.frames
        source_audio = None
        if src.has_audio:
            source_audio = add("src_audio", "TrimAudioDuration", audio=_ref("src_parts", 1),
                               start_index=0.0, duration=src.frames / fps)
            audio_parts.append(source_audio)
    else:
        frames = None
        have = 0
        source_audio = None

    for j, seg in enumerate(segments):
        first_of_video = j == 0 and spec.source is None
        length = seg.frames if first_of_video else context + seg.frames
        prompt_inputs: dict[str, Any] = {}
        if first_of_video and spec.first_frame_image:
            img = add("s0_image", "LoadImage", image=spec.first_frame_image)
            prompt_inputs["first_frame"] = add("s0_fit", "ImageScale", image=img, upscale_method="lanczos",
                                               width=width, height=height, crop="center")
        cond = add(f"s{j}_cond", "MiniMaxH3ImageToVideo", clip=clip, vae=vae, prompt=seg.prompt,
                   width=width, height=height, length=length, **prompt_inputs)
        positive, empty = _ref(cond[0], 0), _ref(cond[0], 1)
        trim_frames: Any = 0
        if not first_of_video:
            pin: dict[str, Any] = {"context_length": str(context), "audio_context_length": AUDIO_CONTEXT_FRAMES}
            if previous_sample is not None:
                # Lossless path: the tail of the previous segment's latent, picture and sound.
                pin["context_latent"] = previous_sample
            else:
                # Extension of an existing video: decoded tail frames and its last second of sound.
                pin["context_frames"] = add(f"s{j}_tail", "ImageFromBatch", image=frames,
                                            batch_index=have - context, length=context)
                if source_audio is not None:
                    pin["audio_vae"] = avae
                    pin["context_audio"] = source_audio
            motion = add(f"s{j}_ctx", "MiniMaxH3MotionContext", conditioning=positive, vae=vae,
                         latent=empty, **pin)
            positive, trim_frames = _ref(motion[0], 0), _ref(motion[0], 1)
        guider = add(f"s{j}_guider", "BasicGuider", model=model, conditioning=positive)
        noise = add(f"s{j}_noise", "RandomNoise", noise_seed=spec.seed + j)
        sample = add(f"s{j}_sample", "SamplerCustomAdvanced", noise=noise, guider=guider,
                     sampler=sampler, sigmas=sigmas, latent_image=empty)
        decoded = add(f"s{j}_decode", "VAEDecode", samples=sample, vae=vae)
        # Cuts the pinned head off picture and sound together, and makes the
        # sound exactly as long as the picture so joins do not drift. A silent
        # source gives a silent result, so its segments skip the audio decode.
        trim_inputs: dict[str, Any] = {}
        if keep_audio:
            trim_inputs["audio"] = add(f"s{j}_adecode", "VAEDecodeAudio", samples=sample, vae=avae)
        trimmed = add(f"s{j}_trim", "MiniMaxH3MotionContextTrim", images=decoded, trim_frames=trim_frames,
                      fps=fps, match_tail=True, **trim_inputs)
        new_frames, new_audio = _ref(trimmed[0], 0), _ref(trimmed[0], 1)
        frames = new_frames if frames is None else add(f"s{j}_cat", "ImageBatch", image1=frames, image2=new_frames)
        have += seg.frames
        if keep_audio:
            audio_parts.append(new_audio)
        units.append({"sample": f"s{j}_sample", "decode": f"s{j}_decode"})
        previous_sample = sample

    audio = None
    if keep_audio and audio_parts:
        audio = audio_parts[0]
        for k, part in enumerate(audio_parts[1:]):
            audio = add(f"audio_cat{k}", "AudioConcat", audio1=audio, audio2=part, direction="after")
    video_inputs: dict[str, Any] = {"images": frames, "fps": fps}
    if audio is not None:
        video_inputs["audio"] = audio
    create = add("create", "CreateVideo", **video_inputs)
    add("save", "SaveVideo", video=create, filename_prefix=spec.filename_prefix, format="mp4", codec="auto")
    return BuiltChain(graph=g, save_node="save", units=units, total_frames=have)


class H3ChainEngine(ChainEngine):
    """MiniMax H3 long video (see the module docstring)."""

    name = "h3_chain"
    model_family = "minimax-h3"
    stride_reason = "h3_17np5_frame_grid"
    min_shot_seconds = MIN_SHOT_SECONDS
    # H3 pins the previous frames as never-denoised conditioning, so there is no strength to set.
    option_keys = ("state_ledger", "context_seconds")
    default_negative = ""  # H3 runs without a negative prompt (turbo LoRA, guidance 1)

    def tail_frames(self, context_seconds: float, fps: float) -> int:
        return context_window(context_seconds, fps)

    def check_source(self, fps: float) -> None:
        if abs(fps - FPS) > 0.5:
            raise PlanError(
                f"the source video is {fps:g} fps; H3 extends 24 fps video. Re-encode it at 24 fps, "
                "or extend it with ltx-2.5-long",
                code="video_fps_unsupported", param="video")

    def plan_segments(self, shots, *, fps, has_source, max_segment_seconds, state_ledger):
        return plan_segments(shots, fps=fps, has_source=has_source,
                             max_segment_seconds=max_segment_seconds, state_ledger=state_ledger)

    def build(self, spec: ChainSpec) -> BuiltChain:
        return build_chain_graph(spec)
