"""Long-video planning and ComfyUI graph building for LTX-2.5.

One request makes one ComfyUI prompt. The prompt renders a first segment (or
loads a source video) and then adds segments one after another. Each added
segment is conditioned on the last ``context`` frames of the video so far, so
motion carries across the join. Nothing here talks to ComfyUI or FastAPI.

Frame arithmetic (LTX-2.5, 8 frames per latent step, first latent is 1 frame):

* a first segment has ``8a + 1`` frames;
* a context tail has ``8c + 1`` frames and encodes to ``c + 1`` latents;
* an added segment is sampled as ``tail + 8m`` frames. The tail part is pinned
  to the real tail with a latent guide, then dropped. ``8m`` new frames remain.

So every added segment adds exactly ``8m`` frames, and the audio of the segment
is cut to the same ``8m`` frames, so audio and video stay the same length.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

FRAME_STEP = 8
DEFAULT_CONTEXT_SECONDS = 1.0
DEFAULT_CONTEXT_STRENGTH = 0.9
DEFAULT_MAX_SEGMENT_SECONDS = 8.0
DEFAULT_MAX_TOTAL_SECONDS = 40.0
MIN_SHOT_SECONDS = 1.0

NEGATIVE_DEFAULT = (
    "text, subtitles, logo, still image, still video, no motion, static, frozen, blurry, "
    "low quality, distorted, bad anatomy, oversaturated, pixelated, low resolution, grainy, "
    "compression artifacts, glitches, watermark, signature, distorted sound, saturated sound"
)
SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"


class PlanError(ValueError):
    """A request cannot be turned into a chain. ``code`` is the API error code."""

    def __init__(self, message: str, *, code: str, param: str | None = None):
        super().__init__(message)
        self.code = code
        self.param = param


@dataclass(frozen=True)
class Segment:
    prompt: str
    frames: int  # frames this segment adds (first segment: its full length, 8a+1)
    shot: int  # index of the storyboard shot this segment belongs to


@dataclass(frozen=True)
class SourceVideo:
    filename: str  # name in ComfyUI's input dir
    frames: int
    fps: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class ChainSpec:
    segments: Sequence[Segment]
    width: int
    height: int
    fps: float
    seed: int
    negative: str = NEGATIVE_DEFAULT
    context_frames_latents: int = 3  # c: tail is 8c+1 frames
    context_strength: float = DEFAULT_CONTEXT_STRENGTH
    source: SourceVideo | None = None
    first_frame_image: str | None = None  # ComfyUI input filename, first segment only
    first_frame_strength: float = 1.0
    filename_prefix: str = "video/ltx-2.5-long"

    @property
    def tail_frames(self) -> int:
        return FRAME_STEP * self.context_frames_latents + 1

    @property
    def start_frames(self) -> int:
        """Frames already present before the first added segment."""
        if self.source is not None:
            return self.source.frames
        return self.segments[0].frames

    @property
    def total_frames(self) -> int:
        if self.source is not None:
            return self.source.frames + sum(s.frames for s in self.segments)
        return sum(s.frames for s in self.segments)


def context_latents(context_seconds: float, fps: float) -> int:
    """Number of context latent steps ``c`` for a context window in seconds."""
    return max(1, round(context_seconds * fps / FRAME_STEP))


def first_segment_frames(seconds: float, fps: float) -> int:
    """Frames of a first segment: ``8a + 1`` nearest to ``seconds``."""
    a = max(1, round((seconds * fps - 1) / FRAME_STEP))
    return FRAME_STEP * a + 1


def _added_blocks(seconds: float, fps: float) -> int:
    return max(1, round(seconds * fps / FRAME_STEP))


def split_blocks(blocks: int, max_blocks: int) -> list[int]:
    """Split ``blocks`` (units of 8 frames) into near-equal parts of at most ``max_blocks``."""
    parts = max(1, -(-blocks // max_blocks))
    base, extra = divmod(blocks, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def plan_segments(
    shots: Sequence[tuple[str, float]],
    *,
    fps: float,
    has_source: bool,
    max_segment_seconds: float = DEFAULT_MAX_SEGMENT_SECONDS,
    state_ledger: str | None = None,
) -> list[Segment]:
    """Turn ``[(prompt, seconds), ...]`` into segments.

    Without a source video the first shot becomes the first segment (its length
    is capped at ``max_segment_seconds`` by moving the rest into added
    segments of the same shot). A shot longer than ``max_segment_seconds`` is
    split into near-equal segments that share the shot prompt. ``state_ledger``
    text is prepended to every segment prompt.
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
                for blocks in split_blocks(_added_blocks(remaining, fps), max_blocks):
                    out.append(Segment(text, blocks * FRAME_STEP, 0))
            continue
        for blocks in split_blocks(_added_blocks(seconds, fps), max_blocks):
            out.append(Segment(text, blocks * FRAME_STEP, index))
    return out


