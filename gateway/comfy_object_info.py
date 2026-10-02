"""ComfyUI ``/object_info`` lookup with a cache that survives transient errors.

The gateway derives the UI workflow it embeds in generated media from each
node type's slot definitions (see ``gateway.comfy_ui_graph``). A successful
answer is cached for the life of the process. A failure is remembered only for
``FAILURE_TTL`` seconds, so a ComfyUI that is down costs one short attempt per
node type per minute, and a transient error does not disable the lookup until
restart.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

FAILURE_TTL = 60.0

_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_FAILED_AT: dict[tuple[str, str], float] = {}


def lookup(host: str, class_type: str) -> Optional[dict[str, Any]]:
    """Return the ``object_info`` entry for ``class_type``, or None."""
    key = (host, class_type)
    if key in _CACHE:
        return _CACHE[key]
    failed_at = _FAILED_AT.get(key)
    if failed_at is not None and time.monotonic() - failed_at < FAILURE_TTL:
        return None
    info: Optional[dict[str, Any]] = None
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=3)) as client:
            response = client.get(f"{host}/object_info/{class_type}")
        if response.status_code == 200:
            info = response.json().get(class_type)
    except Exception:  # noqa: BLE001 - best effort, never fail a render
        info = None
    if info is None:
        _FAILED_AT[key] = time.monotonic()
        return None
    _CACHE[key] = info
    return info
