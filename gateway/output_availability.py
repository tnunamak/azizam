"""Read-time output availability; historical execution status stays intact."""
from __future__ import annotations

import errno
import stat
from pathlib import Path
from typing import Any, Callable, Mapping

AssetPathResolver = Callable[[str], Path | None]


def project(job: Mapping[str, Any], resolver: AssetPathResolver | None) -> dict[str, Any]:
    view = dict(job)
    if resolver is None or not job.get("assets"):
        return view
    assets = []
    states = set()
    for stored in job["assets"]:
        if not isinstance(stored, Mapping):
            assets.append({"available": False, "availability": "unavailable"})
            states.add("unavailable")
            continue
        asset = dict(stored)
        asset_id = asset.get("asset_id") or asset.get("id")
        try:
            path = resolver(asset_id) if isinstance(asset_id, str) else None
            available = path is not None and stat.S_ISREG(path.stat().st_mode)
            state = "available" if available else "missing"
        except OSError as exc:
            available = False
            state = "missing" if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG} else "unavailable"
        states.add(state)
        if available:
            asset.pop("availability", None)
            asset.pop("available", None)
        else:
            asset.update(available=False, availability=state)
            for field in ("url", "asset_url", "content_url", "download_url", "thumbnail_url", "workflow_download_url"):
                asset.pop(field, None)
        assets.append(asset)
    view["assets"] = assets
    if states == {"available"}:
        view.pop("output_state", None)
    elif "available" in states:
        view["output_state"] = "partial"
    else:
        view["output_state"] = "missing" if states == {"missing"} else "unavailable"
    return view
