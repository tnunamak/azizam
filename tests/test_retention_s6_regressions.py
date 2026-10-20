"""Real gateway regressions for retention authority and receipt evidence."""
import asyncio
import copy
import sqlite3

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, media_jobs, receipt_store, retention_facts
from gateway.admin_api import AdminContext
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt, serialize_receipt_for_audience
from gateway.runtime_auth import RuntimeIdentity


@pytest.fixture
def stores(tmp_path, monkeypatch):
    admin = tmp_path / "admin.db"
    receipts = tmp_path / "receipts.db"
    facts = tmp_path / "media.db"
    admin_store.init_db(admin)
    receipt_store.init_db(receipts)
    media_jobs.init_db(facts)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    monkeypatch.setattr(media_jobs, "DB_PATH", facts)
    # Store function defaults bind at import; use the real store with this test's path.
    real_get = receipt_store.get
    real_begin = receipt_store.begin
    real_finalize = receipt_store.finalize
    monkeypatch.setattr(receipt_store, "get", lambda *a, **kw: real_get(*a, **{"path": receipts, **kw}))
    monkeypatch.setattr(receipt_store, "begin", lambda *a, **kw: real_begin(*a, **{"path": receipts, **kw}))
    monkeypatch.setattr(receipt_store, "finalize", lambda *a, **kw: real_finalize(*a, **{"path": receipts, **kw}))
    registry = {"services": {"destination": {"name": "Test service", "config": {
        "connection": {"protocol": "node-graph", "base_url": "http://127.0.0.1:18188"},
    }}}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(admin_store, "effective_admin_roles", lambda *_a, **_kw: frozenset({"owner"}))
    monkeypatch.setattr(proxy, "ADMIN_AUTH", lambda req: AdminContext(
        subject=proxy._gateway_receipt_viewer(req), roles=frozenset({"owner"}), auth_method="session"))
    return receipts, facts, registry


def request(person="owner", *, connection=None):
    auth = proxy.AuthContext(identity=RuntimeIdentity(
        name="test", key_hash="fixture-hash", person_id=person, connection_id=connection))
    return Request({"type": "http", "method": "GET", "path": "/admin/api/me/retention/ledger",
                    "headers": [], "auth_context": auth}), auth


@pytest.mark.parametrize("viewer", ["owner", "admin", "maker"])
def test_real_gateway_ledger_reuses_receipt_maker_mask(stores, viewer):
    receipts, _, _ = stores
    row = RouteReceipt(protocol="test", intent="test", backend="destination", maker_person_id="maker")
    row.set_retention(kind="library_prompt", decision="kept", reason="kept", kept_until_ms=123456, parts=["accepted"], limited_by="member_choice")
    receipt_store.record(row, path=receipts)
    view = proxy._gateway_retention_ledger(request(viewer)[0], row.request_id)
    kind = next(k for k in view["kinds"] if k["id"] == "library_prompt")
    assert kind["decision"] == "kept"
    assert ("kept_until_ms" in kind) is (viewer == "maker")
    assert ("reason" in kind) is (viewer == "maker")


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_owner_app_key_cannot_read_real_rest_or_mcp_ledger(stores, monkeypatch, surface):
    from gateway.admin_api import AdminAuthError
    def no_browser(_request):
        raise AdminAuthError("browser session required")
    monkeypatch.setattr(proxy, "ADMIN_AUTH", no_browser)
    req, auth = request()
    if surface == "rest":
        from gateway.api import create_gateway_router, create_request_retention_router
        from gateway.discovery import WorkflowRegistry
        app = FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.scope["auth_context"] = auth
            return await call_next(request)
        app.include_router(create_gateway_router(WorkflowRegistry()))
        app.include_router(create_request_retention_router(ledger_provider=proxy._gateway_retention_ledger))
        client = TestClient(app)
        assert client.get("/admin/api/me/retention/ledger").status_code == 403
        assert client.get("/admin/api/requests/anything/retention/ledger").status_code == 403
    else:
        from gateway import mcp_server
        from types import SimpleNamespace
        deps = SimpleNamespace(retention_visible=lambda *_: True, retention_ledger=proxy._mcp_retention_ledger)
        result = asyncio.run(mcp_server.call_tool(deps, req, auth, "requests", {"action": "ledger"}))
        assert result["isError"] is True


def test_oauth_management_identity_can_read_real_mcp_ledger(stores, monkeypatch):
    from gateway.admin_api import AdminAuthError
    monkeypatch.setattr(proxy, "ADMIN_AUTH", lambda _: (_ for _ in ()).throw(AdminAuthError()))
    req, auth = request(connection="oauth-connection")
    assert proxy._mcp_retention_ledger(req, auth, None)["destinations"]


@pytest.mark.parametrize("protocol", ["openai.chat", "openai.responses", "openai.audio.transcriptions",
                                      "openai.audio.speech", "openai.audio", "openai.video"])
def test_lifecycle_freezes_captured_facts_for_every_receipt_kind(stores, protocol):
    _, facts, registry = stores
    row = RouteReceipt(intent="test", backend="destination", protocol=protocol)
    original = copy.deepcopy(retention_facts.ledger(registry, service_id="destination"))
    async def run():
        async with ReceiptLifecycleOwner(row) as owner:
            proxy._capture_retention_facts(row)
            registry["services"]["destination"]["name"] = "Changed after dispatch"
            registry["services"]["destination"]["config"]["retention_facts"] = []
            owner.report(status="succeeded", reason="completed")
    asyncio.run(run())
    stored = receipt_store.get(row.request_id, audience="internal")
    assert stored["retention_facts"]
    saved = media_jobs.fact_revisions_for_source("receipt", row.request_id, path=facts)
    assert {f["revision"] for fs in saved.values() for f in fs} == set(stored["retention_facts"])
    assert media_jobs.fact_destinations_for_source("receipt", row.request_id, path=facts)["destination"]["name"] == "Test service"
    assert saved["destination"][0]["holds"] == original["destinations"][-1]["facts"][0]["holds"]


def test_client_cannot_fingerprint_retention_destination():
    row = RouteReceipt(protocol="test", intent="test", retention_facts=["a" * 64])
    assert "retention_facts" not in serialize_receipt_for_audience(row.to_dict(), "client")
    assert serialize_receipt_for_audience(row.to_dict(), "admin")["retention_facts"] == ["a" * 64]


@pytest.mark.parametrize("failure", ["invalid_config", "locked_database"])
def test_retention_capture_and_terminal_are_best_effort(stores, monkeypatch, failure):
    _, _, registry = stores
    if failure == "invalid_config":
        registry["services"]["destination"]["config"]["retention_facts"] = [{"unexpected": True}]
    else:
        monkeypatch.setattr(media_jobs, "save_fact_revisions", lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        monkeypatch.setattr(media_jobs, "save_fact_snapshot", lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")), raising=False)
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    receipt_store.begin(row)
    proxy._capture_retention_facts(row)
    row.finish("succeeded")
    proxy._record_terminal_retention_receipt(row, path=stores[0])
    assert receipt_store.get(row.request_id) is not None
    view = proxy._gateway_retention_ledger(request()[0], row.request_id)
    if failure == "invalid_config":
        assert next(d for d in view["destinations"] if d["id"] == "destination")["facts"] == []
        assert any(d["facts"] for d in view["destinations"] if d["id"] != "destination")
        assert view["fact_basis"] == "Facts saved for this request. Some destination facts are unavailable."
    else:
        assert view["fact_basis"].startswith("Today's facts")


def test_missing_saved_revisions_show_no_longer_kept(stores):
    row = RouteReceipt(protocol="test", intent="test", backend="destination", retention_facts=["f" * 64])
    receipt_store.record(row, path=stores[0])
    view = proxy._gateway_retention_ledger(request()[0], row.request_id)
    assert "no longer kept" in view["fact_basis"].lower()
    assert not any(d["facts"] for d in view["destinations"])


def test_protocol_loopback_freezes_outer_receipt_facts(stores):
    outer = RouteReceipt(protocol="openai.responses", intent="chat.responses")
    child = RouteReceipt(protocol="openai.chat", intent="chat.generate", backend="destination",
                         parent_request_id=outer.request_id)
    auth = proxy.AuthContext(retention_receipt=outer)
    async def run():
        async with ReceiptLifecycleOwner(outer) as owner:
            async with ReceiptLifecycleOwner(child) as inner:
                proxy._capture_retention_facts(child, auth_context=auth)
                stores[2]["services"]["destination"]["config"]["retention_facts"] = []
                inner.report(status="succeeded", reason="completed")
            owner.report(status="succeeded", reason="completed")
    asyncio.run(run())
    assert outer.backend == "destination"
    assert outer.retention_facts == child.retention_facts
    assert outer.retention_facts
    assert media_jobs.fact_revisions_for_source("receipt", outer.request_id, path=stores[1])


def test_settings_ledger_survives_invalid_legacy_facts(stores):
    from fastapi import APIRouter
    from types import SimpleNamespace
    from gateway import retention_fact_api
    stores[2]["services"]["destination"]["config"]["retention_facts"] = [{"bad": "legacy"}]
    app = FastAPI()
    router = APIRouter()
    retention_fact_api.register_routes(router, store=proxy.ADMIN_STORE,
        guard=lambda _: SimpleNamespace(roles={"owner"}), read_json=lambda request: request.json())
    app.include_router(router)
    response = TestClient(app).get("/settings/retention/ledger")
    assert response.status_code == 200
    assert response.json()["ledger"]["fact_basis"].startswith("Today's facts")


def test_default_lifecycle_captures_facts_without_route_callback(stores, monkeypatch):
    from gateway import service_registry
    monkeypatch.setattr(service_registry, "load_service_registry", lambda **_: stores[2])
    monkeypatch.setattr(admin_store, "DB_PATH", proxy.ADMIN_STORE.path)
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    async def run():
        async with ReceiptLifecycleOwner(row) as owner:
            stores[2]["services"]["destination"]["config"]["retention_facts"] = []
            owner.report(status="succeeded", reason="completed")
    asyncio.run(run())
    assert row.retention_facts
    assert media_jobs.fact_revisions_for_source("receipt", row.request_id, path=stores[1])["destination"]


def test_full_service_fact_limit_does_not_break_media_finalization(stores):
    from gateway import media_ingestion
    fact = retention_facts.ledger(stores[2])["destinations"][-1]["facts"][0]
    fact = {k: v for k, v in fact.items() if k not in {"revision", "stale", "stale_label", "source"}}
    stores[2]["services"]["destination"]["config"]["retention_facts"] = [
        {**fact, "id": f"fact-{index}"} for index in range(100)]
    row = RouteReceipt(protocol="openai.audio", intent="audio.generate", backend="destination")
    receipt_store.begin(row)
    proxy._capture_retention_facts(row)
    media_ingestion.finish_receipt(row, status_code=200)
    stored = receipt_store.get(row.request_id)
    assert stored["status"] == "succeeded"
    assert len(stored["retention_facts"]) > 100


def test_former_destination_with_no_facts_is_preserved(stores):
    stores[2]["services"]["destination"]["config"]["retention_facts"] = []
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    proxy._capture_retention_facts(row)
    proxy._record_retention_receipt(row, path=stores[0])
    del stores[2]["services"]["destination"]
    view = proxy._gateway_retention_ledger(request()[0], row.request_id)
    saved = next(d for d in view["destinations"] if d["id"] == "destination")
    assert saved["name"] == "Test service"
    assert saved["facts"] == []


@pytest.mark.parametrize("operation", ["prune", "save", "maintain"])
def test_busy_fact_cleanup_commits_expiry_and_sweeps_orphans(stores, monkeypatch, operation):
    import time
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    proxy._capture_retention_facts(row)
    proxy._record_retention_receipt(row, path=stores[0], created_at=int(time.time()) - 400 * 86400)
    admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY, {"retention_days": 30}, path=proxy.ADMIN_STORE.path)
    def run():
        if operation == "prune":
            return receipt_store.prune(1, path=stores[0], fact_store_path=stores[1])
        if operation == "save":
            preview = receipt_store.retention_preview(30, path=stores[0])
            return receipt_store.save_retention(30, preview, lambda: None, path=stores[0], fact_store_path=stores[1])
        return receipt_store.maintain(proxy.ADMIN_STORE.path, path=stores[0], fact_store_path=stores[1])
    real_release = media_jobs.release_fact_reference_batch
    monkeypatch.setattr(media_jobs, "release_fact_reference_batch", lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    from gateway import request_connections
    with request_connections.request_scope():
        assert run() == 1
        assert receipt_store.get(row.request_id) is None
    monkeypatch.setattr(media_jobs, "release_fact_reference_batch", real_release)
    now = time.time()
    monkeypatch.setattr(receipt_store.time, "time", lambda: now + 301)
    assert receipt_store.maintain(proxy.ADMIN_STORE.path, path=stores[0], fact_store_path=stores[1]) == 0
    assert media_jobs.fact_revisions_for_source("receipt", row.request_id, path=stores[1]) == {}


def test_empty_snapshot_stays_empty_after_all_facts_are_removed(stores):
    for system in ("gateway", "litellm"):
        admin_store.put_retention_fact_override("system", system, [], updated_by="owner", path=proxy.ADMIN_STORE.path)
    stores[2]["services"]["destination"]["config"]["retention_facts"] = []
    row = RouteReceipt(protocol="test", intent="test", backend="destination")
    proxy._capture_retention_facts(row)
    proxy._record_retention_receipt(row, path=stores[0])
    stores[2]["services"] = {}
    view = proxy._gateway_retention_ledger(request()[0], row.request_id)
    assert view["fact_basis"] == "Facts saved for this request."
    assert next(d for d in view["destinations"] if d["id"] == "destination")["facts"] == []
