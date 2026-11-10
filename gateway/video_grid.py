"""The facts a video request is checked and sized against, read from a workflow manifest (CODE_TO_DATA S7, section 2).

A video manifest carries one optional section named after its capability: the role each size, length and picture input
plays, the size and length limits, the frame grid, which workflow takes over for a picture or a long clip, what a request
must supply, and two switches. This module reads that section into frozen types and applies the two numeric rules that
used to live in ``proxy.py``: the frame grid (``seconds_to_length``) and the size clamp (``resolve_size``). It names no
model, engine or workflow; the manifest is the only place those numbers live.

``proxy._seconds_to_length`` and ``proxy._parse_size_clamped`` are the old copies. They stay until the old create path
goes (V5c), and the tests hold the two to the same answers over a sweep, so there is one rule with two readers, not two
rules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any

DEFAULT_SIZE_WHEN_UNPARSED = (1024, 1024)
MINIMUM_SIDE = 64


@dataclass(frozen=True)
class SizeSpec:
    default: str        # "WxH", used when the request sends no size
    max: str            # "WxH", a larger request is clamped to it, never refused
    multiple: int = 32  # each side is floored to a multiple of this


@dataclass(frozen=True)
class LengthSpec:
    min_seconds: float
    max_seconds: float
    default_seconds: float | None = None


@dataclass(frozen=True)
class GridSpec:
    """The frame count of a clip is ``step * k + offset``. ``fps`` is the rate when the request sends none."""

    fps: float
    step: int = 8
    offset: int = 1
    min_frames: int | None = None
    max_frames: int | None = None


@dataclass(frozen=True)
class VideoManifest:
    roles: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    size: SizeSpec | None = None
    length: LengthSpec | None = None
    grid: GridSpec | None = None
    variants: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    requires: frozenset[str] = frozenset()
    select_subject: bool = False
    chain: bool = False


def video_facts(entry: Any) -> VideoManifest | None:
    """The typed section of a registry entry (anything with a ``contract`` mapping) or of a contract mapping itself.
    ``None`` when the manifest has none. The section is the one named by the manifest's own capability."""
    contract = entry if isinstance(entry, Mapping) else getattr(entry, "contract", None)
    if not isinstance(contract, Mapping):
        return None
    raw = contract.get(contract.get("capability"))
    if not isinstance(raw, Mapping):
        return None
    size, length, grid = raw.get("size"), raw.get("length"), raw.get("grid")
    return VideoManifest(
        roles=MappingProxyType(dict(raw.get("roles") or {})),
        size=SizeSpec(default=size["default"], max=size["max"], multiple=int(size.get("multiple", 32))) if size else None,
        length=LengthSpec(
            min_seconds=length["min_seconds"], max_seconds=length["max_seconds"],
            default_seconds=length.get("default_seconds")) if length else None,
        grid=GridSpec(
            fps=grid["fps"], step=int(grid.get("step", 8)), offset=int(grid.get("offset", 1)),
            min_frames=grid.get("min_frames"), max_frames=grid.get("max_frames")) if grid else None,
        variants=MappingProxyType(dict(raw.get("variants") or {})),
        requires=frozenset(raw.get("requires") or ()),
        select_subject=bool(raw.get("select_subject", False)),
        chain=bool(raw.get("chain", False)),
    )


def reference_variants(
    manifest: VideoManifest,
    get_workflow: Callable[[str], Any | None],
    reference_input: Callable[[Any], str | None],
) -> list[tuple[Any, VideoManifest]]:
    """Variants whose declared role is the input carried by the create protocol's reference field.

    Share this relation between registry validation and request selection so a checked registry cannot select two
    variants. Missing workflows or sections retain the selector's existing fallback behavior.
    """
    eligible = []
    for role, workflow_id in manifest.variants.items():
        candidate = get_workflow(workflow_id)
        if candidate is None:
            continue
        candidate_manifest = video_facts(candidate)
        if candidate_manifest is None:
            continue
        candidate_input = reference_input(candidate)
        if candidate_input is not None and candidate_manifest.roles.get(role, role) == candidate_input \
                and all(candidate is not chosen for chosen, _ in eligible):   # two roles may name one workflow
            eligible.append((candidate, candidate_manifest))
    return eligible


def check_video_variants(records: Iterable[Any]) -> None:
    """Reject ambiguous reference variants after all registry records have loaded, before publishing the registry.

    Pass the complete registry's records (not a filtered public catalog). The check reads contracts and the current
    async request maps, just like create selection; it neither changes manifests nor depends on workflow spellings.
    """
    from gateway.workflow_openapi import reference_input

    by_id = {record.id: record for record in records}

    def reference_target(entry: Any) -> str | None:
        record = SimpleNamespace(id=entry.id, intent=entry.contract["intent"], contract=entry.contract)
        return reference_input(record, "async")

    for entry in by_id.values():
        manifest = video_facts(entry)
        if manifest is None:
            continue
        eligible = reference_variants(manifest, by_id.get, reference_target)
        if len(eligible) > 1:
            raise ValueError(f"{entry.id}: multiple variants accept the reference: {sorted(item.id for item, _ in eligible)!r}")


def seconds_to_length(seconds: float, frame_rate: float, *, step: int = 8, offset: int = 1) -> int:
    """The nearest frame count of the form ``step * k + offset`` for ``seconds`` at ``frame_rate``.

    Rounds to the nearest k, not down, so 3 s at 24 fps with step 8 and offset 1 gives 73 frames (3.0417 s). Python's
    ``round`` decides ties (banker's rounding); that is the rule the old create code used, kept exactly. Callers compare
    ``length / frame_rate`` with ``seconds`` to decide whether the duration was adjusted.
    """
    frames = max(1, int(round(seconds * frame_rate)))
    k = max(1, int(round((frames - offset) / float(step))))
    return k * step + offset


def frame_count(seconds: float, frame_rate: float | None, grid: GridSpec) -> int:
    """The frame count sent to the engine: the grid length at the request's rate (or the grid's), within the declared bounds."""
    length = seconds_to_length(seconds, frame_rate or grid.fps, step=grid.step, offset=grid.offset)
    if grid.min_frames is not None:
        length = max(length, grid.min_frames)
    if grid.max_frames is not None:
        length = min(length, grid.max_frames)
    return length


def parse_size(text: str) -> tuple[int, int]:
    """``WxH`` to (width, height). Anything else is read as 1024x1024, as the old reader did."""
    if not text or "x" not in text:
        return DEFAULT_SIZE_WHEN_UNPARSED
    try:
        width, height = text.lower().split("x")
        return int(width), int(height)
    except ValueError:
        return DEFAULT_SIZE_WHEN_UNPARSED


def resolve_size(requested: str | None, size: SizeSpec) -> tuple[int, int]:
    """The size a run uses: the request's (or the default when it sends none), clamped to the largest, then each side
    floored to the multiple with a floor of 64. A size is adjusted, never refused."""
    width, height = parse_size(requested or size.default)
    largest = parse_size(size.max)
    width, height = min(width, largest[0]), min(height, largest[1])
    multiple = max(1, int(size.multiple))
    return max(MINIMUM_SIDE, (width // multiple) * multiple), max(MINIMUM_SIDE, (height // multiple) * multiple)
