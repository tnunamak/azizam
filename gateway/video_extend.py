"""Long-video planning and chain engines.

One request makes one engine prompt. The prompt renders a first segment (or loads a source video) and then adds
segments one after another. Each added segment is conditioned on the last ``context`` frames of the video so far, so
motion carries across the join. The graph for each chain is data: ``workflows/videos/chains/<id>.json`` declares the
segment variants and the binding (frame grid, context windows, shot minimum, negative default, variant rule), and
``gateway/segment_chain.py`` composes it. What stays here is planning (the frame grid arithmetic) and the source
checks. Nothing here talks to the engine or FastAPI, and no engine, model or node class is named here.

Frame arithmetic, with the grid ``frame_step`` and ``frame_offset`` from the binding:

* a first segment has ``step * a + offset`` frames;
* an added segment adds a whole number of steps, so the clip stays on the grid;
* the context window is a number of frames; the binding gives the windows it can pin, or none, and then the window is
  a whole number of steps plus the offset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway import segment_chain
from gateway import openai_media_schemas

DEFAULT_CONTEXT_SECONDS = 1.0
DEFAULT_CONTEXT_STRENGTH = 0.9
DEFAULT_MAX_SEGMENT_SECONDS = 8.0
DEFAULT_MAX_TOTAL_SECONDS = 40.0
CHAIN_DIR = Path(__file__).resolve().parent.parent / "workflows" / "videos" / "chains"


class PlanError(ValueError):
    """A request cannot be turned into a chain. ``code`` is the API error code."""

    def __init__(self, message: str, *, code: str, param: str | None = None,
                 domain: str | None = None, logical_param: str | None = None):
        if param is not None and logical_param is not None:
            raise TypeError("param and logical_param are mutually exclusive")
        if logical_param is not None and domain is None:
            raise TypeError("domain is required with logical_param")
        super().__init__(message)
        self.code = code
        self.domain = domain
        self.logical_param = logical_param
        self.param = openai_media_schemas.wire_error_param(domain, logical_param) if logical_param is not None else param


@dataclass(frozen=True)
class Segment:
    prompt: str
    frames: int  # frames this segment adds (first segment: its full length)
    shot: int  # index of the storyboard shot this segment belongs to


@dataclass(frozen=True)
class SourceVideo:
    filename: str  # name in the engine's input dir
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
    negative: str | None = None  # the binding's default when unset
    context_frames: int | None = None  # tail length in frames; the binding's default when unset
    context_strength: float = DEFAULT_CONTEXT_STRENGTH
    source: SourceVideo | None = None
    first_frame_image: str | None = None  # engine input filename, first segment only
    first_frame_strength: float = 1.0
    filename_prefix: str | None = None  # the binding's default when unset


@dataclass
class BuiltChain:
    graph: dict[str, Any]
    save_node: str
    # Per added segment: node ids that report progress ("sample" then "decode").
    units: list[dict[str, str]]
    total_frames: int = 0


def total_seconds(segments: Sequence[Segment], fps: float, source_frames: int = 0) -> float:
    return (source_frames + sum(s.frames for s in segments)) / fps


def _split_blocks(blocks: int, max_blocks: int) -> list[int]:
    """Split ``blocks`` into near-equal parts of at most ``max_blocks``."""
    parts = max(1, -(-blocks // max_blocks))
    base, extra = divmod(blocks, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


class ChainEngine:
    """A continuity backend for long video, built from one chain template document.

    The gateway routes requests, checks the cap and records the job; the engine plans segments on its binding's frame
    grid and turns a ``ChainSpec`` into one graph with the composer. A workflow selects its engine with ``builder:`` in
    the videos config, naming a template document's id.
    """

    def __init__(self, template: segment_chain.ChainTemplate):
        self.template = template
        binding = template.binding
        self.binding = binding
        self.name = template.id
        self.model_family = binding.model_family
        self.stride_reason = binding.stride_reason
        self.min_shot_seconds = binding.min_shot_seconds
        # Extension options (extensions.com.azizam.workflow) this engine understands.
        self.option_keys = binding.options
        self.default_negative = binding.negative_default

    def first_segment_frames(self, seconds: float, fps: float) -> int:
        """Frames of a first segment: ``step * a + offset`` nearest to ``seconds``."""
        step, offset = self.binding.frame_step, self.binding.frame_offset
        return step * max(1, round((seconds * fps - offset) / step)) + offset

    def _added_blocks(self, seconds: float, fps: float) -> int:
        return max(1, round(seconds * fps / self.binding.frame_step))

    def tail_frames(self, context_seconds: float, fps: float) -> int:
        """Frames of the video so far that condition the next segment."""
        binding = self.binding
        if binding.context_windows:
            wanted = round(context_seconds * fps)
            return next((w for w in sorted(binding.context_windows, reverse=True) if w <= wanted),
                        min(binding.context_windows))
        steps = max(1, round(context_seconds * fps / binding.frame_step))
        return binding.frame_step * steps + binding.frame_offset

    def check_source(self, fps: float) -> None:
        """Raise ``PlanError`` when a source video of this frame rate cannot be extended."""
        check = self.binding.source_check
        if check is not None and abs(fps - check.fps) > check.tolerance:
            raise PlanError(check.message.format(fps=fps), code="video_fps_unsupported",
                            domain="segment_chain", logical_param="source_clip")

    def plan_segments(
        self,
        shots: Sequence[tuple[str, float]],
        *,
        fps: float,
        has_source: bool,
        max_segment_seconds: float = DEFAULT_MAX_SEGMENT_SECONDS,
        state_ledger: str | None = None,
    ) -> list[Segment]:
        """Turn ``[(prompt, seconds), ...]`` into segments.

        Without a source video the first shot becomes the first segment (its length is capped at
        ``max_segment_seconds`` by moving the rest into added segments of the same shot). A shot longer than
        ``max_segment_seconds`` is split into near-equal segments that share the shot prompt. ``state_ledger`` text is
        prepended to every segment prompt.
        """
        if not shots:
            raise PlanError("at least one shot is required", code="invalid_shots", param="shots")
        step = self.binding.frame_step
        max_blocks = max(1, int(max_segment_seconds * fps // step))
        ledger = (state_ledger or "").strip()
        out: list[Segment] = []
        for index, (prompt, seconds) in enumerate(shots):
            text = f"{ledger}\n\n{prompt}" if ledger else prompt
            if index == 0 and not has_source:
                first = self.first_segment_frames(min(seconds, max_segment_seconds), fps)
                out.append(Segment(text, first, 0))
                remaining = seconds - first / fps
                if remaining * fps >= step / 2:
                    for blocks in _split_blocks(self._added_blocks(remaining, fps), max_blocks):
                        out.append(Segment(text, blocks * step, 0))
                continue
            for blocks in _split_blocks(self._added_blocks(seconds, fps), max_blocks):
                out.append(Segment(text, blocks * step, index))
        return out

    def build(self, spec: ChainSpec) -> BuiltChain:
        context = spec.context_frames or self.binding.default_context_frames
        windows = self.binding.context_windows
        if windows and context not in windows:
            raise PlanError(f"context window of {context} frames is not one of {sorted(windows)}",
                            code="invalid_value", param="context_seconds")
        negative = self.default_negative if spec.negative is None else spec.negative
        composed = segment_chain.compose(self.template, spec, context=context, negative=negative)
        return BuiltChain(graph=composed.graph, save_node=composed.save_node, units=composed.units,
                          total_frames=composed.total_frames)


def load_engines(directory: Path = CHAIN_DIR) -> dict[str, ChainEngine]:
    """One engine per template document in ``directory``, by the document's id."""
    engines: dict[str, ChainEngine] = {}
    for path in sorted(directory.glob("*.json")):
        engine = ChainEngine(segment_chain.load(json.loads(path.read_text(encoding="utf-8"))))
        engines[engine.name] = engine
    return engines


ENGINES: dict[str, ChainEngine] = load_engines()


def engine_for(builder: str | None) -> ChainEngine | None:
    return ENGINES.get(str(builder or ""))


def segment_of_node(units: Sequence[Mapping[str, str]], node_id: str, *, node_prefix: str | None = None,
                    progress_only: bool = False) -> int | None:
    """Index of the added-segment unit that owns ``node_id`` (None if shared).

    A unit's sampler and decoder nodes always match. Any other node of a segment matches by ``node_prefix`` (the
    binding's, with ``{index}``) unless ``progress_only``: the engine may run a later segment's text encode before an
    earlier segment's sampler, so progress and the finished-segment count must not follow other nodes.
    """
    for index, unit in enumerate(units):
        if node_id in unit.values():
            return index
    if node_prefix is None or progress_only:
        return None
    index = segment_chain.prefix_index(node_prefix, node_id)
    return index if index is not None and index < len(units) else None


LEGACY_NODE_PREFIX = "s{index}_"  # the prefix every chain used before a record stored its own


def record_node_prefix(chain: Mapping[str, Any]) -> str:
    """The node-id prefix a stored chain record was built with. A record without the key predates it."""
    return str(chain.get("node_prefix") or LEGACY_NODE_PREFIX)
