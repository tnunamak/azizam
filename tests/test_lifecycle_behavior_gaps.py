import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import lifecycle_drivers as ld
from tests.support.fake_upstream import fake_upstreams, load_scenario, read_deadline  # noqa: F401

HANG_BOUND_SECONDS = 10


def _service(host="engine.invalid", port=9123, model_key="model_name"):
    return {
        "host": host,
        "port": port,
        "lifecycle": {
            "driver": "http-load-unload",
            "driver_config": {
                "activation_model_key": model_key,
                "load_url": "/v1/model/load",
                "unload_url": "/v1/model/unload",
                "verify_endpoint": "/v1/model",
                "success_marker": '"status":"finished"',
            },
        },
    }


def _swap(fakes, scenario, *, loaded="old", wire=False, release_waits=0, activation=None, driver_config=None, **values):
    """Run one swap against the model-server fake; the clock advances once per held-back chunk."""
    fake_scenario = load_scenario(f"http-load-unload/{scenario}", **values)
    if wire:
        fake = fakes.loopback(fake_scenario)
        host, port = fake.base_url.removeprefix("http://").split(":")
        service = _service(host, int(port))
    else:
        fake = fakes.asgi(fake_scenario, host="engine.invalid:9123")
        service = _service()
    service["lifecycle"]["driver_config"].update(driver_config or {})

    async def call():
        async def pump():
            for _ in range(release_waits):
                await fakes.clock.until_waiting()
                fakes.clock.advance(1)

        async with asyncio.timeout(HANG_BOUND_SECONDS):
            releaser = asyncio.create_task(pump())
            result = await ld.HttpLoadUnloadDriver().swap(
                service,
                activation or {"model_name": "wanted"},
                ctx=ld.SwapContext(loaded_model=loaded, target_model="wanted"),
            )
            await releaser
            return result

    return asyncio.run(call()), fake


def _calls(fake):
    return [(r.method, r.path, r.json if r.body else None) for r in fake.requests]


def test_explicit_params_skip_the_live_provider(fake_upstreams, monkeypatch):
    provider = Mock(side_effect=AssertionError("explicit params must win"))
    monkeypatch.setitem(ld.PARAMS_PROVIDERS, "test-gap-provider", provider)
    result, fake = _swap(
        fake_upstreams, "load-explicit-params", loaded=None,
        activation={"model_name": "wanted", "params": {"temperature": 0.75}},
        driver_config={"load_body_params": "test-gap-provider"},
    )
    assert result is True
    provider.assert_not_called()
    assert _calls(fake) == [
        ("POST", "/v1/model/load", {"model_name": "wanted", "temperature": 0.75})
    ]


def test_custom_activation_model_key_round_trips(fake_upstreams):
    service = _service(model_key="deployment")
    driver = ld.HttpLoadUnloadDriver()
    activation = driver.build_activation(service, model="wanted")
    assert activation == {"deployment": "wanted"}
    ctx = ld.SwapContext(loaded_model=None, target_model="wanted")
    # The decision to swap reads the same key and makes no call.
    assert driver.swap_required(service, activation, ctx) is True
    result, fake = _swap(
        fake_upstreams, "load-custom-key", loaded=None,
        activation=activation, driver_config={"activation_model_key": "deployment"},
    )
    assert result is True
    assert _calls(fake) == [("POST", "/v1/model/load", {"deployment": "wanted"})]


@pytest.mark.parametrize("params_source", ["explicit", "provider"])
def test_extra_params_cannot_replace_the_requested_model(fake_upstreams, monkeypatch, params_source):
    params = {"model_name": "different-model", "temperature": 0.25}
    provider = Mock(return_value=dict(params))
    monkeypatch.setitem(ld.PARAMS_PROVIDERS, "test-gap-provider", provider)
    service = _service()
    activation = {"model_name": "wanted"}
    if params_source == "explicit":
        activation["params"] = dict(params)
    result, fake = _swap(
        fake_upstreams, "load-params-collision", loaded=None,
        activation=activation, driver_config={"load_body_params": "test-gap-provider"},
    )
    assert result is True
    assert _calls(fake) == [("POST", "/v1/model/load", {"model_name": "wanted", "temperature": 0.25})]
    if params_source == "explicit":
        provider.assert_not_called()
    else:
        provider.assert_called_once()
    assert params["model_name"] == "different-model"


