"""Readiness and loaded-model probes use the same saved connection as the final request.

Regression hypothesis (review 2): the final hop reads the saved record
(scheme, address, path prefix, headers, origin-bound key), but the readiness
poll and the loaded-model probes built ``http://host:port`` + endpoint from the
boot projection. A record behind an https prefix, or one that needs a header
or a key, then failed readiness (503 before the final hop) or read no loaded
model, while the final request itself would have worked. A record without
those settings must still probe the same URL, with no headers, as before.
"""

from __future__ import annotations

import asyncio

import pytest

import proxy
from gateway.lifecycle_drivers import ExternalDriver, SystemdDriver, engine_read_request
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture

HOST = "gpu.fk.test:8443"
ORIGIN = f"https://{HOST}"
KEY = "k-inference"
PROVENANCE = {"source": "hand-written", "reference": "ENGINE_CONTRACT section 2: probes use the record connection"}
MODELS = {"data": [{"id": "served-id", "context_length": 32768}]}


def _saved(origin: str = ORIGIN) -> dict:
    scheme, _, rest = origin.partition("://")
    host, _, port = rest.partition(":")
    return {"scheme": scheme, "host": host, "port": int(port), "base_path": "/provider/v1",
            "headers": [{"name": "X-Tenant", "value": "family"}],
            "api_key_env": "PROBE_TEST_KEY", "credential_origin": origin}


def _engine(path: str, json: dict, *, times: int = 1, keyed: bool = True) -> object:
    headers = {"x-tenant": "family", "authorization": f"Bearer {KEY}" if keyed else {"$absent": True}}
    return scenario_from_data({
        "protocol": "openai-compatible", "name": f"probe{path.replace('/', '-')}", "provenance": PROVENANCE,
        "exchanges": [{"id": "probe", "times": times, "match": {"method": "GET", "path": path, "headers": headers},
                       "response": {"status": 200 if keyed else 401, "json": json}}],
    })


@pytest.fixture(autouse=True)
def inference_key(monkeypatch):
    monkeypatch.setenv("PROBE_TEST_KEY", KEY)


@pytest.mark.parametrize("endpoint,url", [
    ("/v1/models", "http://127.0.0.1:5051/v1/models"),
    ("/props", "http://127.0.0.1:5051/props"),
    ("/health", "http://127.0.0.1:5051/health"),
])
def test_a_plain_service_probes_the_legacy_url_with_no_headers(endpoint, url):
    assert engine_read_request({"host": "127.0.0.1", "port": 5051}, endpoint) == (url, {})


@pytest.mark.parametrize("endpoint,url", [
    ("/v1/models", f"{ORIGIN}/provider/v1/models"),
    ("/props", f"{ORIGIN}/provider/props"),
])
def test_a_saved_connection_gives_the_scheme_prefix_headers_and_key(endpoint, url):
    assert engine_read_request(_saved(), endpoint) == (url, {"x-tenant": "family", "authorization": f"Bearer {KEY}"})
    assert engine_read_request({"config": _saved()}, endpoint)[0] == url


def test_an_address_without_a_port_field_is_still_probed():
    assert engine_read_request({"base_url": f"{ORIGIN}/provider/v1"}, "/v1/models")[0] == f"{ORIGIN}/provider/v1/models"
    assert engine_read_request({"base_url": ORIGIN}, "/v1/models")[0] == f"{ORIGIN}/v1/models"


def test_a_control_credential_never_goes_with_a_read(monkeypatch):
    monkeypatch.setenv("PROBE_CONTROL_KEY", "k-control")
    svc = {**_saved(), "connection": {"address": ORIGIN, "credentials": {
        "control": {"secret_ref": "os.environ/PROBE_CONTROL_KEY", "origin": ORIGIN}}}}
    _url, headers = engine_read_request(svc, "/v1/models")
    assert headers == {"x-tenant": "family", "authorization": f"Bearer {KEY}"}


def test_a_missing_key_makes_the_probe_unknown_not_unauthenticated(monkeypatch):
    monkeypatch.delenv("PROBE_TEST_KEY")
    assert engine_read_request(_saved(), "/v1/models") is None


def test_the_ready_check_reaches_the_saved_prefix_with_header_and_key(fake_upstreams):
    fake_upstreams.asgi(_engine("/provider/v1/models", MODELS), host=HOST)
    assert asyncio.run(ExternalDriver().ensure_ready({**_saved(), "health_endpoint": "/v1/models"}, timeout_s=1.0))