def total_seconds(segments: Sequence[Segment], fps: float, source_frames: int = 0) -> float:
    return (source_frames + sum(s.frames for s in segments)) / fps


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------


@dataclass
class BuiltChain:
    graph: dict[str, Any]
    save_node: str
    # Per added segment: node ids that report progress ("sample" then "decode").
    units: list[dict[str, str]] = field(default_factory=list)
    total_frames: int = 0


def _ref(node: str, index: int = 0) -> list[Any]:
    return [node, index]


def build_chain_graph(spec: ChainSpec) -> BuiltChain:
    """Build the API-format ComfyUI graph for ``spec``."""
    g: dict[str, Any] = {}

    def add(node_id: str, class_type: str, **inputs: Any) -> list[Any]:
        g[node_id] = {"class_type": class_type, "inputs": inputs}
        return _ref(node_id)

    fps = float(spec.fps)
    width, height = spec.width, spec.height
    unet = add("unet", "UNETLoader",
               unet_name="ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
               weight_dtype="default")
    vae = add("vae", "VAELoader", vae_name="ltx-2.5-video-vae-bf16.safetensors")
    avae = add("avae", "VAELoader", vae_name="ltx-2.5-audio-vae-bf16.safetensors")
    clip = add("clip", "CLIPLoader",
               clip_name="gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
               type="ltxv", device="default")
    neg = add("neg", "CLIPTextEncode", clip=clip, text=spec.negative)
    sampler = add("sampler", "KSamplerSelect", sampler_name="euler_ancestral")
    sigmas = add("sigmas", "ManualSigmas", sigmas=SIGMAS)

    def conditioning(j: int, prompt: str) -> list[Any]:
        pos = add(f"s{j}_text", "CLIPTextEncode", clip=clip, text=prompt)
        return add(f"s{j}_cond", "LTXVConditioning", positive=pos, negative=neg, frame_rate=fps)

    def decode(j: int, latent: list[Any]) -> list[Any]:
        return add(f"s{j}_decode", "VAEDecodeTiled", samples=latent, vae=vae, tile_size=512,
                   overlap=64, temporal_size=64, temporal_overlap=16)

    def audio_latent(j: int, frames: int) -> list[Any]:
        return add(f"s{j}_alat", "LTXVEmptyLatentAudio", frames_number=frames,
                   frame_rate=int(round(fps)), batch_size=1, audio_vae=avae)

    units: list[dict[str, str]] = []
    audio_parts: list[list[Any]] = []
    segments = list(spec.segments)
    keep_audio = True

    if spec.source is not None:
        src = spec.source
        keep_audio = src.has_audio
        loaded = add("src_file", "LoadVideo", file=src.filename)
        comps = add("src_parts", "GetVideoComponents", video=loaded)
        frames = _ref("src_parts", 0)
        if (src.width, src.height) != (width, height):
            frames = add("src_scale", "ImageScale", image=frames, upscale_method="lanczos",
                         width=width, height=height, crop="center")
        have = src.frames
        if src.has_audio:
            audio_parts.append(add("src_audio", "TrimAudioDuration", audio=_ref("src_parts", 1),
                                   start_index=0.0, duration=src.frames / fps))
        todo = list(enumerate(segments))
    else:
        first = segments[0]
        cond = conditioning(0, first.prompt)
        latent = add("s0_vlat", "EmptyLTXVLatentVideo", width=width, height=height,
                     length=first.frames, batch_size=1)
        pos, negc = _ref(cond[0], 0), _ref(cond[0], 1)
        if spec.first_frame_image:
            img = add("s0_image", "LoadImage", image=spec.first_frame_image)
            prep = add("s0_prep", "LTXVPreprocess", image=img, img_compression=35)
            latent = add("s0_i2v", "LTXVImgToVideoInplace", vae=vae, image=prep, latent=latent,
                         strength=spec.first_frame_strength, bypass=False)
        av = add("s0_av", "LTXVConcatAVLatent", video_latent=latent, audio_latent=audio_latent(0, first.frames))
        guider = add("s0_guider", "CFGGuider", model=unet, positive=pos, negative=negc, cfg=1.0)
        noise = add("s0_noise", "RandomNoise", noise_seed=spec.seed)
        sample = add("s0_sample", "SamplerCustomAdvanced", noise=noise, guider=guider,
                     sampler=sampler, sigmas=sigmas, latent_image=av)
        sep = add("s0_sep", "LTXVSeparateAVLatent", av_latent=_ref("s0_sample", 0))
        vlat = _ref("s0_sep", 0)
        frames = decode(0, vlat)
        audio_parts.append(add("s0_audio", "LTXVAudioVAEDecode", samples=_ref("s0_sep", 1), audio_vae=avae))
        units.append({"sample": "s0_sample", "decode": "s0_decode"})
        have = first.frames
        todo = list(enumerate(segments))[1:]

    tail = spec.tail_frames
    for j, seg in todo:
        cond = conditioning(j, seg.prompt)
        pos, negc = _ref(cond[0], 0), _ref(cond[0], 1)
        piece = add(f"s{j}_tail", "ImageFromBatch", image=frames, batch_index=have - tail, length=tail)
        enc = add(f"s{j}_enc", "VAEEncode", pixels=piece, vae=vae)
        total = tail + seg.frames
        newv = add(f"s{j}_vlat", "EmptyLTXVLatentVideo", width=width, height=height, length=total, batch_size=1)
        guided = add(f"s{j}_guide", "LTXVAddLatentGuide", positive=pos, negative=negc, vae=vae,
                     latent=newv, guiding_latent=enc, latent_idx=0, strength=spec.context_strength)
        gpos, gneg, glat = _ref(guided[0], 0), _ref(guided[0], 1), _ref(guided[0], 2)
        av = add(f"s{j}_av", "LTXVConcatAVLatent", video_latent=glat, audio_latent=audio_latent(j, total))
        guider = add(f"s{j}_guider", "CFGGuider", model=unet, positive=gpos, negative=gneg, cfg=1.0)
        noise = add(f"s{j}_noise", "RandomNoise", noise_seed=spec.seed + j)
        add(f"s{j}_sample", "SamplerCustomAdvanced", noise=noise, guider=guider,
            sampler=sampler, sigmas=sigmas, latent_image=av)
        add(f"s{j}_sep", "LTXVSeparateAVLatent", av_latent=_ref(f"s{j}_sample", 0))
        add(f"s{j}_crop", "LTXVCropGuides", positive=gpos, negative=gneg, latent=_ref(f"s{j}_sep", 0))
        seg_frames = decode(j, _ref(f"s{j}_crop", 2))
        new = add(f"s{j}_new", "ImageFromBatch", image=seg_frames, batch_index=tail, length=seg.frames)
        frames = add(f"s{j}_cat", "ImageBatch", image1=frames, image2=new)
        have += seg.frames
        if keep_audio:
            seg_audio = add(f"s{j}_audio", "LTXVAudioVAEDecode", samples=_ref(f"s{j}_sep", 1), audio_vae=avae)
            audio_parts.append(add(f"s{j}_atrim", "TrimAudioDuration", audio=seg_audio,
                                   start_index=tail / fps, duration=seg.frames / fps))
        units.append({"sample": f"s{j}_sample", "decode": f"s{j}_decode"})

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


