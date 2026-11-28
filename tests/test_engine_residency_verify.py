"""An unload that declares ``verify: none`` takes the engine's own 2xx answer as its evidence."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway.engine_control import EngineControlRuntime
from gateway.engine_residency import ResidencyController
from tests.support.fake_upstream import HandlerTransport

SERVICE = {"residency": {"unload": {"op": "unload", "verify": "none"}}}


def controller(calls: list, *, observed=None):
    async def invoke(name, args):
        calls.append(("invoke", name))

    async def observe(name):
        calls.append(("observe", name))
        return observed

    return ResidencyController(invoke=invoke, observe=observe)


def test_a_declared_no_verify_unload_is_true_after_the_operation_succeeds_and_reads_nothing() -> None:
    calls: list = []
    assert asyncio.run(controller(calls).unload(SERVICE)) is True
    assert calls == [("invoke", "unload")]


def test_the_operation_failing_is_not_hidden_by_no_verify() -> None:
    async def invoke(name, args):
        raise ValueError("control operation failed")

    with pytest.raises(ValueError):
        asyncio.run(ResidencyController(invoke=invoke, observe=None).unload(SERVICE))


@pytest.mark.parametrize("verify", ["maybe", "", True, 0, ["none"]])
def test_an_unknown_verify_value_is_refused_before_anything_is_sent(verify) -> None:
    calls: list = []
    service = {"residency": {"unload": {"op": "unload", "verify": verify}}}
    assert asyncio.run(controller(calls).unload(service)) is False
    assert calls == []


def test_an_unload_without_verify_still_reads_the_resident_fact() -> None:
    calls: list = []
    service = {"residency": {"unload": {"op": "unload"}}}
    assert asyncio.run(controller(calls, observed=None).unload(service)) is False
    assert calls == [("invoke", "unload"), ("observe", "resident")]


def test_the_control_runtime_sends_the_declared_operation_to_the_control_address_and_is_true_on_2xx() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"unloaded": ["a"]})

    service = {
        "id": "speech-a",
        "config": {
            "connection": {"address": "http://127.0.0.1:9001", "control_address": "http://127.0.0.1:9002"},
            "protocol_operations": {"unload_all": {"method": "POST", "path": "/v1/tasks/unload_all_models"}},
            "residency": {"unload": {"op": "unload_all", "args": {}, "verify": "none"}},
        },
    }
    runtime = EngineControlRuntime(
        service, drain=None, lock_for=None,
        client_factory=lambda **_kw: httpx.AsyncClient(transport=HandlerTransport(handler)))
    assert asyncio.run(runtime.unload()) is True
    assert [(r.method, str(r.url)) for r in sent] == [("POST", "http://127.0.0.1:9002/v1/tasks/unload_all_models")]
    assert sent[0].content == b"{}"


def test_the_control_runtime_is_false_when_the_operation_is_not_declared() -> None:
    service = {"id": "x", "config": {"connection": {"address": "http://127.0.0.1:9001"}, "residency": {}}}
    runtime = EngineControlRuntime(service, drain=None, lock_for=None)
    assert asyncio.run(runtime.unload()) is False
