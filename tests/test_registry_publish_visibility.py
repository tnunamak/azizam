"""Acceptance rows 2, 12 and 20 (CODE_TO_DATA_S4_S8, 5.9 and S5), through the real routes.

Row 2: an unpublished workflow is in the owner's views and absent from App catalogs, the App OpenAPI document and the MCP
tools; publishing it puts it in each. Row 12: a workflow edit changes the OpenAPI document. Row 20: a frontend-only or
alias-only edit changes the snapshot digest and rebuilds the document. Only the registry snapshot is swapped; the
harness is the same signed-in gateway the other route tests use.
"""

from __future__ import annotations

import asyncio

import httpx

import proxy
from gateway import registry_snapshot as rs
from tests.test_event_loop_guard import gateway  # noqa: F401 - fixture

WORKFLOW_ID = "mesh.generate.trellis-2"


def _get(path: str, **caller) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20, **caller) as client:
            return await client.get(path)

    return asyncio.run(call())


def _swap(monkeypatch, *, published: bool, contract_edit=None, services=None):
    """Serve the bundled release with WORKFLOW_ID stored over it, published or not."""
    release = rs.bundled_release()
    manifest = dict(release.workflows[WORKFLOW_ID].manifest)
    manifest.update(contract_edit or {})
    record = {"id": WORKFLOW_ID, "manifest": manifest, "enabled": True, "published": published, "overrides": WORKFLOW_ID,
              "policy": {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "media", "tags": []}}
    snapshot = rs.build({"workflows": {WORKFLOW_ID: record}, "services": services or {}}, release, generation=2)
    monkeypatch.setattr(proxy, "GATEWAY_REGISTRY_PROVIDER", rs.Provider(snapshot))
    # The cached document and its inputs key belong to this snapshot; the undo hands the next test a cold cache.
    monkeypatch.setattr(proxy.app, "openapi_schema", None)
    monkeypatch.setattr(proxy, "_OPENAPI_INPUTS", [""])
    return snapshot


def _gateway_ids(**caller) -> set[str]:
    answer = _get("/gateway/v1/workflows", **caller)
    assert answer.status_code == 200, answer.text
    return {row["id"] for row in answer.json()["workflows"]}


def _openapi_ids() -> set[str]:
    return {row["id"] for row in proxy.app.openapi()["x-azizam-workflows"]}


def _mcp_ids() -> set[str]:
    return {row["id"] for row in proxy._mcp_catalog()}


def test_an_unpublished_workflow_is_absent_from_app_views_and_present_after_publish(gateway, monkeypatch):  # noqa: F811
    try:
        _swap(monkeypatch, published=False)
        assert WORKFLOW_ID not in _gateway_ids(headers=gateway["bearer"])
        assert WORKFLOW_ID not in _openapi_ids()
        assert WORKFLOW_ID not in _mcp_ids()
        assert WORKFLOW_ID not in {row["id"] for row in _get("/me/api/workflows", cookies=gateway["cookies"]).json()["workflows"]}
        _swap(monkeypatch, published=True)
        assert WORKFLOW_ID in _gateway_ids(headers=gateway["bearer"])
        assert WORKFLOW_ID in _openapi_ids()
        assert WORKFLOW_ID in _mcp_ids()
    finally:
        proxy.app.openapi_schema = None


def test_the_owner_audience_sees_an_unpublished_workflow_and_the_client_audience_does_not():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gateway.api import create_gateway_router

    release = rs.bundled_release()
    manifest = dict(release.workflows[WORKFLOW_ID].manifest)
    record = {"id": WORKFLOW_ID, "manifest": manifest, "published": False, "overrides": WORKFLOW_ID, "policy": {
        "exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "media", "tags": []}}
    provider = rs.Provider(rs.build({"workflows": {WORKFLOW_ID: record}}, release, generation=2))
    seen = {}
    for audience in ("client", "admin"):
        app = FastAPI()
        app.include_router(create_gateway_router(provider, audience_resolver=lambda _r, a=audience: a))
        client = TestClient(app)
        seen[audience] = (client.get("/gateway/v1/workflows").json()["workflows"], client.get(f"/gateway/v1/workflows/{WORKFLOW_ID}").status_code)
    assert WORKFLOW_ID not in {row["id"] for row in seen["client"][0]} and seen["client"][1] == 404
    assert WORKFLOW_ID in {row["id"] for row in seen["admin"][0]} and seen["admin"][1] == 200


def test_a_workflow_edit_changes_the_openapi_document(gateway, monkeypatch):  # noqa: F811
    try:
        _swap(monkeypatch, published=True)
        before = proxy.app.openapi()
        _swap(monkeypatch, published=True, contract_edit={"display_name": "Edited display name"})
        after = proxy.app.openapi()
        assert before != after
        assert {r["id"]: r for r in after["x-azizam-workflows"]}[WORKFLOW_ID] != {r["id"]: r for r in before["x-azizam-workflows"]}[WORKFLOW_ID]
    finally:
        proxy.app.openapi_schema = None


def test_a_cached_document_is_rebuilt_when_only_the_snapshot_digest_changes(gateway, monkeypatch):  # noqa: F811
    try:
        first = _swap(monkeypatch, published=True)
        built = proxy.app.openapi()
        assert proxy.app.openapi() is built
        alias_only = {"svc": {"config": {"models": [{"id": "new-alias", "base_model_id": "base"}, {"id": "base"}]}}}
        second = _swap(monkeypatch, published=True, services=alias_only)
        assert second.digest != first.digest
        proxy.app.openapi_schema = built      # a document cached for the old digest
        assert proxy.app.openapi() is not built
    finally:
        proxy.app.openapi_schema = None
