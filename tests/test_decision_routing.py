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


def test_configured_fallback_discloses_skipped_primary_in_http_and_receipt(monkeypatch):
    import proxy
    cfg = config()
    cfg["backends"]["large"]["enabled"] = False
    monkeypatch.setattr(proxy, "config", cfg)
    monkeypatch.setattr(proxy, "authenticate_request", lambda *a: proxy.AuthContext())
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "authorize", lambda *a: True)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "ensure_ready", AsyncMock(return_value=None))
    records = []
    monkeypatch.setattr(proxy.receipt_store, "record", records.append)
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "small-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 9, "output_tokens": 0}})))):
        response = TestClient(proxy.app).post("/v1/systemone", json={**BODY, "model": "decide-quality"})
    assert response.status_code == 200, response.text
    assert response.headers["x-azizam-fallback"] == "large:not_deployed"
    receipt = records[0].to_dict(audience="client")
    assert receipt["requested_model"] == "decide-quality"
    assert receipt["resolved_model"] == "small-v1"
    assert receipt["fallback_reasons"] == ["large:not_deployed"]


@pytest.mark.parametrize("model", ["decide", "small", "small-v1"])
def test_unavailable_single_target_returns_503_without_substitution(model):
    rt = runtime(ensure=AsyncMock(return_value=False))
    result = asyncio.run(rt.dispatch(None, None, {**BODY, "model": model}, "r"))
    assert result.status_code == 503
    assert result.body["error"]["code"] == "model_not_deployed"
    assert "No fallback is configured" in result.body["error"]["message"]
    assert result.resolved_model is None
    assert "x-azizam-fallback" not in result.headers


def test_missing_single_target_is_503():
    cfg = config()
    cfg["backends"]["small"]["enabled"] = False
    with pytest.raises(DecisionsError) as error:
        runtime(cfg=cfg).resolve("decide")
    assert error.value.status == 503


def test_backend_cannot_substitute_undeclared_decision_model():
    rt = runtime()
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "different-v1", "answers": {"q": {"type": "noul", "noul": .9}}})))):
        with pytest.raises(DecisionsError) as error:
            asyncio.run(rt.dispatch(None, None, BODY, "r"))
    assert error.value.status == 503
    assert error.value.code == "model_not_deployed"


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
        assert receipt.to_dict(audience=audience)["decisions"] == {"question_count": 2, "engine": "cpu", "remote": False, "cost_usd": 0.0}


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
    assert response.headers["x-azizam-engine"] == "cpu"
    assert records[0].app_id == "app-test"
    assert records[0].requested_model == "decide"
    assert records[0].resolved_model == "small-v1"
    assert records[0].metadata["decisions"]["question_count"] == 1


def test_existing_only_plan_never_starts_service():
    from types import SimpleNamespace
    ensure = AsyncMock(return_value=None)
    rt = runtime(ensure=ensure)
    real = httpx.AsyncClient
    profile = SimpleNamespace(target_policy=SimpleNamespace(activation="existing_only"))
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(503, json={})))):
        result = asyncio.run(rt.dispatch(None, profile, BODY, "r"))
    assert result.status_code == 503 and result.headers["retry-after"] == "5"
    assert result.fallback_reasons == ("small:overloaded",)
    ensure.assert_not_called()


def test_available_only_models_hide_cold_engines():
    from types import SimpleNamespace
    rt = runtime()
    rt.observed = {key: (__import__('time').monotonic(), None) for key in config()["backends"]}
    profile = SimpleNamespace(target_policy=SimpleNamespace(visibility="available"))
    assert rt.models_for(None, profile) == []


def test_receipt_usage_rejects_untrusted_fields():
    receipt = RouteReceipt(protocol="typesafe.systemone", path="/v1/systemone", intent="decision.systemone")
    receipt.metadata["decisions"] = {"question_count": 1, "engine": "cpu", "remote": False, "cost_usd": 0,
                                     "usage": {"input_tokens": 8, "output_tokens": 0, "key": "untrusted"}}
    assert receipt.to_dict(audience="client")["decisions"]["usage"] == {"input_tokens": 8, "output_tokens": 0}


