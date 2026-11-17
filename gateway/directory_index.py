"""The file names in a directory, listed once until the directory changes.

A Library read asked the filesystem about every asset: up to five ``exists()`` probes per asset
to find its file, and a read of every ``*.meta.json`` to list them. That is a cost per file in
every request, so a library of tens of thousands of files would take seconds to list.

A directory's modification time changes whenever a file in it is created, removed or renamed.
The names are kept only while device, inode and modification time are unchanged, including
across the listing itself: a replacement directory may preserve the old modification time.
A change within ``SETTLE_S`` of now is not trusted, because two changes in the same clock tick
leave the same time (the same race as git's index). A wall-clock high-water mark is checked
under the cache lock; a backward reading clears the cache and returns None for that call.
Such untrusted directories are listed by the caller, as before this module existed.
In-place rewrites of a file do not change the names and are not seen here.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

SETTLE_S = 2.0
_LOCK = threading.Lock()
_NAMES: dict[str, tuple[tuple[int, int, int], frozenset[str]]] = {}
_HIGH_WATER_NS = 0
_GENERATION = 0


def names(directory: Path) -> frozenset[str] | None:
    """The names in ``directory`` while they are known to be current; None when they may not be.

    None also means the directory is missing or unreadable: the caller keeps its own check.
    """
    global _HIGH_WATER_NS, _GENERATION
    key = str(directory)
    # Capture invalidation under the lock, but do directory listing outside it.
    with _LOCK:
        now_ns = time.time_ns()
        if now_ns < _HIGH_WATER_NS:
            _NAMES.clear()
            _GENERATION += 1
            return None
        _HIGH_WATER_NS = now_ns
        try:
            before = os.stat(key)
        except OSError:
            return None
        identity = (before.st_dev, before.st_ino, before.st_mtime_ns)
        if now_ns - before.st_mtime_ns < SETTLE_S * 1e9:
            return None
        hit = _NAMES.get(key)
        if hit is not None and hit[0] == identity:
            return hit[1]
        generation = _GENERATION
    try:
        listed = frozenset(os.listdir(key))
        after = os.stat(key)
        if (after.st_dev, after.st_ino, after.st_mtime_ns) != identity:
            return None
    except OSError:
        return None
    with _LOCK:
        if generation == _GENERATION:
            _NAMES[key] = (identity, listed)
    return listed


def forget(directory: Path | None = None) -> None:
    global _GENERATION
    with _LOCK:
        if directory is None:
            _NAMES.clear()
            _GENERATION += 1
        else:
            _NAMES.pop(str(directory), None)


def stored_digest(name: str) -> str | None:
    """The 64-character digest a cache file name belongs to, or None for a sidecar, scratch or other file.

    A stored media file is ``<digest><suffix>`` with whatever suffix the output kept: ComfyUI
    ``gifs`` and ``images`` outputs are .gif or .webp files whatever the asset kind. Sidecars
    (``.meta.json``) and half-written files (``.tmp``) are not stored media.
    """
    digest, dot, suffix = name.partition(".")
    if len(digest) != 64 or not dot or not suffix or name.endswith((".meta.json", ".tmp")):
        return None
    return digest


def stored_name(names: "frozenset[str] | list[str] | tuple[str, ...]", digest: str) -> str | None:
    """The file in ``names`` that holds ``digest``'s bytes, or None."""
    return min((name for name in names if stored_digest(name) == digest), default=None)
