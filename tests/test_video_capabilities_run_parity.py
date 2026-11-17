"""A caller that follows the MCP ``capabilities`` schema for a video workflow gets a run the route accepts.

For every video workflow in the catalog (no names listed) each advertised input, at a valid value, and the
advertised maximum of each bounded number, is built into a request the way ``run`` builds it and posted to
POST /v1/videos. The engine is a fake ComfyUI that never finishes: the question is whether the route accepts
the request, not whether the render succeeds.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Mapping

import proxy
from gateway.workflow_call import build_request, effective_schema
from tests.test_media_inputs_upload_routes import video_rig  # noqa: F401  (a fixture)
from tests.test_media_inputs_image_route import image_bytes, rig  # noqa: F401  (rig is a fixture)

_PNG = base64.b64encode(image_bytes("PNG")).decode()


def _samples(spec: Mapping[str, Any]) -> list[Any]:
    """Valid values for one advertised input: its default or first choice, and its maximum when bounded."""
    if spec.get("x-azizam-asset"):
        return [f"data:image/png;base64,{_PNG}"]
    if "enum" in spec:
        return [spec["enum"][0]]
    if spec.get("type") in ("integer", "number"):
        out = [spec["default"] if "default" in spec else spec.get("minimum", 1)]
        if "maximum" in spec:
            out.append(spec["maximum"])
        return out
    if "default" in spec:
        return [spec["default"]]
    return [True] if spec.get("type") == "boolean" else ["x"]


def _video_workflows() -> list[Mapping[str, Any]]:
    return [e for e in proxy._mcp_catalog()
            if e["id"].startswith("video.") and (e.get("execution") or {}).get("async")]


def _image_assets_only(entry: Mapping[str, Any]) -> bool:
    """A picture stands in for any asset the workflow takes; a workflow that also needs a clip is left out."""
    record = next(r for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records() if r.id == entry["id"])
    specs = (record.contract.get("inputs") or {}).values()
    return all(any(a.startswith("image/") for a in spec.get("accepts") or ["image/*"])
               for spec in specs if isinstance(spec, Mapping) and spec.get("type") == "asset" and spec.get("required"))


def test_every_advertised_video_input_is_accepted_by_the_route(video_rig) -> None:
    videos = _video_workflows()
    assert len(videos) >= 5
    refused = []
    for entry in filter(_image_assets_only, videos):
        schema = effective_schema(entry)
        props = schema.get("properties") or {}
        route = entry["execution"]["async"]
        submit = {**route["submit"], "fields": route["fields"],
                  "content_type": route["submit"].get("content_type", "")}
        base = {n: _samples(props[n])[0] for n in [*(schema.get("required") or []), *(["prompt"] if "prompt" in props else [])]}
        cases: list[tuple[str, dict[str, Any]]] = []
        together = {n: _samples(spec)[0] for n, spec in props.items()}  # a caller who fills in every field
        for name, spec in props.items():
            for value in _samples(spec):
                given = {**base, name: value}
                if name in ("width", "height"):  # a size is two inputs that travel together
                    given.update({p: _samples(props[p])[0] for p in ("width", "height") if p in props and p != name})
                cases.append((f"{name}={value!r}", given))
        cases.append(("every input at once", together))
        for label, given in cases:
            built = asyncio.run(build_request(lambda *_: None, None, None, entry, submit, given, wrap_intent=None))
            body = {**(built.json or {}), "model": entry["short_id"]}
            response = video_rig.client.post(built.path, json=body)
            if response.status_code >= 400:
                error = json.loads(response.text).get("error", {})
                refused.append(f"{entry['id']}: {label} -> {response.status_code} "
                               f"{error.get('code') or ''} {str(error.get('message'))[:90]}")
    assert not refused, "advertised inputs the route refuses:\n" + "\n".join(refused)