def test_persisted_receipt_client_reads_cost_and_usage(tmp_path):
    from gateway import receipt_store
    db = tmp_path / "receipts.db"
    receipt_store.init_db(path=db)
    receipt = RouteReceipt(protocol="typesafe.systemone", path="/v1/systemone", intent="decision.systemone", app_id="owner")
    receipt.metadata["decisions"] = {"question_count": 1, "engine": "cpu", "remote": False, "cost_usd": 0,
                                     "usage": {"input_tokens": 9, "output_tokens": 0}}
    receipt.finish("ok")
    receipt_store.record(receipt, path=db)
    stored = receipt_store.get(receipt.request_id, path=db, audience="client")
    assert stored["decisions"]["cost_usd"] == 0
    assert stored["decisions"]["usage"]["input_tokens"] == 9


def test_default_bootstrap_aliases_use_cpu_on_demand():
    from pathlib import Path
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "proxy_config.yml").read_text())
    assert validate_aliases(cfg["model_aliases"]) == []
    for alias in ("decide", "decide-quality", "jev-latest"):
        resolved = resolve_alias(cfg["model_aliases"], "decisions", alias)
        assert resolved.engine_spec["placement"] == "cpu"
        assert resolved.engine_spec["residency"] == "on_demand"
        assert resolved.engine_spec["max_state_tokens"] <= resolved.engine_spec["context_tokens"] == 4096
        assert cfg["backends"][resolved.service]["lifecycle"]["driver"] == "systemd"


def test_exhausted_fallback_retains_attempt_receipt_context():
    rt = runtime()
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(529, json={"error": {"message": "Busy"}})))):
        result = asyncio.run(rt.dispatch(None, None, {**BODY, "model": "decide-quality"}, "r"))
    assert result.status_code == 529
    assert result.fallback_reasons == ("large:overloaded", "small:overloaded")
    assert result.headers["x-azizam-fallback"] == "large:overloaded,small:overloaded"
    assert result.backend == "small"
    assert result.engine is None and result.resolved_model is None


def test_cold_catalog_never_connects_to_configured_engines():
    rt = DecisionRuntime(lambda: config(), lambda *args: True, AsyncMock())
    with patch("gateway.decision_routing.httpx.Client", side_effect=AssertionError("catalog must not connect")):
        assert rt.models_for(None)
        assert all(model.engine is None for model in rt.models_for(None))
        assert all(cap.engine is None for cap in rt.capabilities())


def test_all_alias_candidates_unavailable_have_retry_hint():
    cfg = config()
    for service in cfg["backends"].values():
        service["enabled"] = False
    rt = runtime(cfg=cfg)
    with pytest.raises(DecisionsError) as error:
        rt.resolve("decide-quality")
    assert error.value.status == 529
    assert error.value.headers["retry-after"] == "5"


@pytest.mark.parametrize("model", ["decide", "small", "small-v1", "jev-latest"])
def test_dispatch_and_discovery_share_tag_restrictions(monkeypatch, model):
    import proxy
    from gateway.policy import AppProfile, CapabilitySelector
    from gateway.runtime_auth import RuntimeIdentity
    cfg = config()
    monkeypatch.setattr(proxy, "config", cfg)
    auth = proxy.AuthContext(RuntimeIdentity(
        "restricted", "hash", profile=AppProfile(
            id="restricted", allow=CapabilitySelector(tags=frozenset({"approved-chat"})),
        ),
    ))
    cap = proxy.DECISION_RUNTIME.capabilities()[0]
    assert proxy._capability_for_profile(cap, auth).policy_available is False
    ensure = AsyncMock(return_value=None)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "ensure_ready", ensure)
    assert proxy._decision_authorized(auth, model, "small") is False
    assert proxy.DECISION_RUNTIME.models_for(auth, auth.identity.profile) == []
    with pytest.raises(DecisionsError) as error:
        asyncio.run(proxy.DECISION_RUNTIME.dispatch(auth, auth.identity.profile, {**BODY, "model": model}, "r"))
    assert error.value.status == 403
    ensure.assert_not_called()


