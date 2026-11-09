"""The frame grid and the size rule of a video workflow, read from its manifest's ``video`` block.

This is the V5c stand-in for the module V5a delivers (CODE_TO_DATA_S7_VIDEO_MOVE.md 2.3, 2.6): the same names and the same
formula, so the integrator keeps V5a's file and drops this one. Nothing here names a workflow, a model or an engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VideoManifest:
    """The ``video`` block of one manifest, typed. Absent keys are None or empty."""

    roles: Mapping[str, str] = field(default_factory=dict)
    default_size: str | None = None
    max_size: str | None = None
    size_multiple: int = 32
    min_seconds: float = 1.0
    max_seconds: float = 8.0
    default_seconds: float | None = None
    fps: float = 25.0
    step: int = 8
    offset: int = 1
    min_frames: int | None = None
    max_frames: int | None = None
    variants: Mapping[str, str] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    select_subject: bool = False
    chain: bool = False


def video_facts(entry: Any) -> VideoManifest | None:
    """The typed ``video`` block of a workflow entry's contract; None when the contract has none."""
    block = (entry.contract or {}).get("video") if entry is not None else None
    if not isinstance(block, Mapping):
        return None
    size, length, grid = (block.get(name) or {} for name in ("size", "length", "grid"))
    return VideoManifest(
        roles=dict(block.get("roles") or {}), default_size=size.get("default"), max_size=size.get("max"),
        size_multiple=int(size.get("multiple", 32)), min_seconds=float(length.get("min_seconds", 1)),
        max_seconds=float(length.get("max_seconds", 8)),
        default_seconds=float(length["default_seconds"]) if length.get("default_seconds") is not None else None,
        fps=float(grid.get("fps", 25)), step=int(grid.get("step", 8)), offset=int(grid.get("offset", 1)),
        min_frames=int(grid["min_frames"]) if grid.get("min_frames") is not None else None,
        max_frames=int(grid["max_frames"]) if grid.get("max_frames") is not None else None,
        variants=dict(block.get("variants") or {}), requires=tuple(block.get("requires") or ()),
        select_subject=bool(block.get("select_subject")), chain=bool(block.get("chain")))


def seconds_to_length(seconds: float, fps: float, *, step: int, offset: int,
                      min_frames: int | None = None, max_frames: int | None = None) -> int:
    """The frame count of the form ``step * k + offset`` nearest to ``seconds`` at ``fps``, inside the declared bounds.
    Python's ``round`` (ties to even), as the first rule of this wire always used."""
    frames = max(1, int(round(seconds * fps)))
    k = max(1, int(round((frames - offset) / float(step))))
    length = k * step + offset
    if min_frames is not None:
        length = max(length, min_frames)
    if max_frames is not None:
        length = min(length, max_frames)
    return length


def parse_size(text: Any) -> tuple[int, int]:
    """``WxH`` read as a pair; anything else is 1024 square."""
    if not isinstance(text, str) or "x" not in text:
        return 1024, 1024
    try:
        width, height = text.lower().split("x")
        return int(width), int(height)
    except ValueError:
        return 1024, 1024


def clamp_size(size: Any, manifest: VideoManifest) -> tuple[int, int]:
    """The size a run uses: the text read as ``WxH`` (the declared default when it is absent), cut to the declared maximum,
    then floored to the declared multiple with a floor of 64 on each side."""
    width, height = parse_size(size or manifest.default_size or "1280x720")
    if manifest.max_size:
        max_width, max_height = parse_size(manifest.max_size)
        width, height = min(width, max_width), min(height, max_height)
    multiple = max(1, manifest.size_multiple)
    return max(64, width // multiple * multiple), max(64, height // multiple * multiple)
