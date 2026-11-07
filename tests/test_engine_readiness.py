"""An engine the Add engine check verified is ready on the request path too (first-run blocker).

The check reads the definition's readiness route (its primary probe, ``registry/engine_definitions.json``). A new record
stores that path as ``health_endpoint``. An older record with none falls back to it when the default ``/health`` is absent.
A server that does not serve ``/health`` therefore answers chat after it was added, and a server that is really down, locked
or slow still fails with a message that names the route it did not answer.
"""

from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gateway import engine_egress, engine_readiness, lifecycle_drivers
from gateway.engine_admin import build_raw, clean_values
from gateway.engine_definitions import EngineDefinitionError, load_engine_catalog, parse_engine_catalog
from gateway.service_registry import runtime_backend_from_service, service_from_backend_config
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture

HOST = "readiness.fk.test"
KEY = "sk-readiness-fixture"
CATALOG = load_engine_catalog()
OPENAI = CATALOG.definition("openai-compatible")
NATIVE = CATALOG.definition("own-model-list")
LIST_CHECK = {"outcome": "ok", "readiness": {"method": "GET", "path": "/v1/models", "status": 200, "ok": True}}


def _record_config(address: str, *, definition=OPENAI, **values) -> dict:
    clean = clean_values(definition, {"address": address, **values})
    return build_raw(CATALOG, definition, clean, name="Garage GPU", capabilities=["chat"],
                     models=[{"id": "demo-chat", "public_name": "garage-chat"}], enabled=True,
                     check={}, tests=None, secret_id=None)


# -- the readiness route is data ------------------------------------------------------------------------------------

def test_every_definition_declares_a_get_readiness_route_and_a_post_one_is_refused():
    for row in CATALOG.data["definitions"]:
        assert row["probes"]["primary"]["method"] == "GET"
    broken = copy.deepcopy(CATALOG.data)
    broken["definitions"][0]["probes"]["primary"]["method"] = "POST"
    schema = json.loads(open("registry/engine_definitions.schema.json", encoding="utf-8").read())
    with pytest.raises(EngineDefinitionError, match="readiness read and must be GET"):
        parse_engine_catalog(broken, schema=schema)


@pytest.mark.parametrize("address, prefix, expected", [
    ("http://h.test:8080/v1", "", "/v1/models"),
    ("http://h.test:8080", "", "/models"),
    ("http://h.test:8080/v1", "/openai", "/v1/openai/models"),
])
def test_the_readiness_path_sits_under_the_saved_base_for_an_address_probe(address, prefix, expected):
    config = _record_config(address, **({"path_prefix": prefix} if prefix else {}))
    assert engine_readiness.record_readiness_path(config) == expected


def test_a_probe_written_from_the_origin_ignores_the_base_path():
    config = _record_config("http://h.test:11434", definition=NATIVE)
    assert engine_readiness.record_readiness_path(config) == NATIVE["probes"]["primary"]["path"]


def test_a_record_with_no_known_definition_has_no_declared_readiness():
    assert engine_readiness.record_readiness_path({"base_path": "/v1"}) is None
    assert engine_readiness.record_readiness_path({"definition": "not-a-definition"}) is None


def test_a_new_record_stores_only_a_readiness_path_the_check_read_and_got_200_for():
    clean = clean_values(OPENAI, {"address": "http://h.test:8080/v1"})
    args = dict(name="a", capabilities=["chat"], models=[], enabled=True, tests=None, secret_id=None)
    assert build_raw(CATALOG, OPENAI, clean, check=LIST_CHECK, **args)["health_endpoint"] == "/v1/models"
    refused = {"readiness": {"method": "GET", "path": "/v1/models", "status": 404, "ok": False}}
    assert "health_endpoint" not in build_raw(CATALOG, OPENAI, clean, check=refused, **args)
    assert "health_endpoint" not in build_raw(CATALOG, OPENAI, clean, check={}, **args)