def test_existing_only_denied_profile_does_not_probe_decision_health(monkeypatch):
    import proxy
    from gateway.policy import AppProfile, CapabilitySelector, TargetPolicy
    from gateway.runtime_auth import RuntimeIdentity

    cfg = config()
    monkeypatch.setattr(proxy, "config", cfg)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed", {})
    auth = proxy.AuthContext(RuntimeIdentity(
        "restricted",
        "hash",
        profile=AppProfile(
            id="restricted",
            allow=CapabilitySelector(tags=frozenset({"approved-chat"})),
            target_policy=TargetPolicy(activation="existing_only"),
        ),
    ))
    def observed(_service, *, fresh=False):
        if fresh:
            raise AssertionError("denied profile must not probe engine health")
        return None

    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed_engine", observed)

    assert proxy._decision_authorized(auth, "decide", "small") is False
    assert proxy.DECISION_RUNTIME.models_for(auth, auth.identity.profile) == []


def test_existing_only_catalog_uses_cached_engine_without_fresh_probe(monkeypatch):
    import proxy
    from gateway.policy import AppProfile, CapabilitySelector, TargetPolicy
    from gateway.runtime_auth import RuntimeIdentity

    cfg = config()
    monkeypatch.setattr(proxy, "config", cfg)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed", {})
    auth = proxy.AuthContext(RuntimeIdentity(
        "cpu",
        "hash",
        profile=AppProfile(
            id="cpu",
            allow=CapabilitySelector(tags=frozenset({"engine:cpu"})),
            target_policy=TargetPolicy(activation="existing_only"),
        ),
    ))

    def observed(_service, *, fresh=False):
        if fresh:
            raise AssertionError("catalog must not perform fresh decision health probes")
        return "cpu"

    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed_engine", observed)

    models = proxy.DECISION_RUNTIME.models_for(auth, auth.identity.profile)

    assert {model.id for model in models} >= {"decide", "small", "small-v1", "jev-latest"}
    assert all(model.engine == "cpu" for model in models)


def test_existing_only_dispatch_rechecks_policy_after_fresh_engine_tags(monkeypatch):
    import time
    import proxy
    from gateway.policy import AppProfile, CapabilitySelector, TargetPolicy
    from gateway.runtime_auth import RuntimeIdentity

    cfg = config()
    monkeypatch.setattr(proxy, "config", cfg)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed", {})
    auth = proxy.AuthContext(RuntimeIdentity(
        "gpu",
        "hash",
        profile=AppProfile(
            id="gpu",
            allow=CapabilitySelector(tags=frozenset({"engine:gpu"})),
            target_policy=TargetPolicy(activation="existing_only"),
        ),
    ))
    def observed(service, *, fresh=False):
        if fresh:
            proxy.DECISION_RUNTIME.observed[service] = (time.monotonic(), "gpu")
            return "gpu"
        cached = proxy.DECISION_RUNTIME.observed.get(service)
        return cached[1] if cached else None

    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed_engine", observed)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "ensure_ready", AsyncMock(return_value=AssertionError("must not activate")))
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "small-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 1, "output_tokens": 1}}, headers={"x-decision-engine": "gpu"})))):
        result = asyncio.run(proxy.DECISION_RUNTIME.dispatch(auth, auth.identity.profile, BODY, "r"))

    assert result.status_code == 200
    assert result.engine == "gpu"


