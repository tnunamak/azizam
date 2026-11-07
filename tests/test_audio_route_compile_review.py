"""Review r1 of the audio route move (azizam-audio-route-compile-review-r1): one test per fixed finding.

- a manifest map that does not fit its template answers 500 and submits nothing (finding 1);
- the route loads the template the manifest's binding names, not a second file from config (finding 5);
- prompt and tags are placed independently, so distinct targets each get their value (finding 6);
- an unset seed is drawn inside the manifest's declared bounds (design note).
"""

from __future__ import annotations

import asyncio
import json

import httpx

import pytest
from typing import Any
from unittest.mock import patch

import proxy
from tests.support.config_snapshot import derived_patch
from gateway import generated_defaults
from gateway.engine_protocol.audio_graph import AudioLimits, effective_manifest, manifest_inputs
from gateway.manifests import ManifestError
from tests.support.access import access_context
from tests.support.audio_workflow import audio_manifest
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture

HOST = "audio-review.test:7890"


class _JsonRequest:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return dict(self._body)


def _call(body: dict[str, Any], *, manifest: dict[str, Any], template: dict[str, Any], fake_upstreams,
          loaded: list[Any] | None = None):
    """Run the route against the shared fake engine; it fails the test if the route reaches the engine."""
    def answer(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the route reached the engine: {request.method} {request.url.path}")

    fake_upstreams.router.add_transport(HOST, HandlerTransport(answer))

    def load(cfg: dict[str, Any]) -> dict[str, Any]:
        if loaded is not None:
            loaded.append(cfg.get("file"))
        return json.loads(json.dumps(template))

    with derived_patch("COMFY_ENABLED", True), \
            patch.object(proxy, "_service_availability_response", new=lambda *_a, **_k: None), \
            patch.object(proxy, "_resolve_audio_workflow", return_value=("fx", {"file": "config/elsewhere.json"})), \
            patch.object(proxy, "_load_audio_workflow_manifest", new=lambda _name: manifest), \
            patch.object(proxy, "_load_audio_workflow_file", new=load), \
            derived_patch("COMFY_HOST", f"http://{HOST}"), \
            patch.object(proxy, "configured_downstream_headers", new=lambda *_a, **_k: {}):
        return asyncio.run(proxy.proxy_audio_generate(_JsonRequest(body), access_context()))


def test_a_map_target_missing_from_the_template_is_a_deployment_fault_and_nothing_is_submitted(fake_upstreams):
    manifest = audio_manifest("fx", {"prompt": [("999", "text")]})
    response = _call({"model": "fx", "prompt": "x"}, manifest=manifest, template={"1": {"inputs": {"text": ""}}},
                     fake_upstreams=fake_upstreams)
    assert response.status_code == 500
    error = json.loads(response.body)["error"]
    assert error["type"] == "api_error"
    assert "999" not in error["message"], "the public error names a node id"


def test_the_route_loads_the_template_the_manifest_binding_names(fake_upstreams):
    manifest = audio_manifest("fx", {"prompt": [("1", "text")]}, workflow="workflows/audio/fx-from-manifest.json")
    loaded: list[Any] = []
    with patch.object(proxy, "_build_graph_submit_payload", side_effect=RuntimeError("stop before the wire")):
        _call({"model": "fx", "prompt": "x"}, manifest=manifest, template={"1": {"inputs": {"text": ""}}}, loaded=loaded,
              fake_upstreams=fake_upstreams)
    assert loaded == ["workflows/audio/fx-from-manifest.json"]


def test_prompt_and_tags_with_distinct_targets_are_both_placed():
    manifest = effective_manifest(audio_manifest("fx", {"prompt": [("1", "text")], "tags": [("2", "tags")]}),
                                  AudioLimits())
    inputs = manifest_inputs({"prompt": "melody", "tags": "jazz"}, manifest=manifest)
    assert inputs == {"prompt": "melody", "tags": "jazz"}


def test_an_unset_seed_is_drawn_inside_the_declared_bounds(monkeypatch):
    manifest = audio_manifest("fx", {"prompt": [("1", "text")], "seed": [("1", "seed")]})
    manifest["inputs"]["seed"].update(minimum=3, maximum=9)
    asked: list[tuple[int, int]] = []
    monkeypatch.setattr(generated_defaults, "secure_source", lambda low, high: asked.append((low, high)) or low)
    inputs = manifest_inputs({"prompt": "x"}, manifest=effective_manifest(manifest, AudioLimits()))
    assert asked == [(3, 9)] and inputs["seed"] == 3


def test_fractional_seed_bounds_draw_only_the_integers_inside(monkeypatch):
    manifest = audio_manifest("fx", {"prompt": [("1", "text")], "seed": [("1", "seed")]})
    manifest["inputs"]["seed"].update(minimum=3.5, maximum=9.5)
    asked: list[tuple[int, int]] = []
    monkeypatch.setattr(generated_defaults, "secure_source", lambda low, high: asked.append((low, high)) or low)
    manifest_inputs({"prompt": "x"}, manifest=effective_manifest(manifest, AudioLimits()))
    assert asked == [(4, 9)]


def test_seed_bounds_outside_the_public_range_are_a_manifest_fault():
    manifest = audio_manifest("fx", {"prompt": [("1", "text")], "seed": [("1", "seed")]})
    manifest["inputs"]["seed"].update(minimum=2**32, maximum=2**33)
    with pytest.raises(ManifestError):
        manifest_inputs({"prompt": "x"}, manifest=effective_manifest(manifest, AudioLimits()))


def test_the_route_answers_a_seed_manifest_fault_with_500_and_submits_nothing(fake_upstreams):
    manifest = audio_manifest("fx", {"prompt": [("1", "text")], "seed": [("1", "seed")]})
    manifest["inputs"]["seed"].update(minimum=2**32, maximum=2**33)
    response = _call({"model": "fx", "prompt": "x"}, manifest=manifest, template={"1": {"inputs": {"text": ""}}},
                     fake_upstreams=fake_upstreams)
    assert response.status_code == 500
    assert json.loads(response.body)["error"]["type"] == "api_error"
