"""Read-time output availability; historical execution status stays intact."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

AssetPathResolver = Callable[[str], Path | None]


def project(job: Mapping[str, Any], resolver: AssetPathResolver | None) -> dict[str, Any]:
    view = dict(job)
    if resolver is None or not job.get("assets"):
        return view
    assets = []
    for stored in job["assets"]:
        if not isinstance(stored, Mapping):
            continue
        asset = dict(stored)
        asset_id = asset.get("asset_id") or asset.get("id")
        try:
            path = resolver(asset_id) if isinstance(asset_id, str) else None
            available = path is not None and path.is_file()
            state = "available" if available else "missing"
        except OSError:
            available, state = False, "unavailable"
        asset.update(available=available, availability=state)
        if not available:
            for field in ("url", "asset_url", "content_url", "download_url", "thumbnail_url", "workflow_download_url"):
                asset.pop(field, None)
        assets.append(asset)
    view["assets"] = assets
    states = {asset["availability"] for asset in assets}
    if states:
        view["output_state"] = next(iter(states)) if len(states) == 1 else "partial"
    return view
