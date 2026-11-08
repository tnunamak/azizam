"""The mesh row in the shared catalog: its facts, the pool's live probe as availability evidence, and the row projected from both.

The facts come from the route's own frontend row and the service record (the same resolver as the route); the probe reads
the service health and the observer's facts and acquires no capacity. Only the fakes from tests/support/mesh_harness.py stand in for
the network.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from gateway import mesh_api
from gateway.availability import project_capabilities
from gateway.capabilities import Capability, summarize_capabilities, summarize_capability
from tests.support.mesh_harness import OBSERVER_URL, TOTAL, TRELLIS_URL, Harness


def _facts(harness: Harness) -> Capability:
    return mesh_api.discovery_facts(harness.deps().current_manifest(), harness.frontends)


def _evidence(harness: Harness) -> dict[str, Any]:
    return asyncio.run(mesh_api.pool_evidence(harness.deps()))


def _listing(harness: Harness) -> Capability:
    """The mesh row as the shared catalog lists it: its facts, projected with the pool's live evidence."""
    (row,) = project_capabilities([_facts(harness)], service_records={"services": harness.services},
                                  evidence=_evidence(harness))
    return row


def _set_service(harness: Harness, service_id: str, mutate) -> None:
    record = copy.deepcopy(harness.services[service_id])
    mutate(record)
    harness.services[service_id] = record


def test_the_facts_name_the_route_and_its_tiers_and_carry_no_state() -> None:
    capability = _facts(Harness())

    assert capability.intent == "mesh.generate"
    assert capability.backend == "trellis"
    assert capability.workflow == "mesh.generate.trellis-2"
    assert capability.capability == "mesh"
    assert capability.title == "3D models"
    assert capability.protocol == "openai.mesh"
    assert capability.output_media_types == ("model/gltf-binary",)
    assert capability.input_modalities == ("image",)
    assert capability.output_modalities == ("mesh",)
    assert capability.execution == "sync"
    assert capability.exposure == "default"
    assert capability.trust_tier == "stable"
    assert capability.resource_tier == "batch"
    # The state is the probe's, and joins through the shared projection; the facts hold none.
    assert capability.lifecycle == "unknown"
    assert capability.health == "unknown"
    assert not capability.availability


def test_a_route_without_a_usable_frontend_keeps_the_default_tiers() -> None:
    harness = Harness()
    harness.frontends = [record for record in harness.frontends if record.get("id") != "mesh"]

    capability = _facts(harness)

    assert capability.exposure == "disabled"
    assert capability.trust_tier == "experimental"
    assert capability.resource_tier == "batch"


def test_a_healthy_pool_lists_the_mesh_row_as_ready_and_warm() -> None:
    row = _listing(Harness())

    assert row.lifecycle == "warm"
    assert row.health == "healthy"
    assert row.available is True
    assert row.availability is not None
    assert row.availability["status"] == "ready"
    assert row.availability["available"] is True
    assert row.caveats == ()


def test_concurrent_probes_share_one_health_and_one_observer_probe_within_the_ttl() -> None:
    harness = Harness()
    urls: list[str] = []
    original = harness.handler

    def spy(request):
        urls.append(str(request.url))
        return original(request)

    harness.handler = spy
    deps = harness.deps()

    async def probe_eight_at_once():
        return await asyncio.gather(*(mesh_api.pool_evidence(deps) for _ in range(8)))

    answers = asyncio.run(probe_eight_at_once())
    assert all(answer["trellis"]["available"] for answer in answers)
    assert urls.count(f"{TRELLIS_URL}/health") == 1
    assert urls.count(f"{OBSERVER_URL}/facts") == 1

    # A later listing inside the fresh window reads the answer, and probes nothing.
    assert asyncio.run(mesh_api.pool_evidence(deps))["trellis"]["available"] is True
    assert urls.count(f"{TRELLIS_URL}/health") == 1
    assert urls.count(f"{OBSERVER_URL}/facts") == 1


def test_a_cached_answer_keeps_the_time_its_probe_finished() -> None:
    deps = Harness().deps()

    first = asyncio.run(mesh_api.pool_evidence(deps))["trellis"]
    later = asyncio.run(mesh_api.pool_evidence(deps))["trellis"]

    assert first["source"] == "live_probe"
    assert isinstance(first["observed_at_s"], int)
    assert later["observed_at_s"] == first["observed_at_s"]


