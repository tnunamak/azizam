"""Discovery of POST /v1/meshes/generate: the mesh capability row, its live probe, and its public projection.

The row comes from the frontend record and the trellis service record (the same resolver as the route); the
probe reads the service health and the observer's facts and acquires no capacity. Only the fakes from
test_mesh_route.py stand in for the network.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from gateway.capabilities import Capability, summarize_capabilities, summarize_capability
from gateway.mesh_api import discovery_capability
from tests.test_mesh_route import GLB, GOOD_CAPACITY, OBSERVER_URL, TRELLIS_URL, TOTAL, MIB, Harness


def _discover(harness: Harness) -> Capability:
    return asyncio.run(discovery_capability(harness.deps()))


def _set_service(harness: Harness, service_id: str, mutate) -> None:
    record = copy.deepcopy(harness.services[service_id])
    mutate(record)
    harness.services[service_id] = record


def test_a_healthy_pool_lists_the_mesh_row_with_its_media_type_and_route_tiers() -> None:
    capability = _discover(Harness())

    assert capability.intent == "mesh.generate"
    assert capability.backend == "trellis"
    assert capability.workflow == "mesh.generate.trellis-2"
    assert capability.capability == "mesh.generate"
    assert capability.protocol == "openai.mesh"
    assert capability.output_media_types == ("model/gltf-binary",)
    assert capability.input_modalities == ("image",)
    assert capability.output_modalities == ("mesh",)
    assert capability.execution == "sync"
    assert capability.exposure == "default"
    assert capability.trust_tier == "stable"
    assert capability.resource_tier == "batch"
    assert capability.lifecycle == "warm"
    assert capability.health == "healthy"
    assert capability.available is True
    assert capability.caveats == ()


def test_discovery_is_read_only_and_takes_no_capacity() -> None:
    harness = Harness()
    harness.downstream_mode = "ok"

    _discover(harness)

    assert harness.leases.active_claims() == 0
    assert harness.downstream == []
    assert harness.receipts == []


def test_the_probe_reaches_the_service_health_and_the_observer_facts() -> None:
    harness = Harness()
    urls: list[str] = []
    original = harness.handler

    def spy(request):
        urls.append(str(request.url))
        return original(request)

    harness.handler = spy
    _discover(harness)

    assert f"{TRELLIS_URL}/health" in urls
    assert f"{OBSERVER_URL}/facts" in urls


@pytest.mark.parametrize("mutate_harness, reason_fragment", [
    (lambda h: setattr(h, "health_status", 503), "health"),
    (lambda h: setattr(h, "observer_status", 503), "observer"),
    (lambda h: setattr(h, "observer_body", {"free": TOTAL + 1, "total": TOTAL}), "observer"),
    (lambda h: setattr(h, "observer_body", {"free": "1", "total": TOTAL}), "observer"),
    (lambda h: setattr(h, "observer_body", ["not", "a", "document"]), "observer"),
    (lambda h: _set_service(h, "vram-observer", lambda r: r.update(enabled=False)), "observer"),
    (lambda h: _set_service(h, "trellis", lambda r: r.update(enabled=False)), "disabled"),
    (lambda h: _set_service(h, "trellis", lambda r: r.update(capabilities=["images"])), "capability"),
    (lambda h: _set_service(h, "trellis", lambda r: r["config"].update(resource_capacity={"enabled": False})), "exclusive"),
    (lambda h: _set_service(h, "trellis", lambda r: r.pop("config")), "resource_capacity"),
    (lambda h: _set_service(h, "trellis", lambda r: r.update(enabled=True, capabilities=["mesh.generate"],
                                                             config={**r.get("config", {}), "base_url": None,
                                                                     "host": None})), "base"),
    (lambda h: setattr(h, "frontends", [f for f in h.frontends if f.get("id") != "mesh"]), "frontend"),
])
def test_an_unavailable_pool_lists_the_row_as_unavailable_with_a_bounded_reason(mutate_harness, reason_fragment) -> None:
    harness = Harness()
    mutate_harness(harness)

    capability = _discover(harness)

    assert capability.lifecycle == "stopped"
    assert capability.health == "unhealthy"
    assert capability.available is False
    assert len(capability.caveats) == 1
    assert reason_fragment in capability.caveats[0]
    # Bounded: the reason never carries a raw body or a secret header.
    assert "internal secret" not in capability.caveats[0]


def test_a_failing_service_probe_is_not_called_a_success_when_the_body_is_garbage() -> None:
    harness = Harness()
    harness.health_status = 200
    original = harness.handler

    def garbage_for_health(request):
        if str(request.url) == f"{TRELLIS_URL}/health":
            raise RuntimeError("socket reset with a raw body")
        return original(request)

    harness.handler = garbage_for_health
    capability = _discover(harness)

    assert capability.available is False
    assert "raw body" not in json.dumps(capability.caveats)


def test_the_client_projection_names_the_protocol_and_media_type_and_availability() -> None:
    capability = _discover(Harness())

    summary = summarize_capability(capability, "client")

    assert summary["output_media_types"] == ["model/gltf-binary"]
    assert summary["protocol"] == "openai.mesh"
    assert summary["workflow"] == "mesh.generate.trellis-2"
    assert summary["available"] is True
    assert summary["availability"]["available"] is True
    assert "backend" not in summary


def test_an_unavailable_projection_says_so_on_the_client_catalog() -> None:
    harness = Harness()
    harness.observer_status = 503

    catalog = summarize_capabilities([_discover(harness)], audience="client")

    (row,) = catalog["capabilities"]
    assert row["available"] is False
    assert row["availability"]["available"] is False
    assert row["output_media_types"] == ["model/gltf-binary"]


def test_capabilities_without_the_new_fields_keep_their_summary_shape() -> None:
    plain = Capability(intent="image.generate", backend="comfyui", workflow="krea2-turbo")

    summary = summarize_capability(plain, "client")

    assert "protocol" not in summary
    assert "output_media_types" not in summary
