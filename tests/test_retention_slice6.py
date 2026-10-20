"""The fact ledger's persistence, authority, and offline read contract."""

from __future__ import annotations

import asyncio
import copy
import socket
import sqlite3
import time
from datetime import date
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, mcp_server, media_jobs, receipt_store, retention_fact_api
from gateway.retention_facts import ledger, revision


def _registry() -> dict:
    return {"services": {
        "local": {"name": "Local graph", "config": {
            "connection": {"protocol": "node-graph", "base_url": "http://127.0.0.1:18188"},
        }},
        "remote": {"name": "Remote provider", "config": {
            "remote_provider": True, "preset_id": "openai", "base_url": "https://example.test/v1",
        }},
    }}


def _client(path, roles: set[str]) -> TestClient:
    admin_store.init_db(path)
    app = FastAPI()
    router = APIRouter(prefix="/admin/api")
    store = SimpleNamespace(path=path, get_service_registry=_registry)
    retention_fact_api.register_routes(
        router, store=store,
        guard=lambda _request: SimpleNamespace(roles=roles, audit_actor_id="test-owner"),
        read_json=lambda request: request.json(),
    )
    app.include_router(router)
    return TestClient(app)


def test_owner_can_remove_seed_and_it_stays_removed_after_database_reopen(tmp_path, monkeypatch):
    path = tmp_path / "admin.db"
    client = _client(path, {"owner"})
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network read")))
    before = client.get("/admin/api/settings/retention/ledger")
    assert before.status_code == 200
    provider = next(row for row in before.json()["ledger"]["destinations"] if row["id"] == "remote")
    assert provider["facts"]
    assert "Checked" in before.json()["ledger"]["never_save_text"]
    assert client.put("/admin/api/providers/remote/retention/facts", json={"retention_facts": []}).status_code == 200
    assert admin_store.retention_fact_overrides(path=path)[("provider", "remote")] == []
    admin_store.init_db(path)
    after = _client(path, {"admin"}).get("/admin/api/settings/retention/ledger")
    assert after.status_code == 200
    assert next(row for row in after.json()["ledger"]["destinations"] if row["id"] == "remote")["facts"] == []
    assert _client(path, {"admin"}).put("/admin/api/providers/remote/retention/facts", json={"retention_facts": []}).status_code == 403


def test_engine_and_system_facts_are_editable_and_stale_is_labelled(tmp_path):
    client = _client(tmp_path / "admin.db", {"owner"})
    current = client.get("/admin/api/engines/local/retention/ledger")
    assert current.status_code == 200
    graph = next(row for row in current.json()["ledger"]["destinations"] if row["id"] == "local")
    assert graph["facts"][0]["id"] == "graph-history"
    assert graph["reach"] == "Azizam reaches it on this computer"
    assert client.put("/admin/api/engines/local/retention/facts", json={"retention_facts": []}).status_code == 200
    assert client.put("/admin/api/settings/retention/systems/litellm/facts", json={"retention_facts": []}).status_code == 200
    view = client.get("/admin/api/settings/retention/ledger").json()["ledger"]
    assert next(row for row in view["destinations"] if row["id"] == "local")["facts"] == []
    assert next(row for row in view["destinations"] if row["id"] == "litellm")["facts"] == []
    stale = ledger(_registry(), kinds={}, now=date(2028, 1, 1))
    assert next(row for row in stale["destinations"] if row["id"] == "local")["facts"][0]["stale_label"] == "This was last checked on 2026-10-19."


def test_fact_revisions_survive_edit_and_receipt_prune_while_kept_record_exists(tmp_path):
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    fact = copy.deepcopy(next(row for row in ledger(_registry(), kinds={})["destinations"] if row["id"] == "remote")["facts"][0])
    fact = {key: value for key, value in fact.items() if key not in {"revision", "stale", "stale_label", "source"}}
    digest = revision(fact)
    media_jobs.save_fact_revisions([fact], source_kind="receipt", source_id="r1", destination_id="remote", path=path)
    media_jobs.save_fact_revisions([fact], source_kind="kept_record", source_id="k1", destination_id="remote", path=path)
    changed = {**fact, "how_long": "edited by owner"}
    media_jobs.save_fact_revisions([changed], source_kind="receipt", source_id="r2", destination_id="remote", path=path)
    assert media_jobs.fact_revisions_for_source("receipt", "r1", path=path)["remote"][0]["how_long"] == fact["how_long"]
    assert media_jobs.release_fact_references("receipt", "r1", path=path) == 0
    assert media_jobs.fact_revisions([digest], path=path)[0]["how_long"] == fact["how_long"]
    assert media_jobs.release_fact_references("kept_record", "k1", path=path) == 1
    assert media_jobs.fact_revisions_for_source("receipt", "r2", path=path)["remote"][0]["how_long"] == "edited by owner"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retention_fact_revisions").fetchone()[0] == 1


def test_admin_store_schema_and_fact_override_are_closed(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 16
        assert {row[1] for row in conn.execute("PRAGMA table_info(retention_fact_overrides)")} >= {"target_type", "target_id", "facts_json"}
    admin_store.put_retention_fact_override("provider", "remote", [], updated_by="owner", path=path)
    assert admin_store.retention_fact_overrides(path=path) == {("provider", "remote"): []}


def test_receipt_pruning_releases_only_its_fact_reference(tmp_path):
    facts_path = tmp_path / "media_jobs.db"
    receipts_path = tmp_path / "receipts.db"
    media_jobs.init_db(facts_path)
    receipt_store.init_db(receipts_path)
    fact = {key: value for key, value in ledger(_registry(), kinds={})["destinations"][-1]["facts"][0].items()
            if key not in {"revision", "stale", "stale_label", "source"}}
    digest = media_jobs.save_fact_revisions([fact], source_kind="receipt", source_id="r1", destination_id="remote", path=facts_path)[0]
    media_jobs.save_fact_revisions([fact], source_kind="kept_record", source_id="k1", destination_id="remote", path=facts_path)
    receipt_store.record({"request_id": "r1", "retention_facts": [digest]}, created_at=int(time.time()) - 60, path=receipts_path)
    assert receipt_store.prune(retain_s=1, path=receipts_path, fact_store_path=facts_path) == 1
    assert media_jobs.fact_revisions_for_source("receipt", "r1", path=facts_path) == {}
    assert media_jobs.fact_revisions([digest], path=facts_path)


def test_mcp_ledger_action_is_hidden_and_rejected_without_admin_scope():
    seen = []
    deps = SimpleNamespace(
        retention_visible=lambda _request, auth: auth == "admin",
        retention_ledger=lambda _request, _auth, request_id: seen.append(request_id) or {"request_id": request_id},
    )
    denied = asyncio.run(mcp_server.call_tool(deps, None, "member", "requests", {"action": "ledger"}))
    assert denied["isError"] is True
    assert seen == []
    allowed = asyncio.run(mcp_server.call_tool(deps, None, "admin", "requests", {"action": "ledger", "request_id": "r1"}))
    assert allowed.get("isError") is not True
    assert seen == ["r1"]