@pytest.mark.parametrize(
    "running,healthy,expected",
    [(None, False, "unknown"), (False, False, "stopped"), (True, True, "running"), (True, False, "starting")],
)
def test_docker_status_handles_every_inspection_outcome(monkeypatch, running, healthy, expected):
    driver = ld.DockerDriver()
    inspect = AsyncMock(return_value=running)
    poll = AsyncMock(return_value=healthy)
    monkeypatch.setattr(driver, "_is_running", inspect)
    monkeypatch.setattr(ld, "_poll_health", poll)
    service = {"lifecycle": {"driver": "docker", "driver_config": {"container": "fake-container"}}}

    assert asyncio.run(driver.status(service)) == expected
    inspect.assert_awaited_once_with(service["lifecycle"]["driver_config"])
    if running is True:
        poll.assert_awaited_once_with(service, driver._DOCKER_POLL_INTERVAL_S)
    else:
        poll.assert_not_awaited()


@pytest.mark.parametrize(
    "content,verified_model,expected,expected_gets",
    [
        (b'{"status":"finished"}', "different", True, 0),
        (b'{"status":"error","message":"fake failure"}', "wanted", False, 0),
        # The operator-declared success marker is authoritative: a later error
        # line does not turn a finished load into a failure.
        (b'{"status":"finished"}\n{"status":"error","message":"fake failure"}', "wanted", True, 0),
        (b'{"status":"loading"}', "wanted", True, 1),
        (b'{"status":"loading"}', "different", False, 1),
        (b'{"status":"loading"}', None, False, 1),
    ],
)
def test_load_outcome_requires_no_error_or_exact_verified_target(
    fake_upstreams, content, verified_model, expected, expected_gets
):
    result, fake = _swap(
        fake_upstreams, "load-outcome", load_text=content.decode(), verified=verified_model
    )
    assert result is expected
    calls = _calls(fake)
    assert calls[:2] == [
        ("POST", "/v1/model/unload", None),
        ("POST", "/v1/model/load", {"model_name": "wanted"}),
    ]
    assert calls[2:] == [("GET", "/v1/model", None)] * expected_gets


@pytest.mark.parametrize("wire", [False, True], ids=["asgi", "loopback"])
def test_marker_cut_across_wire_chunks_is_a_success(fake_upstreams, wire):
    result, fake = _swap(fake_upstreams, "load-split-marker", wire=wire, release_waits=1)
    assert result is True
    assert [c[:2] for c in _calls(fake)] == [("POST", "/v1/model/unload"), ("POST", "/v1/model/load")]


def test_a_dropped_load_connection_is_a_failed_swap_without_verify(fake_upstreams):
    result, fake = _swap(fake_upstreams, "load-disconnect", wire=True)
    assert result is False
    assert [c[:2] for c in _calls(fake)] == [("POST", "/v1/model/unload"), ("POST", "/v1/model/load")]


def test_a_rate_limited_load_is_a_failed_swap_without_verify(fake_upstreams):
    result, fake = _swap(fake_upstreams, "load-rate-limited")
    assert result is False
    assert [c[:2] for c in _calls(fake)] == [("POST", "/v1/model/unload"), ("POST", "/v1/model/load")]


def test_a_load_that_never_answers_ends_at_the_read_budget(fake_upstreams, monkeypatch):
    deadline = read_deadline(monkeypatch, fake_upstreams.clock)
    result, fake = _swap(fake_upstreams, "load-slow-first-byte", wire=True)
    assert result is False
    assert deadline.read_fired == 1
    assert [c[:2] for c in _calls(fake)] == [("POST", "/v1/model/unload"), ("POST", "/v1/model/load")]


@pytest.mark.parametrize(
    "running,healthy,expected",
    [
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
