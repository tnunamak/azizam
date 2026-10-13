"""The file names in a directory, listed once until the directory changes.

A Library read asked the filesystem about every asset: up to five ``exists()`` probes per asset
to find its file, and a read of every ``*.meta.json`` to list them. That is a cost per file in
every request, so a library of tens of thousands of files would take seconds to list.

A directory's modification time changes whenever a file in it is created, removed or renamed.
The names are kept while that time is unchanged. A change within ``SETTLE_S`` of now is not
trusted, because two changes in the same clock tick leave the same time (the same race as git's
index): such a directory is listed again each call, as before this module existed.
In-place rewrites of a file do not change the names and are not seen here.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

SETTLE_S = 2.0
_LOCK = threading.Lock()
_NAMES: dict[str, tuple[int, frozenset[str]]] = {}


def names(directory: Path) -> frozenset[str] | None:
    """The names in ``directory`` while they are known to be current; None when they may not be.

    None also means the directory is missing or unreadable: the caller keeps its own check.
    """
    key = str(directory)
    try:
        mtime_ns = os.stat(key).st_mtime_ns
    except OSError:
        return None
    if time.time_ns() - mtime_ns < SETTLE_S * 1e9:
        return None
    with _LOCK:
        hit = _NAMES.get(key)
        if hit is not None and hit[0] == mtime_ns:
            return hit[1]
    try:
        listed = frozenset(os.listdir(key))
        if os.stat(key).st_mtime_ns != mtime_ns:  # changed while listing
            return None
    except OSError:
        return None
    with _LOCK:
        _NAMES[key] = (mtime_ns, listed)
    return listed


def forget(directory: Path | None = None) -> None:
    with _LOCK:
        if directory is None:
            _NAMES.clear()
        else:
            _NAMES.pop(str(directory), None)
