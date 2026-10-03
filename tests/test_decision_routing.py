"""Decision routing tests at the alias, policy, lifecycle and receipt boundaries."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.capabilities import summarize_capability
from gateway.decision_routing import DecisionRuntime
from gateway.decisions_api import DecisionsError
from gateway.receipts import RouteReceipt
from gateway.stable_aliases import resolve_alias, validate_aliases


def config():
    return {
        "backends": {
            "small": {"host": "127.0.0.1", "port": 18091, "enabled": True},
            "large": {"host": "127.0.0.1", "port": 18092, "enabled": True},
        },
        "model_aliases": {"decisions": {
            "engines": {
                "small": {"service": "small", "model": "small-v1", "calibrated": True, "max_options": 255},
                "large": {"service": "large", "model": "large-v1"},
            },
            "stable": {"decide": ["small"], "decide-quality": ["large", "small"]},
            "openai": [{"match": "jev-*", "to": "decide"}, {"match": "*/*", "passthrough": True}],
        }},
    }


BODY = {"model": "decide", "state": "Hello", "questions": {"q": {"type": "noul", "instructions": "Greeting?"}}}


def test_aliases_and_no_implicit_remote():
    cfg = config()["model_aliases"]
    assert validate_aliases(cfg) == []
    assert resolve_alias(cfg, "decisions", "jev-latest").upstream_model == "small-v1"
    assert resolve_alias(cfg, "decisions", "decide-quality").fallbacks == (("small", "small"),)
    assert resolve_alias(cfg, "decisions", "typesafe/jev-latest") is None


def runtime(cfg=None, authorize=None, ensure=None):
    cfg = cfg or config()
    obj = DecisionRuntime(lambda: cfg, authorize or (lambda *args: True), ensure or AsyncMock(return_value=None))
    obj.observed = {key: (__import__('time').monotonic(), "cpu") for key in cfg["backends"]}
    return obj


def test_policy_denied_before_start():
    ensure = AsyncMock(return_value=None)
    rt = runtime(authorize=lambda *args: False, ensure=ensure)
    with pytest.raises(DecisionsError) as error:
        asyncio.run(rt.dispatch(None, None, BODY, "r"))
    assert error.value.status == 403
    ensure.assert_not_called()


def test_free_fallback_observes_engine_and_model():
    rt = runtime()
    real = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if request.url.port == 18092:
            return httpx.Response(529, json={"error": {"message": "Busy"}})
        return httpx.Response(200, json={"model": "small-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 9, "output_tokens": 0}}, headers={"x-decision-engine": "cpu"})
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler))):
        result = asyncio.run(rt.dispatch(None, None, {**BODY, "model": "decide-quality"}, "r"))
    assert [call["model"] for call in calls] == ["large-v1", "small-v1"]
    assert result.engine == "cpu" and result.cost_usd == 0
    assert result.fallback_reasons == ("large:overloaded",)


def test_malformed_backend_rejected():
    rt = runtime()
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "x", "answers": {}})))):
        with pytest.raises(DecisionsError) as error:
            asyncio.run(rt.dispatch(None, None, BODY, "r"))
    assert error.value.status == 502


def test_model_catalog_and_capabilities_follow_policy_actual_engine():
    rt = runtime(authorize=lambda auth, model, backend: backend == "small")
    models = rt.models_for(None)
    assert "decide-quality" not in [m.id for m in models]
    assert {"decide", "jev-latest", "jev-preview"} <= {m.id for m in models}
    cap = summarize_capability(rt.capabilities()[0], audience="client")
    assert cap["engine"] == "cpu" and cap["cost_class"] == "free-local"
    assert cap["limits"]["max_options"] == 255


def test_receipt_metadata_visible_and_scrubbed():
    receipt = RouteReceipt(protocol="typesafe.systemone", path="/v1/systemone", intent="decision.systemone")
    receipt.metadata["decisions"] = {"question_count": 2, "engine": "cpu", "remote": False, "cost_usd": 0, "secret": "never expose"}
    for audience in ("client", "admin"):
        assert receipt.to_summary(audience=audience)["decisions"] == {"question_count": 2, "engine": "cpu", "remote": False, "cost_usd": 0.0}


def test_mounted_gateway_path_and_owned_receipt(monkeypatch):
    import proxy
    from gateway.runtime_auth import RuntimeIdentity
    cfg = config()
    rt = proxy.DECISION_RUNTIME
    monkeypatch.setattr(proxy, "config", cfg)
    monkeypatch.setattr(proxy, "authenticate_request", lambda *a: proxy.AuthContext(RuntimeIdentity("test", "hash", app_id="app-test")))
    monkeypatch.setattr(rt, "authorize", lambda *a: True)
    monkeypatch.setattr(rt, "ensure_ready", AsyncMock(return_value=None))
    records = []
    monkeypatch.setattr(proxy.receipt_store, "record", records.append)
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "small-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 9, "output_tokens": 0}}, headers={"x-decision-engine": "cpu"})))):
        response = TestClient(proxy.app).post("/typesafe/v1/systemone", json=BODY)
    assert response.status_code == 200, response.text
    assert response.headers["x-gateway-engine"] == "cpu"
    assert records[0].app_id == "app-test"
    assert records[0].requested_model == "decide"
    assert records[0].resolved_model == "small-v1"
    assert records[0].metadata["decisions"]["question_count"] == 1