def test_dispatch_propagates_remaining_deadline_to_engine():
    rt = runtime()
    real = httpx.AsyncClient
    captured = []
    def handler(request):
        captured.append(float(request.headers.get("x-decision-deadline-ms", "0")))
        return httpx.Response(200, json={"model": "small-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 1, "output_tokens": 1}})
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler))):
        result = asyncio.run(rt.dispatch(None, None, BODY, "r"))
    assert result.status_code == 200
    assert 0 < captured[0] <= 15000


def test_dispatch_deadline_bounds_total_response_time(monkeypatch):
    import gateway.decision_routing as routing
    monkeypatch.setattr(routing, "ROUTING_TIMEOUT_SECONDS", .03, raising=False)
    rt = runtime()
    real = httpx.AsyncClient
    cancelled = []
    async def handler(request):
        try:
            await asyncio.sleep(.12)
            return httpx.Response(200, json={"model": "small-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 1, "output_tokens": 1}})
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler))):
        result = asyncio.run(rt.dispatch(None, None, BODY, "r"))
    assert result.status_code == 503
    assert cancelled == [True]


def test_dispatch_deadline_includes_lifecycle_health_timeout(monkeypatch):
    import gateway.decision_routing as routing

    monkeypatch.setattr(routing, "ROUTING_TIMEOUT_SECONDS", .01, raising=False)
    cfg = config()
    cfg["backends"]["small"]["lifecycle"] = {
        "driver": "systemd",
        "driver_config": {"health_timeout_s": .2},
    }
    started = []

    async def ensure(_service):
        started.append(True)
        await asyncio.sleep(.04)
        return None

    rt = runtime(cfg=cfg, ensure=ensure)
    real = httpx.AsyncClient

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": "small-v1",
                "answers": {"q": {"type": "noul", "noul": .9}},
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        )

    with patch(
        "gateway.decision_routing.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler)),
    ):
        result = asyncio.run(rt.dispatch(None, None, BODY, "r"))

    assert result.status_code == 200
    assert started == [True]


@pytest.mark.parametrize("app_key", [False, True])
def test_configured_timeout_allows_on_demand_start_for_app_and_lan(monkeypatch, app_key):
    import gateway.decision_routing as routing
    import gateway.lifecycle_drivers as lifecycle
    import proxy
    from gateway.policy import AppProfile
    from gateway.runtime_auth import RuntimeIdentity

    # Scale seconds down: startup exceeds the default routing deadline, but
    # fits the operator's explicit service timeout and lifecycle health budget.
    monkeypatch.setattr(routing, "ROUTING_TIMEOUT_SECONDS", .03)
    cfg = config()
    cfg["backends"]["large"].update(timeout=.3, lifecycle={
        "driver": "systemd", "driver_config": {"health_timeout_s": .2},
    })
    monkeypatch.setattr(proxy, "config", cfg)
    monkeypatch.setattr(proxy, "HEALTH_CHECK_TIMEOUT", .01)
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "observed", {})
    monkeypatch.setattr(lifecycle, "_poll_health", AsyncMock(return_value=False))
    starts = []

    class Driver:
        async def ensure_ready(self, service, timeout_s):
            starts.append(timeout_s)
            await asyncio.sleep(.06)
            return True

    monkeypatch.setattr(lifecycle, "driver_for_service", lambda *args: Driver())
    profile = AppProfile(id="local")
    auth = proxy.AuthContext(RuntimeIdentity("test-app", "hash", profile=profile)) if app_key else proxy.AuthContext()
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "large-v1", "answers": {"q": {"type": "noul", "noul": .9}}, "usage": {"input_tokens": 1, "output_tokens": 0}})))):
        result = asyncio.run(proxy.DECISION_RUNTIME.dispatch(auth, profile, {**BODY, "model": "decide-quality"}, "r"))
    assert result.status_code == 200
    assert starts == [.2]