@pytest.fixture(autouse=True)
def _fresh_fallback_memory():
    lifecycle_drivers._READINESS_FALLBACK.clear()
    yield
    lifecycle_drivers._READINESS_FALLBACK.clear()


# -- the driver -----------------------------------------------------------------------------------------------------

def _runtime(address: str, *, stored: bool, **values) -> dict:
    """The flat backend the lifecycle driver receives, for a record the Add engine routes would write."""
    clean = clean_values(OPENAI, {"address": address, **values})
    check = LIST_CHECK if stored else {}
    raw = build_raw(CATALOG, OPENAI, clean, name="Garage GPU", capabilities=["chat"], models=[], enabled=True,
                    check=check, tests=None, secret_id=None)
    return runtime_backend_from_service(service_from_backend_config("garage", raw, source="ui"))


def _server(fakes, *, health, models):
    """A stand-in engine; ``health`` and ``models`` are the status each route answers (None: the route is not served)."""
    exchanges = []
    for exchange_id, path, status in (("health", "/health", health), ("models", "/v1/models", models), ("openapi", "/openapi.json", 404)):
        if status is not None:
            body = {"object": "list", "data": [{"id": "m"}]} if status == 200 else {"error": {"message": "no"}}
            exchanges.append({"id": exchange_id, "match": {"method": "GET", "path": path}, "response": {"status": status, "json": body},
                              "repeat": True, "required": False})
    scenario = scenario_from_data({"protocol": "engine-probe", "name": "readiness", "exchanges": exchanges,
                                   "provenance": {"source": "hand-written", "reference": "first-run report: readiness read /health, which many servers do not serve"}})
    return fakes.asgi(scenario, host=HOST)


def _ready(svc) -> bool:
    return asyncio.run(lifecycle_drivers.ExternalDriver().ensure_ready(svc, timeout_s=0.0))


def _paths(fake, exchange_id):
    return [request.path for request in fake.calls(exchange_id)]


def test_an_older_record_falls_back_once_to_the_definitions_route_when_health_answers_404(fake_upstreams):
    fake = _server(fake_upstreams, health=404, models=200)
    svc = _runtime(f"http://{HOST}/v1", stored=False)
    assert "health_endpoint" not in svc
    assert _ready(svc) is True
    assert _paths(fake, "health") == ["/health"] and _paths(fake, "models") == ["/v1/models"]
    # Remembered for the process: the next check goes straight to the route that answered.
    assert _ready(svc) is True
    assert len(fake.calls("health")) == 1 and len(fake.calls("models")) == 2


def test_a_record_with_a_stored_endpoint_never_reads_health(fake_upstreams):
    fake = _server(fake_upstreams, health=404, models=200)
    assert _ready(_runtime(f"http://{HOST}/v1", stored=True)) is True
    assert not fake.calls("health") and len(fake.calls("models")) == 1


def test_health_answering_200_still_makes_an_older_record_ready_without_the_fallback(fake_upstreams):
    fake = _server(fake_upstreams, health=200, models=500)
    assert _ready(_runtime(f"http://{HOST}/v1", stored=False)) is True
    assert not fake.calls("models")


@pytest.mark.parametrize("status", [500, 503, 401, 403])
def test_a_real_unhealthy_or_locked_answer_on_health_does_not_fall_back(fake_upstreams, status):
    fake = _server(fake_upstreams, health=status, models=200)
    assert _ready(_runtime(f"http://{HOST}/v1", stored=False)) is False
    assert not fake.calls("models"), "only a 404 (route absent) may fall back"
    assert lifecycle_drivers._READINESS_FALLBACK == {}