def test_a_hanging_health_endpoint_does_not_hold_a_listing_once_an_answer_is_cached(monkeypatch) -> None:
    harness = Harness()
    deps = harness.deps()
    assert asyncio.run(mesh_api.pool_evidence(deps))["trellis"]["available"] is True

    # The cached answer is now due a refresh, and the refresh hangs on the health endpoint.
    monkeypatch.setattr(mesh_api, "POOL_PROBE_FRESH_SECONDS", 0.0)

    async def listing_while_health_hangs():
        release = asyncio.Event()

        async def hang_on_health(request):
            if str(request.url) == f"{TRELLIS_URL}/health":
                await release.wait()

        harness.hook = hang_on_health
        try:
            # The fake transport ignores httpx timeouts, so a listing that waited on the probe would not return.
            return await asyncio.wait_for(mesh_api.pool_evidence(deps), timeout=2.0)
        finally:
            release.set()

    evidence = asyncio.run(listing_while_health_hangs())

    assert evidence["trellis"]["available"] is True
    assert evidence["trellis"]["reason"] == "Service health and observer facts answered."


def test_the_probe_is_read_only_and_takes_no_capacity() -> None:
    harness = Harness()
    harness.downstream_mode = "ok"

    _evidence(harness)

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
    _evidence(harness)

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
    (lambda h: _set_service(h, "trellis", lambda r: r.pop("config")), "exclusive"),
    (lambda h: _set_service(h, "trellis", lambda r: r.update(enabled=True, capabilities=["mesh"],
                                                             config={**r.get("config", {}), "base_url": None,
                                                                     "host": None})), "base"),
    # No frontend record: the route is not set up, which is Off rather than a fault (pool_evidence's setup states).
    (lambda h: setattr(h, "frontends", [f for f in h.frontends if f.get("id") != "mesh"]), "not configured"),
])
def test_an_unavailable_pool_lists_the_row_as_unavailable_with_a_bounded_reason(mutate_harness, reason_fragment) -> None:
    harness = Harness()
    mutate_harness(harness)

    row = _listing(harness)

    assert row.lifecycle == "stopped"
    assert row.health == "unhealthy"
    assert row.available is False
    assert row.caveats == ()
    assert row.availability is not None
    assert reason_fragment in row.availability["reason"]
    # Bounded: the reason never carries a raw body or a secret header.
    assert "internal secret" not in json.dumps(row.availability)


def test_a_failing_service_probe_is_not_called_a_success_when_the_body_is_garbage() -> None:
    harness = Harness()
    harness.health_status = 200
    original = harness.handler

    def garbage_for_health(request):
        if str(request.url) == f"{TRELLIS_URL}/health":
            raise RuntimeError("socket reset with a raw body")
        return original(request)

    harness.handler = garbage_for_health
    row = _listing(harness)

    assert row.available is False
    assert "raw body" not in json.dumps(row.availability)


def test_the_client_projection_names_the_protocol_and_media_type_and_availability() -> None:
    row = _listing(Harness())

    summary = summarize_capability(row, "client")

    assert summary["output_media_types"] == ["model/gltf-binary"]
    assert summary["protocol"] == "openai.mesh"
    assert summary["workflow"] == "mesh.generate.trellis-2"
    assert summary["available"] is True
    assert summary["availability"]["available"] is True
    assert "backend" not in summary


def test_an_unavailable_projection_says_so_on_the_client_catalog() -> None:
    harness = Harness()
    harness.observer_status = 503

    catalog = summarize_capabilities([_listing(harness)], audience="client")

    (row,) = catalog["capabilities"]
    assert row["available"] is False
    assert row["availability"]["available"] is False
    assert row["output_media_types"] == ["model/gltf-binary"]


def test_capabilities_without_the_new_fields_keep_their_summary_shape() -> None:
    plain = Capability(intent="image.generate", backend="comfyui", workflow="krea2-turbo")

    summary = summarize_capability(plain, "client")

    assert "protocol" not in summary
    assert "output_media_types" not in summary


def test_a_route_whose_workflow_record_is_absent_is_listed_unavailable_without_a_probe() -> None:
    harness = Harness()
    harness.workflow_present = False
    harness.health_status = 200
    answer = _evidence(harness)["trellis"]
    assert answer["available"] is False and answer["status"] == "unavailable" and answer["enabled"] is False
    assert answer["reason"] == mesh_api.WORKFLOW_ABSENT
    assert harness.downstream == []
