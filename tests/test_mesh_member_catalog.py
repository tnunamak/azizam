"""The member workflow catalog (GET /me/api/workflows) lists the mesh workflow, so Try can offer Photo to 3D.

The mesh row is not in the shared capability catalog: it carries its own live probe and joins each listing after the
shared projection. The general listing added it; the member catalog that Try reads did not, so the tab stayed hidden
on a healthy gateway. These tests go through the real handler with a signed-in owner; only the probe's network is
faked, by the harness from test_mesh_route.py.
"""

from __future__ import annotations

import asyncio

import httpx

import proxy
from gateway import mesh_api
from tests.test_event_loop_guard import gateway  # noqa: F401 - the signed-in gateway fixture
from tests.test_mesh_route import Harness

MESH_WORKFLOW = "mesh.generate.trellis-2"


def _member_catalog(cookies) -> dict:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=cookies, timeout=20) as client:
            return await client.get("/me/api/workflows")

    answer = asyncio.run(call())
    assert answer.status_code == 200, answer.text
    return answer.json()


def _probe_with(monkeypatch, harness: Harness) -> None:
    deps = harness.deps()
    monkeypatch.setattr(proxy, "MESH_DEPS", deps)


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


def test_the_probe_runs_once_for_a_listing_with_several_views(gateway, monkeypatch):  # noqa: F811
    calls: list[object] = []
    real = mesh_api.discovery_capability

    async def counted(deps):
        calls.append(deps)
        return await real(deps)

    _probe_with(monkeypatch, Harness())
    monkeypatch.setattr(proxy.mesh_api, "discovery_capability", counted)

    _member_catalog(gateway["cookies"])

    assert len(calls) == 1
