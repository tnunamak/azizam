"""Parse a shipped YAML file once per version of the file.

A runtime config reload rebuilds the workflow registry and the seed catalogs from release files, and every owner
write (a publish toggle included) reloads. Parsing the same unchanged YAML on each reload was most of a publish
request (measured 2026-11-13: 145 parses, 2.0 of 3.2 s). This module keys each parse by a hash of the file's bytes,
so any change to a file parses again (a stat key would miss a same-size rewrite inside one timestamp tick), and gives
each caller its own deep copy, so no caller can change what the next one reads. Only parsing is cached: callers
still validate every time.
"""
from __future__ import annotations

import copy
import hashlib
import threading
from pathlib import Path
from typing import Any

import yaml

_LIMIT = 1024
_parsed: dict[bytes, Any] = {}
_lock = threading.Lock()


def load_yaml(path: str | Path) -> Any:
    """``yaml.safe_load`` of the file, from cache when the file has not changed since it was last parsed."""
    text = Path(path).read_bytes()
    key = hashlib.sha256(text).digest()
    with _lock:
        hit = key in _parsed
        document = _parsed.get(key)
    if not hit:
        document = yaml.safe_load(text.decode("utf-8"))
        with _lock:
            if len(_parsed) >= _LIMIT:
                _parsed.clear()
            _parsed[key] = document
    return copy.deepcopy(document)
