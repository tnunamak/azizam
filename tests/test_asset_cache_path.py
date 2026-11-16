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


def test_directory_replacement_with_preserved_mtime_has_new_names(cache):
    directory = cache / "listed"
    directory.mkdir()
    (directory / "old.txt").write_bytes(b"x")
    settle(directory)
    before = directory.stat()
    assert directory_index.names(directory) == frozenset({"old.txt"})

    replacement = cache / "replacement"
    replacement.mkdir()
    (replacement / "new.txt").write_bytes(b"x")
    directory.rename(cache / "retired")
    replacement.rename(directory)
    os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = directory.stat()
    assert after.st_ino != before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert directory_index.names(directory) == frozenset({"new.txt"})


def test_backward_clock_clears_directory_cache(cache, monkeypatch):
    (cache / "entry.txt").write_bytes(b"x")
    settle(cache)
    now = time.time_ns()
    # Isolate the process-wide clock history and restore it after this test.
    monkeypatch.setattr(directory_index, "_HIGH_WATER_NS", 0, raising=False)
    monkeypatch.setattr(directory_index.time, "time_ns", lambda: now)
    assert directory_index.names(cache) == frozenset({"entry.txt"})
    assert directory_index._NAMES

    monkeypatch.setattr(directory_index.time, "time_ns", lambda: now - 3_000_000_000)
    assert directory_index.names(cache) is None
    assert directory_index._NAMES == {}
    # Remaining below the high-water mark must not make time trusted again.
    assert directory_index.names(cache) is None
    assert directory_index._NAMES == {}


def test_directory_replaced_during_listing_is_not_cached(cache, monkeypatch):
    directory = cache / "listed"
    directory.mkdir()
    (directory / "old.txt").write_bytes(b"x")
    settle(directory)
    before = directory.stat()
    replacement = cache / "replacement"
    replacement.mkdir()
    (replacement / "new.txt").write_bytes(b"x")
    original_listdir = directory_index.os.listdir

    def replace_after_listing(path):
        listed = original_listdir(path)
        if str(path) == str(directory):
            directory.rename(cache / "retired")
            replacement.rename(directory)
            os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert directory.stat().st_ino != before.st_ino
        return listed

    monkeypatch.setattr(directory_index.os, "listdir", replace_after_listing)
    assert directory_index.names(directory) is None
    assert str(directory) not in directory_index._NAMES
