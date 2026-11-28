"""M9 oracle (S7 parity, video): the engine graph for each long-video chain, as canonical JSON.

The chain graphs are built in code today (``gateway/video_extend.py``, ``gateway/video_extend_h3.py``). M9 moves them to
templates plus a segment composer. The move must not change one byte the engine receives, so this module pins the graph,
its progress units and its frame count for a matrix of specs that covers every branch of both builders: no source and a
source, a first-frame picture, a silent source, a source that needs scaling, one and several segments, and each context
option. The expected values are in ``fixtures/s7_video_chain/graphs.json``, recorded from the code before the move.
To re-record: ``./venv/bin/python -m tests.test_s7_video_chain_graphs``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway import video_extend
from gateway.video_extend import ChainSpec, Segment, SourceVideo

GOLDEN = Path(__file__).parent / "fixtures" / "s7_video_chain" / "graphs.json"


def _segments(*frames: int) -> list[Segment]:
    return [Segment(f"scene {i}", n, min(i, 1)) for i, n in enumerate(frames)]


def _source(*, audio: bool = True, size: tuple[int, int] = (768, 512)) -> SourceVideo:
    return SourceVideo("clip.mp4", 97, 24.0, size[0], size[1], audio)


def _cases() -> dict[str, tuple[object, ChainSpec]]:
    ltx, h3 = video_extend.ENGINES["ltx25_chain"], video_extend.ENGINES["h3_chain"]
    base = dict(width=768, height=512, fps=24.0, seed=7)
    ltx_prefix = dict(base, filename_prefix="video/ltx-2.5-long")
    h3_prefix = dict(base, filename_prefix="video/minimax-h3-long", negative="")
    return {
        "ltx_one_segment": (ltx, ChainSpec(_segments(97), **ltx_prefix)),
        "ltx_three_segments": (ltx, ChainSpec(_segments(97, 64, 40), **ltx_prefix)),
        "ltx_first_frame": (ltx, ChainSpec(_segments(97, 64), first_frame_image="in.png", first_frame_strength=0.8,
                                           **ltx_prefix)),
        "ltx_context_options": (ltx, ChainSpec(_segments(97, 64), context_frames=33, context_strength=0.5, **ltx_prefix)),
        "ltx_source_audio": (ltx, ChainSpec(_segments(64, 40), source=_source(), **ltx_prefix)),
        "ltx_source_silent_scaled": (ltx, ChainSpec(_segments(64), source=_source(audio=False, size=(640, 360)), **ltx_prefix)),
        "h3_one_segment": (h3, ChainSpec(_segments(101), **h3_prefix)),
        "h3_three_segments": (h3, ChainSpec(_segments(101, 68, 34), **h3_prefix)),
        "h3_first_frame": (h3, ChainSpec(_segments(101, 68), first_frame_image="in.png", **h3_prefix)),
        "h3_context_56": (h3, ChainSpec(_segments(101, 68), context_frames=56, **h3_prefix)),
        "h3_context_5": (h3, ChainSpec(_segments(101, 68), context_frames=5, **h3_prefix)),
        "h3_source_audio": (h3, ChainSpec(_segments(68, 34), source=_source(), **h3_prefix)),
        "h3_source_silent_scaled": (h3, ChainSpec(_segments(68), source=_source(audio=False, size=(640, 360)), **h3_prefix)),
    }


def _built(engine, spec: ChainSpec) -> dict:
    built = engine.build(spec)
    return {"graph": built.graph, "save_node": built.save_node, "units": built.units, "total_frames": built.total_frames}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("name", sorted(_cases()))
def test_the_engine_graph_is_byte_identical_after_canonical_sorting(name):
    engine, spec = _cases()[name]
    expected = json.loads(GOLDEN.read_text())[name]
    assert _canonical(_built(engine, spec)) == _canonical(expected)


def test_the_matrix_and_the_record_agree():
    assert sorted(json.loads(GOLDEN.read_text())) == sorted(_cases())


def test_a_context_window_h3_cannot_pin_is_refused():
    engine, spec = _cases()["h3_context_5"]
    bad = ChainSpec(spec.segments, width=spec.width, height=spec.height, fps=spec.fps, seed=spec.seed, context_frames=7)
    with pytest.raises(video_extend.PlanError) as err:
        engine.build(bad)
    assert err.value.code == "invalid_value" and err.value.param == "context_seconds"


if __name__ == "__main__":
    GOLDEN.write_text(json.dumps({name: _built(*case) for name, case in _cases().items()}, sort_keys=True, indent=1) + "\n")