@pytest.mark.parametrize("timeout", [.03, None, "invalid", float("inf"), float("nan"), 0, -1])
def test_configured_decision_timeout_still_bounds_startup(monkeypatch, timeout):
    import gateway.decision_routing as routing
    monkeypatch.setattr(routing, "ROUTING_TIMEOUT_SECONDS", .03)
    cfg = config()
    cfg["backends"]["large"]["timeout"] = timeout
    cancelled = []

    async def ensure(service):
        try:
            await asyncio.sleep(.12)
        except asyncio.CancelledError:
            cancelled.append(service)
            raise

    rt = runtime(cfg=cfg, ensure=ensure)
    result = asyncio.run(rt.dispatch(None, None, {**BODY, "model": "decide-quality"}, "r"))
    assert result.status_code == 529
    assert result.fallback_reasons == ("large:deadline_exceeded",)
    assert cancelled == ["large"]


def test_explicit_timeout_bounds_lifecycle_and_fallback_budget(monkeypatch):
    import gateway.decision_routing as routing
    monkeypatch.setattr(routing, "ROUTING_TIMEOUT_SECONDS", .3)
    cfg = config()
    cfg["backends"]["large"].update(timeout=.03, lifecycle={
        "driver": "systemd", "driver_config": {"health_timeout_s": .2},
    })
    cfg["backends"]["small"]["timeout"] = .4
    async def start(service):
        await asyncio.sleep(.06)

    ensure = AsyncMock(side_effect=start)
    rt = runtime(cfg=cfg, ensure=ensure)
    result = asyncio.run(rt.dispatch(None, None, {**BODY, "model": "decide-quality"}, "r"))
    assert result.status_code == 529
    assert result.fallback_reasons == ("large:deadline_exceeded",)
    ensure.assert_awaited_once_with("large")


def test_engine_deadline_response_is_retryable_and_preserves_reason():
    rt = runtime()
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(504, json={"error": {"code": "deadline_exceeded"}})))):
        result = asyncio.run(rt.dispatch(None, None, BODY, "r"))
    assert result.status_code == 503
    assert result.fallback_reasons == ("small:deadline_exceeded",)


def test_allowed_local_tag_agrees_for_discovery_and_direct_dispatch(monkeypatch):
    import proxy
    from gateway.policy import AppProfile, CapabilitySelector
    from gateway.runtime_auth import RuntimeIdentity
    monkeypatch.setattr(proxy, "config", config())
    auth = proxy.AuthContext(RuntimeIdentity("local", "hash", profile=AppProfile(
        id="local", allow=CapabilitySelector(tags=frozenset({"local"})),
    )))
    for name in ("decide", "small", "small-v1", "jev-latest"):
        cap = proxy.DECISION_RUNTIME.capability_for(name, "small")
        assert proxy._capability_for_profile(cap, auth).policy_available is True
        assert proxy._decision_authorized(auth, name, "small") is True


def test_fallback_authorization_uses_fallback_engine_metadata(monkeypatch):
    import proxy
    from gateway.policy import AppProfile, CapabilitySelector
    from gateway.runtime_auth import RuntimeIdentity
    import time
    monkeypatch.setattr(proxy, "config", config())
    rt = proxy.DECISION_RUNTIME
    monkeypatch.setattr(rt, "observed", {"large": (time.monotonic(), "gpu"), "small": (time.monotonic(), "cpu")})
    ensure = AsyncMock(return_value=None)
    monkeypatch.setattr(rt, "ensure_ready", ensure)
    auth = proxy.AuthContext(RuntimeIdentity("gpu", "hash", profile=AppProfile(
        id="gpu", allow=CapabilitySelector(tags=frozenset({"engine:gpu"})),
    )))
    real = httpx.AsyncClient
    with patch("gateway.decision_routing.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(529, json={"error": {"code": "overloaded"}})))):
        with pytest.raises(DecisionsError) as error:
            asyncio.run(rt.dispatch(auth, auth.identity.profile, {**BODY, "model": "decide-quality"}, "r"))
    assert error.value.status == 403
    ensure.assert_awaited_once_with("large")
