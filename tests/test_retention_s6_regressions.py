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
    monkeypatch.setattr(proxy, "ADMIN_AUTH", lambda _request: AdminContext(
        subject="owner", roles=frozenset({"owner"}), auth_method="session"))
    return receipts, facts, registry


def request(person="owner", *, connection=None):
    auth = proxy.AuthContext(identity=RuntimeIdentity(
        name="test", key_hash="fixture-hash", person_id=person, connection_id=connection))
    return Request({"type": "http", "method": "GET", "path": "/gateway/v1/me/retention/ledger",
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


def test_owner_app_key_cannot_read_real_rest_or_mcp_ledger(stores, monkeypatch):
    from gateway.admin_api import AdminAuthError
    def no_browser(_request):
        raise AdminAuthError("browser session required")
    monkeypatch.setattr(proxy, "ADMIN_AUTH", no_browser)
    req, auth = request()
    with pytest.raises(PermissionError):
        proxy._gateway_retention_ledger(req)
    with pytest.raises(PermissionError):
        proxy._mcp_retention_ledger(req, auth, None)


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
    proxy._capture_retention_facts(row)
    row.finish("succeeded")
    proxy._record_terminal_retention_receipt(row, path=stores[0])
    assert receipt_store.get(row.request_id) is not None
    view = proxy._gateway_retention_ledger(request()[0], row.request_id)
    assert view["fact_basis"].startswith("Today's facts")


def test_missing_saved_revisions_show_no_longer_kept(stores):
    row = RouteReceipt(protocol="test", intent="test", backend="destination", retention_facts=["f" * 64])
    receipt_store.record(row, path=stores[0])
    view = proxy._gateway_retention_ledger(request()[0], row.request_id)
    assert "no longer kept" in view["fact_basis"].lower()
    assert not any(d["facts"] for d in view["destinations"])
