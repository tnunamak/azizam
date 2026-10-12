import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from gateway import lifecycle_drivers as ld


def _service(model_key="model_name"):
    return {
        "host": "engine.invalid",
        "port": 9123,
        "lifecycle": {
            "driver": "http-load-unload",
            "driver_config": {
                "activation_model_key": model_key,
                "load_url": "/load",
                "unload_url": "/unload",
                "verify_endpoint": "/verify",
                "success_marker": '"status":"finished"',
            },
        },
    }


def _install_replay(
    monkeypatch,
    *,
    load_content=b'{"status":"finished"}',
    verified_model="wanted",
):
    original_client = httpx.AsyncClient
    calls = []

    def reply(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/load":
            return httpx.Response(200, content=load_content)
        if request.method == "POST" and request.url.path == "/unload":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/verify":
            return httpx.Response(200, json={"id": verified_model})
        raise AssertionError(f"unexpected fake request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(reply)

    def factory(*args, **kwargs):
        kwargs.setdefault("trust_env", False)
        return original_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


@pytest.mark.parametrize("operation", ["swap_required", "swap"])
def test_custom_activation_model_key_round_trips(monkeypatch, operation):
    calls = _install_replay(monkeypatch)
    driver = ld.HttpLoadUnloadDriver()
    service = _service("deployment")
    activation = driver.build_activation(service, model="wanted")
    assert activation == {"deployment": "wanted"}
    ctx = ld.SwapContext(loaded_model=None, target_model="wanted")

    if operation == "swap_required":
        assert driver.swap_required(service, activation, ctx) is True
        assert calls == []
    else:
        assert asyncio.run(driver.swap(service, activation, ctx=ctx)) is True
        assert calls == [("POST", "/load", {"deployment": "wanted"})]


@pytest.mark.parametrize("params_source", ["explicit", "provider"])
def test_extra_params_cannot_replace_the_requested_model(monkeypatch, params_source):
    calls = _install_replay(monkeypatch)
    service = _service()
    service["lifecycle"]["driver_config"]["load_body_params"] = "test-gap-provider"
    params = {"model_name": "different-model", "temperature": 0.25}
    provider = Mock(return_value=dict(params))
    monkeypatch.setitem(ld.PARAMS_PROVIDERS, "test-gap-provider", provider)
    activation = {"model_name": "wanted"}
    if params_source == "explicit":
        activation["params"] = dict(params)

    result = asyncio.run(
        ld.HttpLoadUnloadDriver().swap(
            service,
            activation,
            ctx=ld.SwapContext(loaded_model=None, target_model="wanted"),
        )
    )
    assert result is True
    assert calls == [
        ("POST", "/load", {"model_name": "wanted", "temperature": 0.25})
    ]
    if params_source == "explicit":
        provider.assert_not_called()
    else:
        provider.assert_called_once_with(service)
    assert params["model_name"] == "different-model"


@pytest.mark.parametrize(
    "running,healthy,expected",
    [
        (None, False, "unknown"),
        (False, False, "stopped"),
        (True, True, "running"),
        (True, False, "starting"),
    ],
)
def test_docker_status_handles_every_inspection_outcome(
    monkeypatch, running, healthy, expected
):
    driver = ld.DockerDriver()
    inspect = AsyncMock(return_value=running)
    poll = AsyncMock(return_value=healthy)
    monkeypatch.setattr(driver, "_is_running", inspect)
    monkeypatch.setattr(ld, "_poll_health", poll)
    service = {
        "lifecycle": {
            "driver": "docker",
            "driver_config": {"container": "fake-container"},
        }
    }

    assert asyncio.run(driver.status(service)) == expected
    inspect.assert_awaited_once_with(service["lifecycle"]["driver_config"])
    if running is True:
        poll.assert_awaited_once_with(service, driver._DOCKER_POLL_INTERVAL_S)
    else:
        poll.assert_not_awaited()