class ChainEngine:
    """A continuity backend for long video.

    The gateway plans segments and routes requests; an engine turns a
    ``ChainSpec`` into one ComfyUI graph and says how each added segment is
    conditioned on the video so far. A workflow selects its engine with
    ``builder:`` in proxy_config.yml. Only ``ltx25_chain`` exists today (frame
    window: the last ``context`` frames are encoded and pinned with a latent
    guide). Planned: an LTX overlap-latent engine (keeps latents between
    segments, no decode round trip) and an H3 multishot latent engine; both
    reuse the planner, the routes, the record and the failure info unchanged.
    """

    name = "ltx25_chain"
    model_family = "ltx-2.5"

    def build(self, spec: "ChainSpec") -> "BuiltChain":
        return build_chain_graph(spec)


ENGINES: dict[str, ChainEngine] = {ChainEngine.name: ChainEngine()}


def engine_for(builder: str | None) -> ChainEngine | None:
    return ENGINES.get(str(builder or ""))


def segment_of_node(units: Sequence[Mapping[str, str]], node_id: str) -> int | None:
    """Index of the added-segment unit that owns ``node_id`` (None if shared)."""
    for index, unit in enumerate(units):
        if node_id in unit.values() or node_id.startswith(f"s{index}_"):
            return index
    return None