def test_a_timeout_on_health_does_not_fall_back(fake_upstreams):
    fake = _server(fake_upstreams, health=200, models=200)
    svc = _runtime(f"http://{HOST}/v1", stored=False)
    with patch("gateway.lifecycle_drivers.engine_egress.async_client") as client:
        session = AsyncMock()
        session.get = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        client.return_value.__aenter__ = AsyncMock(return_value=session)
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        assert _ready(svc) is False
    assert not fake.calls("models") and lifecycle_drivers._READINESS_FALLBACK == {}


def test_a_fallback_route_that_is_down_or_absent_too_fails(fake_upstreams):
    _server(fake_upstreams, health=404, models=503)
    assert _ready(_runtime(f"http://{HOST}/v1", stored=False)) is False


def test_an_explicit_health_endpoint_wins_and_never_falls_back(fake_upstreams):
    fake = _server(fake_upstreams, health=200, models=200)
    svc = {**_runtime(f"http://{HOST}/v1", stored=True), "health_endpoint": "/openapi.json"}  # the engine answers 404 here
    assert _ready(svc) is False
    assert _paths(fake, "openapi") == ["/openapi.json"] and not fake.calls("health") and not fake.calls("models")


def test_a_process_ready_observation_wins_and_reads_no_route(fake_upstreams):
    fake = _server(fake_upstreams, health=404, models=200)
    svc = {**_runtime(f"http://{HOST}/v1", stored=False), "observations": {"process_ready": {"http": {"path": "/health"}}}}
    with patch("gateway.engine_observations.ObservationReader.read", new=AsyncMock(return_value=type("R", (), {"known": True, "value": False})())):
        assert _ready(svc) is False
    assert not fake.requests
    # No readiness GET ran, so the failure text must not name one.
    text = lifecycle_drivers.readiness_failure_text(svc)
    assert "GET" not in text and "/health" not in text and "process_ready" in text


@pytest.mark.parametrize("address, expected", [
    ("http://readiness.fk.test:8080/v1", "http://readiness.fk.test:8080/v1/models"),
    ("http://readiness.fk.test:8080", "http://readiness.fk.test:8080/models"),
    ("http://readiness.fk.test:8080/provider/v1", "http://readiness.fk.test:8080/provider/v1/models"),
])
def test_the_fallback_reads_the_route_the_check_read_under_the_base_prefix(address, expected):
    svc = {**_runtime(address, stored=False), "probe_with_inference_connection": True}  # what saving the connection sets
    assert lifecycle_drivers._fallback_request(svc, engine_readiness.record_readiness_path(svc))[0] == expected
    stored = {**_runtime(address, stored=True, **{}), "probe_with_inference_connection": True}
    # The stored endpoint is the same URL when the check read the route under this address.
    clean_path = engine_readiness.record_readiness_path(stored)
    assert lifecycle_drivers._health_request({**stored, "health_endpoint": clean_path})[0] == expected


def test_an_extra_path_prefix_is_kept_and_not_doubled_when_the_stored_endpoint_names_it():
    svc = {**_runtime("http://readiness.fk.test:8080/v1", stored=False, path_prefix="/openai"), "probe_with_inference_connection": True}
    path = engine_readiness.record_readiness_path(svc)
    assert path == "/v1/openai/models"
    assert lifecycle_drivers._fallback_request(svc, path)[0] == "http://readiness.fk.test:8080/v1/openai/models"
    assert lifecycle_drivers._health_request({**svc, "health_endpoint": path})[0] == "http://readiness.fk.test:8080/v1/openai/models"


def test_the_key_goes_only_to_the_origin_it_was_stored_for(monkeypatch):
    monkeypatch.setenv("READINESS_FIXTURE_KEY", KEY)
    from gateway.service_registry import origin_of

    svc = _runtime(f"http://{HOST}/v1", stored=False, api_key_env="READINESS_FIXTURE_KEY")
    svc["probe_with_inference_connection"] = True  # what saving the connection through the engine page sets
    svc["credential_origin"] = origin_of(f"http://{HOST}/v1")
    path = engine_readiness.record_readiness_path(svc)
    assert lifecycle_drivers._fallback_request(svc, path)[1].get("authorization") == f"Bearer {KEY}"
    svc["credential_origin"] = "http://elsewhere.fk.test:80"
    url, headers = lifecycle_drivers._fallback_request(svc, path)
    assert url.startswith(f"http://{HOST}") and "authorization" not in headers


