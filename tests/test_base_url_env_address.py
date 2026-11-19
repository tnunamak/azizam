"""A record that names ``base_url_env`` is dialed at the address that variable holds, by every reader.

In a Docker Compose install the gateway reaches LiteLLM at ``http://litellm:4000`` (``AZIZAM_LITELLM_BASE_URL``),
while the release record's ``default_base_url`` is the bare-metal ``http://127.0.0.1:4011``. Chat read the variable;
the status grid and the model catalog's health probe read only the record, so they dialed the loopback default
inside the gateway container, where nothing listens. Status then said "LiteLLM is not accepting connections" while
chat worked. These tests make the Compose condition: the variable names a live server and nothing is made to
listen at the default address.
"""
from __future__ import annotations

import asyncio

import pytest

import proxy
import status_endpoint
from gateway import service_registry, url_binding
from gateway.backends.llm_execution import litellm_runtime_from_registry
from gateway.engine_egress import destinations_of
from gateway.model_presentation import service_health_endpoint
from tests.support.fake_upstream.loopback import LoopbackServer

ENV_NAME = "AZIZAM_LITELLM_BASE_URL"
DEFAULT = "http://127.0.0.1:4011"


async def _liveliness(scope, _receive, send):
    """What LiteLLM answers on its liveliness path: a bare JSON string, not an object."""
    seen.append(scope["path"])
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'"I\'m alive!"', "more_body": False})


seen: list[str] = []


@pytest.fixture
def litellm_server():
    seen.clear()
    server = LoopbackServer(_liveliness).start()
    try:
        yield server
    finally:
        server.stop()


def _release_litellm() -> dict:
    return service_registry.release_registry()["services"]["litellm"]


def _backend() -> dict:
    return service_registry.runtime_backend_from_service(_release_litellm())


def test_the_release_record_names_a_variable_and_a_loopback_default():
    config = _release_litellm()["config"]
    assert (config["base_url_env"], config["default_base_url"]) == (ENV_NAME, DEFAULT)


def test_the_status_grid_probes_the_address_the_variable_names(litellm_server, monkeypatch):
    monkeypatch.setenv(ENV_NAME, litellm_server.base_url)

    (row,) = asyncio.run(status_endpoint._backend_grid({"backends": {"litellm": _backend()}}))

    # What matters here is the dialed address: the probe reached the server and reports no connection fault. (The
    # bare-string answer is read as "answered, readiness unknown"; request evidence then settles it.)
    assert seen == ["/health/liveliness"]
    assert row["last_error"] is None
    assert row["status"] != "down"


def test_the_model_catalog_probe_dials_the_address_the_variable_names(litellm_server, monkeypatch):
    monkeypatch.setenv(ENV_NAME, litellm_server.base_url)
    monkeypatch.setattr(proxy, "_HEALTH_EVIDENCE_CACHE", {})

    evidence = asyncio.run(proxy._models_catalog_health_evidence(
        {"services": {"litellm": _release_litellm()}}, service_ids=["litellm"]))["litellm"]

    assert seen == ["/health/liveliness"]
    assert evidence["source"] == "health"
    assert evidence["status"] != "unavailable"


@pytest.mark.parametrize("value, expected", [
    ("http://litellm:4000", "http://litellm:4000"),
    ("http://litellm:4000/", "http://litellm:4000"),
    ("  http://litellm:4000/  ", "http://litellm:4000"),
])
def test_the_dialed_address_is_the_variable_when_set(value, expected, monkeypatch):
    monkeypatch.setenv(ENV_NAME, value)
    backend = _backend()

    assert service_registry.engine_base_address(backend) == expected
    assert service_registry.checked_engine_base_address(backend) == expected
    assert service_health_endpoint(backend) == f"{expected}/health/liveliness"


@pytest.mark.parametrize("value", [None, ""])
def test_the_dialed_address_is_the_default_when_the_variable_is_unset_or_empty(value, monkeypatch):
    if value is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, value)

    assert service_registry.engine_base_address(_backend()) == DEFAULT


def test_a_variable_that_is_not_an_http_address_is_refused_not_replaced_by_the_default(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "litellm:4000")

    with pytest.raises(service_registry.EngineAddressError) as refused:
        service_registry.checked_engine_base_address(_backend())

    assert "base_url_env" in str(refused.value) and "litellm:4000" not in str(refused.value)
    assert service_registry.engine_base_address(_backend()) == ""


def test_chat_and_the_probes_read_one_address(monkeypatch):
    registry = {"services": {"litellm": _release_litellm()}}
    for env in ({}, {ENV_NAME: "http://litellm:4000/"}):
        monkeypatch.delenv(ENV_NAME, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        chat = litellm_runtime_from_registry(registry, env=env).base_url
        assert chat == service_registry.engine_base_address(_backend())


def test_an_explicit_environment_is_read_instead_of_the_process_environment(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "http://from-process:1")

    config = _backend()

    assert service_registry.engine_base_address(config, environ={ENV_NAME: "http://from-argument:2"}) == "http://from-argument:2"
    assert service_registry.engine_base_address(config, environ={}) == DEFAULT


def test_a_record_that_names_no_variable_is_unchanged(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "http://litellm:4000")

    assert service_registry.engine_base_address({"base_url": "http://engine:9/v1"}) == "http://engine:9/v1"
    assert service_registry.engine_base_address({"host": "engine", "port": 9}) == "http://engine:9"


# -- the stored address stays the stored address -----------------------------------------------------------------
# A credential is bound to the origin the record stores, and the owner's approval stamps that origin. Neither may
# follow the process environment, or a changed variable would move where a key goes without the owner's approval.


def test_the_stored_address_ignores_the_variable(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "http://litellm:4000")
    config = _release_litellm()["config"]

    assert service_registry.service_base_url(config) == DEFAULT
    assert service_registry.engine_origin(config) == service_registry.origin_of(DEFAULT)
    assert url_binding.base_of({"config": config}, "config.health_endpoint") == DEFAULT


def test_the_credential_fingerprint_ignores_the_variable(monkeypatch):
    config = _release_litellm()["config"]
    monkeypatch.delenv(ENV_NAME, raising=False)
    before = service_registry._endpoint_fingerprint(config)

    monkeypatch.setenv(ENV_NAME, "http://litellm:4000")

    assert service_registry._endpoint_fingerprint(config) == before


def test_the_egress_gate_maps_the_address_the_variable_names(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "http://litellm:4000")

    assert {"litellm:4000", "127.0.0.1:4011"} <= destinations_of(_release_litellm())
