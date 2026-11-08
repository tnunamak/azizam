"""The mesh row in the signed-in listings: the member catalog that Try reads, and the capability listing.

The mesh row is one row of the shared capability catalog (proxy._mesh_facts). Its state is the pool's live probe, which
joins every listing as availability evidence (proxy.LIVE_PROBES), so the member catalog and the general listing show it
the same way. These tests go through the real handlers with a signed-in owner; only the probe's network is faked, by the
harness from tests/support/mesh_harness.py.
"""

from __future__ import annotations

import asyncio

import httpx

import proxy
from gateway import mesh_api
from tests.test_event_loop_guard import gateway  # noqa: F401 - fixture
from tests.support.mesh_harness import OBSERVER_URL, TRELLIS_URL, Harness

MESH_WORKFLOW = "mesh.generate.trellis-2"
MESH_KEY = ("mesh.generate", MESH_WORKFLOW)


def _get(path: str, **caller) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20, **caller) as client:
            return await client.get(path)

    return asyncio.run(call())


def _member_catalog(cookies) -> dict:
    answer = _get("/me/api/workflows", cookies=cookies)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _capability_listing(headers) -> dict:
    answer = _get("/gateway/v1/capabilities", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _probe_with(monkeypatch, harness: Harness) -> None:
    monkeypatch.setitem(proxy.LIVE_PROBES, "mesh", lambda: mesh_api.pool_evidence(harness.deps()))


def _mesh_rows(catalog: dict) -> list[dict]:
    return [row for row in catalog["workflows"] if row["capability"] == "mesh"]


def test_a_healthy_pool_puts_the_mesh_workflow_in_the_member_catalog_as_ready(gateway, monkeypatch):  # noqa: F811
    _probe_with(monkeypatch, Harness())

    rows = _mesh_rows(_member_catalog(gateway["cookies"]))

    assert [row["id"] for row in rows] == [MESH_WORKFLOW]
    assert rows[0]["availability"]["available"] is True
    assert rows[0]["availability"]["status"] == "ready"


def test_an_unhealthy_pool_lists_the_mesh_workflow_as_not_available(gateway, monkeypatch):  # noqa: F811
    harness = Harness()
    harness.health_status = 503
    _probe_with(monkeypatch, harness)

    rows = _mesh_rows(_member_catalog(gateway["cookies"]))

    assert [row["id"] for row in rows] == [MESH_WORKFLOW]
    assert rows[0]["availability"]["available"] is False
    assert rows[0]["availability"]["status"] == "unavailable"


def test_a_route_that_is_not_set_up_or_turned_off_lists_as_off_not_as_a_fault(gateway, monkeypatch):  # noqa: F811
    # A gateway without the 3D route (no frontend or service record) lists the row as not configured, and a turned-off
    # service as disabled: the Home pill counts neither as an issue.
    harness = Harness()
    harness.frontends = [record for record in harness.frontends if record.get("id") != mesh_api.FRONTEND_ID]
    _probe_with(monkeypatch, harness)
    unset = _mesh_rows(_member_catalog(gateway["cookies"]))[0]["availability"]
    assert (unset["status"], unset["configured"], unset["enabled"]) == ("unavailable", False, True)

    harness = Harness()
    harness.services["trellis"]["enabled"] = False
    _probe_with(monkeypatch, harness)
    off = _mesh_rows(_member_catalog(gateway["cookies"]))[0]["availability"]
    assert (off["status"], off["enabled"]) == ("unavailable", False)


def test_a_listing_probes_the_pool_once_however_many_views_it_builds(gateway, monkeypatch):  # noqa: F811
    harness = Harness()
    urls: list[str] = []
    original = harness.handler

    def spy(request):
        urls.append(str(request.url))
        return original(request)

    harness.handler = spy
    _probe_with(monkeypatch, harness)

    _member_catalog(gateway["cookies"])

    assert urls.count(f"{TRELLIS_URL}/health") == 1
    assert urls.count(f"{OBSERVER_URL}/facts") == 1


def test_both_listings_draw_the_mesh_row_from_the_shared_catalog_for_the_same_caller(gateway, monkeypatch):  # noqa: F811
    _probe_with(monkeypatch, Harness())
    callers: list[proxy.AuthContext] = []
    shared = proxy._gateway_capabilities_for_auth_context

    def recording(auth_context):
        callers.append(auth_context)
        return shared(auth_context)

    monkeypatch.setattr(proxy, "_gateway_capabilities_for_auth_context", recording)

    def listing(fetch):
        # The caller's own catalog: what the shared catalog produces for every caller this listing built.
        callers.clear()
        answer = fetch()
        catalog = {(cap.intent, cap.workflow) for caller in callers for cap in shared(caller)}
        return answer, catalog

    capabilities, capability_keys = listing(lambda: _capability_listing(gateway["bearer"]))
    workflows, workflow_keys = listing(lambda: _member_catalog(gateway["cookies"]))

    listed_capabilities = [(row.get("intent"), row.get("workflow")) for row in capabilities["capabilities"]]
    listed_workflows = [(row.get("intent"), row.get("id")) for row in workflows["workflows"]]
    assert set(listed_capabilities) <= capability_keys
    assert set(listed_workflows) <= workflow_keys
    assert listed_capabilities.count(MESH_KEY) == 1
    assert listed_workflows.count(MESH_KEY) == 1
    # The one row is the route's: a picture in, a GLB back, in one call on the mesh protocol. The registry's general
    # workflow row (a media job taking text) must not stand in for it.
    (row,) = [row for row in capabilities["capabilities"] if (row.get("intent"), row.get("workflow")) == MESH_KEY]
    assert (row["execution"], row["input_modalities"], row["output_modalities"], row["protocol"]) == (
        "sync", ["image"], ["mesh"], mesh_api.MESH_PROTOCOL)