# -- the egress rules -------------------------------------------------------------------------------------------------

def test_the_definitions_readiness_route_is_a_declared_probe_path_and_nothing_else_is():
    svc = _runtime(f"http://{HOST}/v1", stored=False)
    record = {"config": {key: value for key, value in svc.items()}}
    declared = engine_egress.declared_probe_paths(record)
    assert "/v1/models" in declared and "/health" in declared
    assert "/v1/chat/completions" not in declared and "/v1/unload" not in declared
    assert "/v1/models" in engine_egress.declared_probe_paths({"config": {**record["config"], "health_endpoint": "/v1/models"}})
    assert engine_egress.declared_probe_paths({"config": {"definition": "nothing", "base_path": "/v1"}}) == {"/health"}


# -- what a client is told ----------------------------------------------------------------------------------------------

def test_the_failure_text_for_an_engine_the_gateway_does_not_start_names_the_route_not_the_address():
    svc = _runtime(f"http://{HOST}:8080/v1", stored=True, api_key_env="READINESS_FIXTURE_KEY")
    text = lifecycle_drivers.readiness_failure_text(svc)
    assert text == "the engine did not answer its readiness check (GET /v1/models)"
    assert HOST not in text and "8080" not in text


def test_the_failure_text_follows_the_fallback_route_once_it_is_known():
    svc = _runtime(f"http://{HOST}:8080/v1", stored=False)
    assert lifecycle_drivers.readiness_failure_text(svc).endswith("(GET /health)")
    lifecycle_drivers._READINESS_FALLBACK[lifecycle_drivers._readiness_fallback_key(svc)] = "/v1/models"
    assert lifecycle_drivers.readiness_failure_text(svc).endswith("(GET /v1/models)")


@pytest.mark.parametrize("driver", ["command", "docker", "systemd", "process", "http-load-unload"])
def test_an_engine_the_gateway_starts_keeps_its_could_not_start_text(driver):
    svc = {**_runtime(f"http://{HOST}/v1", stored=True), "lifecycle": {"driver": driver}, "process": {"driver": driver}}
    assert lifecycle_drivers.readiness_failure_text(svc) is None


# -- review fixes (second pass) -----------------------------------------------------------------------------------------

class _GatedClient:
    """A stand-in HTTP client: /health answers 404 only after ``waiting`` polls have all asked for it; /v1/models answers 200."""

    def __init__(self, waiting: int):
        self.waiting, self.arrived, self.released, self.calls = waiting, 0, asyncio.Event(), []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        path = httpx.URL(url).path
        self.calls.append(path)
        if path == "/health":
            self.arrived += 1
            if self.arrived >= self.waiting:
                self.released.set()
            await self.released.wait()
            return httpx.Response(404)
        return httpx.Response(200)


def test_concurrent_first_polls_agree_on_the_fallback():
    svc = _runtime(f"http://{HOST}:8080/v1", stored=False)
    gate = _GatedClient(2)

    async def both():
        driver = lifecycle_drivers.ExternalDriver()
        return await asyncio.gather(driver.ensure_ready(svc, timeout_s=0.0), driver.ensure_ready(svc, timeout_s=0.0))

    with patch("gateway.lifecycle_drivers.engine_egress.async_client", gate), patch("gateway.lifecycle_drivers.engine_egress.probe", return_value={}):
        assert asyncio.run(both()) == [True, True]
    assert gate.calls.count("/health") == 2 and gate.calls.count("/v1/models") == 2


