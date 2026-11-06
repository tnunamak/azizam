"""Parity: the graph the portable ``audio.generate`` path submits is the graph ``proxy.proxy_audio_generate`` submits.

The route now compiles through the same adapter, so the route-versus-adapter rows check the route edge only. The
independent oracle is the golden table at the end: the graphs the route sent before it moved off ``_meta``.

The oracle is the live route. The proxy's httpx client is replaced by one that records the ``/prompt`` body and stops
the call, so what is compared is what the route really sends. The candidate is the node-graph adapter's ``submit`` for
``audio.generate``, which sends through the in-process fake engine. Both bodies are dumped as canonical JSON (sorted
keys, no spaces) and compared byte for byte. The workflow template and manifest are the committed files; the proxy's
own manifest and UI-derivation lookups are patched to return the same values the candidate receives.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

import httpx
import pytest

import proxy
from gateway.engine_protocol import audio_graph, description
from gateway.engine_protocol.audio_graph import AudioLimits
from gateway.engine_protocol.node_graph import NodeGraphAdapter
from gateway.engine_protocol.types import CanonicalRequest, Connection, Timeouts
from gateway.manifests import load_workflow_manifest
from tests.support.access import access_context
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parent.parent
HOST = "graph.test:7890"
ORACLE_HOST = "oracle.test:7890"
CONN = Connection(address=f"http://{HOST}", timeouts=Timeouts(connect_s=10, first_byte_s=300, total_s=300))
PINNED_RANDOM_SEED = 4242


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _manifest(name: str) -> dict[str, Any]:
    return load_workflow_manifest(ROOT / "manifests" / "workflows" / f"{name}.yml")


def _ui_twin(api: Mapping[str, Any]) -> dict[str, Any]:
    """A stand-in UI graph: derived from the submitted graph, so a difference in the graph shows here too."""
    return {"nodes": sorted(api), "classes": sorted(str(node.get("class_type")) for node in api.values())}


def _capture_prompts(fake_upstreams, host: str, sink: list[Any]) -> None:
    """The fake engine at ``host``: records each prompt it receives, then breaks the connection so the route stops."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/prompt":
            sink.append(json.loads(request.content))
            raise httpx.ReadError("the parity capture stops the route here", request=request)
        return httpx.Response(404)

    fake_upstreams.router.add_transport(host, HandlerTransport(answer))


class _JsonRequest:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return copy.deepcopy(self._body)


def _proxy_submission(model: str, body: dict[str, Any], *, with_ui: bool, fake_upstreams) -> Any:
    """The prompt body ``proxy_audio_generate`` submits for this request today."""
    sink: list[Any] = []
    _capture_prompts(fake_upstreams, ORACLE_HOST, sink)
    manifest = _manifest(_workflow_name(model))

    async def route() -> None:
        try:
            await proxy.proxy_audio_generate(_JsonRequest({"model": model, **body}), access_context())
        except httpx.HTTPError:
            pass

    with patch.object(proxy, "COMFY_ENABLED", True), \
            patch.object(proxy, "COMFY_HOST", f"http://{ORACLE_HOST}"), \
            patch.object(proxy, "_service_availability_response", new=lambda *_a, **_k: None), \
            patch.object(proxy, "_load_audio_workflow_manifest", new=lambda _name: copy.deepcopy(manifest)), \
            patch.object(proxy, "_derive_ui_workflow", new=(_ui_twin if with_ui else (lambda _api: None))), \
            patch.object(proxy, "configured_downstream_headers", new=lambda *_a, **_k: {}):
        asyncio.run(route())
    assert len(sink) == 1, f"the route submitted {len(sink)} prompts"
    return sink[0]


def _limits(cfg: Mapping[str, Any]) -> AudioLimits:
    """The deployment's duration policy as the proxy config states it; a key the config omits stays unset."""
    def number(key: str) -> float | None:
        return float(cfg[key]) if key in cfg else None

    return AudioLimits(default_duration=number("default_duration"), min_seconds=number("min_seconds"),
                       max_seconds=number("max_seconds"))


def _effective_for_test(manifest: dict[str, Any], limits: AudioLimits) -> dict[str, Any]:
    """The one effective input spec of the workflow (D1 fix 4): both the edge and the compiler read it."""
    return audio_graph.effective_manifest(manifest, limits)


