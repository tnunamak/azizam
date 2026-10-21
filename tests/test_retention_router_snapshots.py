"""Responses outer receipts keep the router's evidence from before outbound I/O."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import proxy
from gateway import admin_store, media_jobs, provider_execution, receipt_store, retention_facts, service_registry
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from gateway.provider_catalog import load_provider_catalog
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt
from tests.support.access import identity as identity_of
from tests.test_provider_execution import _profile
from tests.test_provider_routing import _Client, _Response, _registry, _request


@pytest.mark.parametrize("route", ["local", "paid"])
def test_real_router_freezes_outer_evidence_before_owner_edits_at_egress(tmp_path, monkeypatch, route):
    admin = tmp_path / "admin.db"
    receipts = tmp_path / "receipts.db"
    facts_db = tmp_path / "facts.db"
    admin_store.init_db(admin)
    receipt_store.init_db(receipts)
    media_jobs.init_db(facts_db)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", admin)
    monkeypatch.setattr(admin_store, "DB_PATH", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", facts_db)
    # Default lifecycle persistence is real SQLite, with only its destination redirected.
    for name in ("begin", "finalize", "update_phases", "get"):
        original = getattr(receipt_store, name)
        monkeypatch.setattr(receipt_store, name,
            lambda *args, _original=original, **kwargs: _original(*args, **{**kwargs, "path": receipts}))

    registry = _registry()
    registry["services"]["openrouter"]["config"]["price"]["output_usd_per_million_tokens"] = "2.00"
    registry["services"]["local"] = {"name": "Original local engine", "config": {}}
    target_id = "local" if route == "local" else "openrouter"
    fact = copy.deepcopy(load_provider_catalog()["openai"].retention_facts[0])
    fact["holds"] = "exact evidence before outbound I/O"
    registry["services"][target_id]["config"]["retention_facts"] = [fact]
    original_name = registry["services"][target_id].get("name", target_id)
    expected_revision = retention_facts.revision(fact)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(service_registry, "load_service_registry", lambda **_: registry)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-provider-key")
    monkeypatch.setattr(proxy, "_dispatch_admission_response", lambda *_a, **_k: None)
    monkeypatch.setattr(proxy, "_current_loaded_model_policy_decision", AsyncMock(return_value=None))
    # Supply the resolved local target; the real router must use its logical service,
    # rather than the substrate's service ID, when selecting retention evidence.
    monkeypatch.setattr(proxy, "_resolve_request_target", lambda *_a, **kw:
        None if kw.get("aliases_only") else SimpleNamespace(logical_service_id="local"))
    model = "local-chat" if route == "local" else "openrouter/openai/gpt-4.1-mini"
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}
    outer = RouteReceipt(protocol="openai.responses", intent="chat.responses", path="/v1/responses")
    identity = identity_of(_profile(), name="paid-app", app_id="test-app", accountable_person_id="test-person")
    auth = proxy.AuthContext(identity=identity, retention_receipt=outer)
    outbound_calls = []

    async def send(execution, **_kwargs):
        outbound_calls.append(execution)
        # This assertion fails if either the local capture or the paid callback
        # is removed, even though a child lifecycle can still save its own facts.
        snapshot = getattr(outer, "_retention_fact_snapshot", None)
        assert snapshot is not None
        destination = next(row for row in snapshot[1]["destinations"] if row["id"] == target_id)
        assert destination["facts"][0]["revision"] == expected_revision
        assert destination["facts"][0]["holds"] == fact["holds"]
        registry["services"][target_id]["name"] = "Edited while request runs"
        registry["services"][target_id]["config"]["retention_facts"] = []
        return _Client(), _Response(headers={"content-type": "application/json", "x-litellm-response-cost": "0.000001"},
            body=b'{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}')

    monkeypatch.setattr(proxy, "send_llm_execution_request", send)
    monkeypatch.setattr(provider_execution, "send_llm_execution_request", send)
    runtime = LiteLlmRuntimeConfig(enabled=True, base_url="http://substrate.test", api_key="test-key", service_id="litellm")

    async def run():
        async with ReceiptLifecycleOwner(outer) as owner:
            response = await proxy._forward_litellm_proxy_request(
                _request(body), request_path="/v1/chat/completions", auth_context=auth,
                body_json=body, runtime_config=runtime, parent_request_id=outer.request_id)
            assert response.status_code == 200, response.body
            owner.report(status="succeeded", reason="completed")
    asyncio.run(run())

    assert len(outbound_calls) == 1
    stored = receipt_store.get(outer.request_id, audience="internal")
    assert stored["status"] == "succeeded"
    assert expected_revision in stored["retention_facts"]
    saved = media_jobs.fact_revisions_for_source("receipt", outer.request_id, path=facts_db)
    assert saved[target_id][0]["holds"] == fact["holds"]
    assert saved[target_id][0]["revision"] == expected_revision
    destinations = media_jobs.fact_destinations_for_source("receipt", outer.request_id, path=facts_db)
    assert destinations[target_id]["name"] == original_name
    assert registry["services"][target_id]["config"]["retention_facts"] == []