def test_legacy_secret_reference_survives_the_actual_readiness_path(monkeypatch, fake_upstreams):
    from gateway import service_registry

    fake_upstreams.asgi(_engine("/provider/v1/models", MODELS, times=2), host=HOST)
    monkeypatch.setenv("AZIZAM_DOWNSTREAM_API_KEY", KEY)
    raw = {**_saved(), "health_endpoint": "/v1/models"}
    raw.pop("api_key_env")
    service = service_registry.service_from_backend_config("legacy-key", raw, source="ui")
    service["secret_refs"] = {"api_key": "<external-secret>"}
    projected = service_registry.runtime_backend_from_service(service)
    monkeypatch.setattr(proxy, "config", {"backends": {"legacy-key": projected}})
    assert asyncio.run(proxy._ensure_target("legacy-key", None, None, None, take_lease=False)) == (True, None)
    monkeypatch.setattr(proxy, "config", {"backends": {}})
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"services": {"legacy-key": service}})
    monkeypatch.setattr(proxy, "_RESOURCE_FACT_CACHE", {})
    assert asyncio.run(proxy.check_service_health("legacy-key"))


def test_the_ready_check_sends_no_key_bound_to_another_origin(fake_upstreams):
    fake_upstreams.asgi(_engine("/provider/v1/models", {}, keyed=False), host=HOST)
    svc = {**_saved(), "health_endpoint": "/v1/models", "credential_origin": "https://other.fk.test:443"}
    assert not asyncio.run(ExternalDriver().ensure_ready(svc, timeout_s=0.0))


def test_the_switch_health_wait_uses_the_saved_connection(fake_upstreams):
    fake_upstreams.asgi(_engine("/provider/health", {"status": "ok"}), host=HOST)
    assert asyncio.run(SystemdDriver()._wait_until_healthy(_saved(), "/health", 1.0)) is not None


def _saved_registry(monkeypatch, backend: str, saved: dict, **fields):
    record = {"config": {**saved, **fields}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"services": {backend: record}})
    monkeypatch.setattr(proxy, "config", {"backends": {backend: {
        "host": "127.0.0.1", "port": 1, "capabilities": ["chat"], **fields}}})
    monkeypatch.setitem(proxy.LLM_BACKEND_HOSTS, backend, "http://127.0.0.1:1")
    monkeypatch.setattr(proxy, "_loaded_state", {})
    monkeypatch.setattr(proxy, "_MODEL_CONTEXT_CACHE", {})


def test_the_switch_readiness_probe_reads_the_loaded_model_through_the_saved_connection(monkeypatch, fake_upstreams):
    # The loaded-state seed is a synchronous read, so this engine listens on a real loopback port.
    engine = fake_upstreams.loopback(_engine("/provider/v1/models", MODELS, times=2))
    _saved_registry(monkeypatch, "probe-llm", _saved(engine.base_url),
                    state_type="openai_compatible", loaded_model_identity="path")
    assert asyncio.run(proxy._currently_loaded_model_for("probe-llm")) == "served-id"
    assert asyncio.run(proxy.current_llm_model_for_backend("probe-llm")) == "served-id"


def test_the_current_model_and_context_window_probes_use_the_saved_connection(monkeypatch, fake_upstreams):
    fake_upstreams.asgi(_engine("/provider/v1/models", MODELS, times=2), host=HOST)
    _saved_registry(monkeypatch, "probe-llm", _saved(), state_type="openai_compatible",
                    context_window_probe={"endpoint": "/v1/models"})
    assert asyncio.run(proxy.current_llm_model_for_backend("probe-llm")) == "served-id"
    assert asyncio.run(proxy._probed_context_window("probe-llm")) == 32768


def test_active_backend_health_uses_the_same_prefixed_keyed_connection(monkeypatch, fake_upstreams):
    fake_upstreams.asgi(_engine("/provider/health", {"status": "ok"}), host=HOST)
    monkeypatch.setattr(proxy, "config", {"backends": {"health-probe": _saved()}})
    monkeypatch.setattr(proxy, "LLM_BACKEND_HOSTS", {"health-probe": "http://stale.invalid:1"})
    monkeypatch.setattr(proxy, "_RESOURCE_FACT_CACHE", {})
    assert asyncio.run(proxy.get_active_backend()) == "health-probe"


def test_a_backend_without_a_saved_record_keeps_the_boot_host(monkeypatch):
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"services": {}})
    monkeypatch.setitem(proxy.LLM_BACKEND_HOSTS, "probe-legacy", "http://127.0.0.1:5099")
    assert proxy._engine_probe("probe-legacy", "/v1/models") == ("http://127.0.0.1:5099/v1/models", {})
    assert proxy._engine_probe("probe-legacy", "/props") == ("http://127.0.0.1:5099/props", {})
