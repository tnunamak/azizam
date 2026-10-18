"""ComfyUI ``/object_info`` lookup with a cache that survives transient errors.

The gateway derives the UI workflow it embeds in generated media from each
node type's slot definitions (see ``gateway.engine_protocol.node_ui_graph``). A successful
answer is cached for the life of the process. A failure is remembered only for
``FAILURE_TTL`` seconds, so a ComfyUI that is down costs one short attempt per
node type per minute, and a transient error does not disable the lookup until
restart.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from typing import Any, Optional

import httpx

FAILURE_TTL = 60.0

# Last-known-good copy of each answer, so a ComfyUI that is restarting or busy
# when a job is submitted does not cost that job its embedded workflow. Unset
# (None) disables the disk copy; the gateway sets it at startup.
DISK_CACHE_DIR: Optional[Path] = None

_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_FAILED_AT: dict[tuple[str, str], float] = {}


def _disk_path(class_type: str) -> Optional[Path]:
    if DISK_CACHE_DIR is None:
        return None
    return DISK_CACHE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", class_type) + ".json")


def _disk_read(class_type: str) -> Optional[dict[str, Any]]:
    path = _disk_path(class_type)
    try:
        return json.loads(path.read_text()) if path is not None and path.exists() else None
    except Exception:  # noqa: BLE001 - a bad cache file is just a miss
        return None


def _disk_write(class_type: str, info: dict[str, Any]) -> None:
    path = _disk_path(class_type)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info))
        tmp.replace(path)
    except OSError:
        pass


def lookup(host: str, class_type: str) -> Optional[dict[str, Any]]:
    """Return the ``object_info`` entry for ``class_type``, or None."""
    key = (host, class_type)
    if key in _CACHE:
        return _CACHE[key]
    failed_at = _FAILED_AT.get(key)
    if failed_at is not None and time.monotonic() - failed_at < FAILURE_TTL:
        return _disk_read(class_type)
    info: Optional[dict[str, Any]] = None
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=3)) as client:
            response = client.get(f"{host}/object_info/{quote(class_type, safe='')}")
        if response.status_code == 200:
            info = response.json().get(class_type)
    except Exception:  # noqa: BLE001 - best effort, never fail a render
        info = None
    if info is None:
        _FAILED_AT[key] = time.monotonic()
        return _disk_read(class_type)
    _CACHE[key] = info
    _disk_write(class_type, info)
    return info