def _workflow_name(model: str) -> str:
    name, cfg = proxy._resolve_audio_workflow(model)
    assert cfg is not None, model
    return name


def _adapter_submission(model: str, body: dict[str, Any], *, with_ui: bool, fake_upstreams) -> Any:
    """The prompt body the portable path submits, read off the wire of the in-process fake engine.

    The route edge (``audio_graph.manifest_inputs``) reads the public body into the manifest's typed inputs; the adapter
    compiles and posts them. Nothing else is applied.
    """
    posted: list[Any] = []

    def answer(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": "parity"})

    fake_upstreams.router.add_transport(HOST, HandlerTransport(answer))
    name = _workflow_name(model)
    cfg = proxy._audio_workflow_table()[name]
    manifest = _manifest(name)
    assert manifest["bindings"][proxy._GRAPH_BINDING]["workflow"] == cfg["file"], "manifest and config name different graphs"
    doc = description.load_registry(description.REGISTRY_DIR)["node-graph"]
    adapter = NodeGraphAdapter(doc, workflow_root=ROOT, client_id=proxy.COMFY_CLIENT_ID,
                               derive_ui=(_ui_twin if with_ui else None))
    effective = _effective_for_test(manifest, _limits(cfg))
    inputs = audio_graph.manifest_inputs(dict(body, model=model), manifest=effective)
    request = CanonicalRequest(op="audio.generate", body=inputs, params={"manifest": effective})
    ref = asyncio.run(adapter.submit(CONN, "audio.generate", request))
    assert ref.id == "parity"
    assert len(posted) == 1
    return posted[0]


# (model, request body, derive a UI twin, pin the random seed used when the request names none)
ROWS = [
    pytest.param("tangoflux", {"prompt": "a door creaks", "duration": 4, "seed": 1234}, False, False,
                 id="tangoflux-prompt-duration-seed"),
    pytest.param("tangoflux", {"input": "rain on a tin roof", "seed": 7}, True, False,
                 id="tangoflux-input-default-duration-with-ui"),
    pytest.param("tangoflux", {"tags": "thunder rolling", "duration": 6.5}, False, True,
                 id="tangoflux-tags-as-prompt-random-seed"),
    pytest.param("tangoflux", {"prompt": "  glass shatters  ", "type": "sfx", "duration": "7", "seed": 0}, False, False,
                 id="tangoflux-padded-prompt-string-duration-seed-zero"),
    pytest.param("tangoflux", {"prompt": "a bell", "duration": 3, "seed": 99,
                               "extensions": {"com.azizam.workflow": {"steps": 20, "guidance": 3.5}}}, False, False,
                 id="tangoflux-extension-steps-guidance"),
    pytest.param("tangoflux", {"prompt": "a bell", "seed": 2**32 - 1,
                               "extensions": {"com.azizam.workflow": {"batch_size": 2}}}, True, False,
                 id="tangoflux-extension-batch-max-seed-with-ui"),
    pytest.param("ace-step-1.5-xl-turbo", {"prompt": "generic music prompt", "tags": "dream pop, piano",
                                           "lyrics": "first line\nsecond line", "duration": 12, "seed": 1234},
                 False, False, id="ace-prompt-tags-lyrics-duration-seed"),
    pytest.param("ace-step-1.5-xl-turbo", {"tags": "dream pop", "lyrics": "la la", "seed": 5}, True, False,
                 id="ace-lyrics-tags-prompt-fallback-with-ui"),
    pytest.param("ace-step-1.5-xl-turbo", {"input": "  a slow waltz  ", "duration": 20, "seed": 3}, False, False,
                 id="ace-input-padded"),
    # yue2-song has no `prompt` input: the route writes its prompt into the node the `tags` input maps to (node 2).
    pytest.param("yue2-song", {"tags": "dream pop, piano", "lyrics": "la la", "duration": 60, "seed": 3}, False, False,
                 id="yue-tags-lyrics-duration-seed"),
    pytest.param("yue2-song", {"prompt": "a sad song about rain", "duration": 120, "seed": 9}, True, False,
                 id="yue-prompt-as-tags-with-ui"),
    pytest.param("yue2-song", {"prompt": "x", "tags": "", "seed": 1}, False, False,
                 id="yue-empty-tags-wins-over-prompt"),
    pytest.param("yue2-song", {"input": "a waltz", "lyrics": "one two"}, False, True,
                 id="yue-input-default-duration-random-seed"),
    # sa3-small-sfx: its manifest default (10) and the deployed config default (10) agree; the rows pin both paths.
    pytest.param("sa3-small-sfx", {"prompt": "a bell", "seed": 7}, False, False,
                 id="sa3-omitted-duration-explicit-seed"),
    pytest.param("sa3-small-sfx", {"prompt": "a door creaks", "duration": 5, "seed": 7}, True, False,
                 id="sa3-explicit-duration-explicit-seed-with-ui"),
    pytest.param("sa3-small-sfx", {"prompt": "rain on glass", "duration": 120, "seed": 0}, False, False,
                 id="sa3-duration-at-deployed-max"),
]


@pytest.mark.parametrize(("model", "body", "with_ui", "pin_random"), ROWS)
def test_portable_graph_matches_the_graph_the_route_submits(model, body, with_ui, pin_random, fake_upstreams, monkeypatch):
    if pin_random:
        monkeypatch.setattr(random, "randint", lambda low, high: PINNED_RANDOM_SEED)
    expected = _proxy_submission(model, body, with_ui=with_ui, fake_upstreams=fake_upstreams)
    actual = _adapter_submission(model, body, with_ui=with_ui, fake_upstreams=fake_upstreams)
    assert _canonical(actual) == _canonical(expected)


@pytest.mark.parametrize(("model", "body", "param"), [
    pytest.param("tangoflux", {"prompt": "x", "seed": -1}, "seed", id="seed-out-of-range"),
    pytest.param("tangoflux", {"prompt": "x", "duration": 500}, "duration", id="duration-over-max"),
    pytest.param("tangoflux", {"prompt": "x", "type": "opera"}, "type", id="unknown-type"),
    pytest.param("tangoflux", {"prompt": "x", "stray": True}, "stray", id="unknown-top-level-field"),
    pytest.param("sa3-small-sfx", {"prompt": "x", "duration": 121}, "duration", id="sa3-duration-over-deployed-max"),
])
def test_refused_requests_name_the_same_field_as_the_route(model, body, param, fake_upstreams):
    """The route edge and the route refuse the same requests, for the same field, and nothing reaches the engine."""
    name = _workflow_name(model)
    effective = _effective_for_test(_manifest(name), _limits(proxy._audio_workflow_table()[name]))
    with pytest.raises(audio_graph.InvalidAudioRequest) as refused:
        audio_graph.manifest_inputs(dict(body, model=model), manifest=effective)
    assert refused.value.param == param
    sink: list[Any] = []
    _capture_prompts(fake_upstreams, ORACLE_HOST, sink)

    async def route():
        return await proxy.proxy_audio_generate(_JsonRequest({"model": model, **body}), access_context())

    with patch.object(proxy, "COMFY_ENABLED", True), \
            patch.object(proxy, "COMFY_HOST", f"http://{ORACLE_HOST}"), \
            patch.object(proxy, "_service_availability_response", new=lambda *_a, **_k: None):
        response = asyncio.run(route())
    assert response.status_code == 400
    error = json.loads(response.body.decode("utf-8"))["error"]
    assert error.get("param") == param
    assert sink == [], "a refused request reached the engine"


GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "audio_route_graphs.json").read_text(encoding="utf-8"))["rows"]


@pytest.mark.parametrize(("model", "body", "with_ui", "pin_random"), ROWS)
def test_the_route_still_submits_the_graph_the_meta_path_submitted(model, body, with_ui, pin_random, request,
                                                                    fake_upstreams, monkeypatch):
    """The independent oracle: the route compiled through the manifest sends what the retired ``_meta`` injection sent.

    The goldens were captured from the route before it moved (tests/fixtures/audio_route_graphs.json), so this does not
    compare the new path with itself."""
    if pin_random:
        monkeypatch.setattr(random, "randint", lambda low, high: PINNED_RANDOM_SEED)
    actual = _proxy_submission(model, body, with_ui=with_ui, fake_upstreams=fake_upstreams)
    assert actual.pop("client_id") == proxy.COMFY_CLIENT_ID  # drawn once per process, so not in the golden
    assert _canonical(actual) == _canonical(GOLDEN[request.node.callspec.id])
