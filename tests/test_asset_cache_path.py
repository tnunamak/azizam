"""An asset's file is found by its digest, whatever suffix the output kept.

ComfyUI `gifs` and `images` outputs keep their own suffix, so a video or audio
asset can be a .gif or .webp file. A guessed suffix list per kind called those
files missing.
"""
from __future__ import annotations

import os
import time

import pytest

import proxy
from gateway import directory_index

DIGEST = "ab" * 32


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    directory_index.forget()
    yield tmp_path
    directory_index.forget()


def settle(directory):
    """Age the directory so its listing is trusted (see gateway/directory_index.py)."""
    old = time.time() - 60
    os.utime(directory, (old, old))
    directory_index.forget()


@pytest.mark.parametrize("settled", [True, False])
@pytest.mark.parametrize("asset_id,name", [
    (f"vid_{DIGEST}", f"{DIGEST}.gif"),
    (f"vid_{DIGEST}", f"{DIGEST}.webp"),
    (f"aud_{DIGEST}", f"{DIGEST}.webp"),
    (f"img_{DIGEST}", f"{DIGEST}.mp4"),
    (f"vid_{DIGEST}", f"{DIGEST}.mp4"),
])
def test_a_file_with_any_suffix_is_the_assets_file(cache, settled, asset_id, name):
    (cache / name).write_bytes(b"x")
    if settled:
        settle(cache)
    assert proxy.asset_cache_path(asset_id) == cache / name


@pytest.mark.parametrize("settled", [True, False])
def test_sidecars_and_half_written_files_are_not_the_asset(cache, settled):
    (cache / f"{DIGEST}.meta.json").write_text("{}")
    (cache / f"{DIGEST}.png.tmp").write_bytes(b"half")
    if settled:
        settle(cache)
    path = proxy.asset_cache_path(f"img_{DIGEST}")
    assert path is not None and not path.exists()