def test_two_records_at_one_address_with_different_connections_do_not_share_a_memo():
    one = _runtime(f"http://{HOST}:8080/v1", stored=False, api_key_env="KEY_ONE")
    two = {**one, "headers": [{"name": "x-tenant", "value": "b"}]}
    third = {**one, "probe_with_inference_connection": True}
    keys = {lifecycle_drivers._readiness_fallback_key(item) for item in (one, two, third)}
    assert len(keys) == 3
    assert lifecycle_drivers._readiness_fallback_key(dict(one)) == lifecycle_drivers._readiness_fallback_key(one)
    assert KEY not in repr(lifecycle_drivers._readiness_fallback_key({**one, "api_key": KEY}))
    # B's own /health is read even though A already remembered its fallback.
    lifecycle_drivers._READINESS_FALLBACK[lifecycle_drivers._readiness_fallback_key(one)] = "/v1/models"
    assert lifecycle_drivers._readiness_fallback_key(two) not in lifecycle_drivers._READINESS_FALLBACK


def test_editing_a_connection_drops_the_remembered_fallback(fake_upstreams):
    fake = _server(fake_upstreams, health=404, models=200)
    svc = _runtime(f"http://{HOST}/v1", stored=False)
    assert _ready(svc) is True and len(fake.calls("health")) == 1
    edited = {**svc, "headers": [{"name": "x-tenant", "value": "c"}]}
    assert _ready(edited) is True
    assert len(fake.calls("health")) == 2, "an edited record is read afresh, not served from the old memo"


@pytest.mark.parametrize("address, expected", [
    ("http://readiness.fk.test:8080/provider/v1", "http://readiness.fk.test:8080/provider/v1/models"),
    ("http://readiness.fk.test:8080/proxy", "http://readiness.fk.test:8080/proxy/models"),
])
@pytest.mark.parametrize("explicit_base", ["base_url", "api_base"])
def test_a_legacy_explicit_base_record_is_not_given_its_base_path_twice(address, expected, explicit_base):
    svc = {**_runtime(address, stored=False), explicit_base: address, "probe_with_inference_connection": True}
    path = engine_readiness.record_readiness_path(svc)
    assert lifecycle_drivers._fallback_request(svc, path)[0] == expected
    assert lifecycle_drivers._health_request({**svc, "health_endpoint": path})[0] == expected


def test_an_owner_set_health_endpoint_keeps_its_relative_meaning_under_an_explicit_base():
    address = "http://readiness.fk.test:8080/provider/v1"
    svc = {**_runtime(address, stored=False), "base_url": address, "probe_with_inference_connection": True, "health_endpoint": "/healthz"}
    assert lifecycle_drivers._health_request(svc)[0] == f"{address}/healthz"


def _egress_record():
    return {"enabled": True, "residency": {"transition": {"enabled": True, "activation": "pass_through"}},
            "config": {"definition": "openai-compatible", "base_path": "/v1", "host": "127.0.0.1", "port": 59151,
                       "base_url": "http://127.0.0.1:59151/v1"}}


def test_the_readiness_route_is_allowed_exactly_once_and_not_under_the_base_prefix():
    projection = engine_egress.project({"engine": _egress_record()})
    allowed = projection.probe_paths["127.0.0.1:59151"]
    assert "/v1/models" in allowed and "/health" in allowed and "/v1/health" in allowed
    assert "/v1/v1/models" not in allowed


def test_a_marked_get_of_the_doubled_readiness_route_is_refused():
    from gateway.transition_gate import TransitionUnavailable

    previous = engine_egress._live
    engine_egress.install(engine_egress.project({"engine": _egress_record()}))
    try:
        def send(path):
            engine_egress.check_request(httpx.Request("GET", f"http://127.0.0.1:59151{path}", extensions=engine_egress.probe()))
        send("/v1/models")  # declared: allowed without a lease
        with pytest.raises(TransitionUnavailable):
            send("/v1/v1/models")
    finally:
        engine_egress.install(previous)
