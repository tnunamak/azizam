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
from gateway import private_io
from gateway import engine_egress

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
        private_io.atomic_write_text(path, json.dumps(info))
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
        info = _fetch(host, class_type, only_200=True)
    except Exception:  # noqa: BLE001 - best effort, never fail a render
        info = None
    if info is None:
        _FAILED_AT[key] = time.monotonic()
        return _disk_read(class_type)
    _CACHE[key] = info
    _disk_write(class_type, info)
    return info


def _fetch(host: str, node_type: str, *, only_200: bool = False) -> Any:
    """Read one definition, preserving the lookup reader's strict status policy."""
    with engine_egress.client(timeout=httpx.Timeout(10, connect=3)) as client:
        response = client.get(f"{host}/object_info/{quote(node_type, safe='')}", extensions=engine_egress.probe())
    if only_200:
        if response.status_code != 200:
            return None
    else:
        response.raise_for_status()
    return response.json().get(node_type)


def fetch_fresh(host: str, node_type: str) -> dict[str, Any]:
    """Fetch a node definition without process or disk caches; raise on failure."""
    info = _fetch(host, node_type)
    if not isinstance(info, dict):
        raise ValueError(f"engine returned no definition for {node_type}")
    return info


def fetch_header(host: str, folder: str, filename: str) -> Optional[dict[str, Any]]:
    """The metadata block of one model file's header, or None when the engine has none for it (a 404: the file has no
    metadata, or is not a safetensors file). Any other answer raises, so a failed read is never taken for "no metadata"."""
    with engine_egress.client(timeout=httpx.Timeout(10, connect=3)) as client:
        response = client.get(f"{host}/view_metadata/{quote(folder, safe='')}", params={"filename": filename},
                              extensions=engine_egress.probe())
    if response.status_code == 404:
        return None
    response.raise_for_status()
    metadata = response.json()
    return metadata if isinstance(metadata, dict) else None
