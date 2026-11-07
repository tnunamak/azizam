"""A binding and contract for a test that runs a graph round on a made-up one-node workflow.

A round reads its result through the workflow's selectors, so a round that is given no binding is refused
(``selectors_missing``). The fake engine's histories put the result of node "1" under ``images``.
"""

from __future__ import annotations

from typing import Any

IMAGE_AT_NODE_1: dict[str, Any] = {
    "binding": {"workflow": "made-up.json",
                "selectors": {"image": {"node": "1", "key": "images", "from": "output_dir", "decode": "none", "take": "all"}}},
    "contract_outputs": {"image": {"media_type": "image/png"}},
}

def video_manifest(node: str, key: str) -> dict[str, Any]:
    """The part of a video workflow's manifest that says where its result is: the history ``node`` and ``key``."""
    selector = {"node": node, "key": key, "from": "output_dir", "decode": "none", "take": "first"}
    return {"bindings": {"comfyui": {"workflow": "made-up.json", "selectors": {"video": selector}}},
            "outputs": {"video": {"media_type": "video/mp4"}}}
