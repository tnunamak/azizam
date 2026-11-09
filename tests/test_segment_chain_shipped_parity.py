"""V4: the shipped chain templates compose exactly as they did before the prefix-grammar and export-link checks.

The pinned digests in ``fixtures/s7_video_chain/compose_matrix.json`` were recorded from the implementation before the
V4 change, for lengths 1, 2, 3, 4 and 12, with and without a source. To re-record:
``./venv/bin/python -m tests.test_segment_chain_shipped_parity``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import segment_chain as sc
from gateway.video_extend import CHAIN_DIR

GOLDEN = Path(__file__).parent / "fixtures" / "s7_video_chain" / "compose_matrix.json"
LENGTHS = (1, 2, 3, 4, 12)


def _templates() -> dict[str, sc.ChainTemplate]:
    return {p.stem: sc.load(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(CHAIN_DIR.glob("*.json"))}


def _digest(template: sc.ChainTemplate, count: int, with_source: bool) -> str:
    binding = template.binding
    source = SimpleNamespace(filename="clip.mp4", frames=97, width=768, height=512, has_audio=True) if with_source else None
    spec = SimpleNamespace(
        segments=[SimpleNamespace(prompt=f"scene {i}", frames=97 if i == 0 else 64) for i in range(count)],
        width=768, height=512, fps=24.0, seed=7, source=source, first_frame_image=None, first_frame_strength=1.0,
        context_strength=0.5, filename_prefix="video/long")
    composed = sc.compose(template, spec, context=binding.default_context_frames, negative="")
    payload = {"graph": composed.graph, "units": composed.units, "save": composed.save_node, "total": composed.total_frames}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _matrix() -> dict[str, str]:
    return {f"{name}/{count}/{'source' if src else 'plain'}": _digest(template, count, src)
            for name, template in _templates().items() for count in LENGTHS for src in (False, True)}


@pytest.mark.parametrize("key", sorted(json.loads(GOLDEN.read_text())) if GOLDEN.exists() else [])
def test_a_shipped_chain_composes_byte_identically_to_the_recorded_output(key):
    assert _matrix()[key] == json.loads(GOLDEN.read_text())[key]


def test_the_shipped_matrix_and_the_record_agree():
    assert sorted(_matrix()) == sorted(json.loads(GOLDEN.read_text()))


if __name__ == "__main__":
    GOLDEN.write_text(json.dumps(_matrix(), indent=1, sort_keys=True) + "\n")
