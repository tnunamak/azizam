"""Self-tests for the fake upstream package (TEST_STRATEGY 4.6, slice FK1)."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from tests.support.fake_upstream import (
    FakeUpstreams,
    ScenarioError,
    VirtualClock,
    load_scenario,
    scenario_from_data,
    verify,
)
from tests.support.fake_upstream.clock import ClockClosed
from tests.support.fake_upstream.scenario import SCENARIO_DIR
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

_PLACEHOLDER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


def test_every_scenario_file_loads_and_lives_under_its_protocol() -> None:
    problems = []
    paths = sorted(SCENARIO_DIR.rglob("*.json"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        values = {name: "fk-value" for name in _PLACEHOLDER.findall(text)}
        try:
            scenario = scenario_from_data(json.loads(text), source=str(path), **values)
        except ScenarioError as error:
            problems.append(str(error))
            continue
        folder = path.parent.name
        if folder != "selftest" and folder != scenario.protocol.name:
            problems.append(f"{path}: protocol {scenario.protocol.name!r} filed under {folder!r}")
        if scenario.name != path.stem:
            problems.append(f"{path}: name {scenario.name!r} differs from the file name")
    assert problems == []


def _valid() -> dict:
    return {
        "protocol": "openai-compatible",
        "name": "probe",
        "provenance": {"source": "hand-written", "reference": "unit test"},
        "exchanges": [
            {"id": "a", "match": {"method": "GET", "path": "/v1/models"}, "response": {"json": {}}},
        ],
    }


def _with(path: tuple, value: object) -> dict:
    data = _valid()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_with(("protocol",), "grpc"), "unknown protocol 'grpc'"),
        (_with(("provenance",), {"source": "guess", "reference": "x"}), "provenance.source"),
        (_with(("provenance",), {"source": "hand-written", "reference": " "}), "provenance.reference"),
        (_with(("exchanges", 0, "match", "path"), "/api/chat"), "is not part of protocol 'openai-compatible'"),
        (_with(("exchanges", 0, "match", "verb"), "GET"), "unknown keys ['verb']"),
        (_with(("exchanges", 0, "response"), {"body": [{"text": "a", "hex": "00"}]}), "exactly one of"),
        (_with(("exchanges", 0, "response", "body"), []), "either json or body, not both"),
        (_with(("exchanges", 0, "times"), 0), "times must be a positive integer"),
        (_with(("exchanges", 0, "match", "json"), {"model": "{{model}}"}), "placeholder {{model}} has no value"),
        (_with(("exchanges",), []), "at least one exchange"),
    ],
    ids=["protocol", "source", "reference", "path", "match-key", "part", "json-and-body", "times", "placeholder", "empty"],
)
def test_invalid_scenarios_fail_at_load(data: dict, message: str) -> None:
    with pytest.raises(ScenarioError) as error:
        scenario_from_data(data)
    assert message in str(error.value)


def test_event_parts_need_a_protocol_with_event_framing() -> None:
    data = _valid()
    data["protocol"] = "comfyui"
    data["exchanges"][0]["match"]["path"] = "/history"
    data["exchanges"][0]["response"] = {"body": [{"event": {"a": 1}}]}
    with pytest.raises(ScenarioError, match="protocol 'comfyui' has no event framing"):
        scenario_from_data(data)


def test_unexpected_call_fails_the_test_even_when_the_gateway_absorbs_the_599(monkeypatch, tmp_path) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.asgi(load_scenario("openai-compatible/chat-text", model="fk-model", content="x"), host="litellm.fk.test")
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)

    async def list_models() -> httpx.Response:
        async with gateway_client() as client:
            return await client.get("/v1/models")

    response = asyncio.run(list_models())
    fakes.close()

    # The gateway treats a non-200 catalog as "no LiteLLM models": the 599 is absorbed.
    assert response.status_code == 200
    assert [request.path for request in fake.requests] == ["/v1/models"]
    problems = fakes.problems()
    assert problems[0].startswith("chat-text@litellm.fk.test: unmatched request GET /v1/models: ")
    assert problems[1:] == ["chat-text@litellm.fk.test: required exchange 'chat' was called 0 time(s), expected 1 time(s)"]
    with pytest.raises(pytest.fail.Exception) as failure:
        verify(fakes)
    assert str(failure.value) == "fake upstream contract broken:\n" + "\n".join(problems)


def test_calls_out_of_order_or_past_their_count_are_latched(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.asgi(load_scenario("selftest/ordered-pair"), host="pair.fk.test")

    async def calls() -> list[httpx.Response]:
        async with httpx.AsyncClient(base_url=fake.base_url) as client:
            return [
                await client.get("/v1/models"),
                await client.post("/v1/embeddings", json={"input": "c"}),
                await client.post("/v1/embeddings", json={"input": "a"}),
                await client.post("/v1/embeddings", json={"input": "a"}),
                await client.post("/v1/embeddings", json={"input": "c"}),
                await client.get("/v1/models"),
            ]

    responses = asyncio.run(calls())
    fakes.close()

    assert [r.status_code for r in responses] == [200, 599, 200, 599, 201, 200]
    assert responses[4].headers["x-fk"] == "1"
    assert responses[4].json() == {"n": 3}
    assert responses[1].json() == {
        "error": {"type": "fake_upstream_violation", "message": "exchange 'last' called out of order"},
        "request": {"method": "POST", "path": "/v1/embeddings", "query": {}, "body": {"input": "c"}},
    }
    assert [request.exchange for request in fake.requests] == ["models", None, "first", None, "last", "models"]
    assert [p.split(": {")[0] for p in fakes.problems()] == [
        "ordered-pair@pair.fk.test: exchange 'last' called out of order",
        "ordered-pair@pair.fk.test: exchange 'first' called more than 1 time(s)",
    ]


def test_unconsumed_required_exchanges_fail_and_optional_ones_do_not(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fakes.asgi(load_scenario("selftest/ordered-pair"), host="pair.fk.test")
    fakes.close()
    assert fakes.problems() == [
        "ordered-pair@pair.fk.test: required exchange 'models' was called 0 time(s), expected at least once",
        "ordered-pair@pair.fk.test: required exchange 'first' was called 0 time(s), expected 1 time(s)",
        "ordered-pair@pair.fk.test: required exchange 'last' was called 0 time(s), expected 1 time(s)",
    ]


async def _read_until(chunks, received: bytearray, suffix: bytes) -> None:
    while not received.endswith(suffix):
        received += await chunks.__anext__()


EVENT_A = b'{"message":{"content":"a"},"done":false}\n'
CUT = b'{"message":{"content":"caf\xc3'
REST = b'\xa9"},"done":false}\n'


def test_loopback_sends_nothing_past_a_cut_until_the_clock_moves_then_drops(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.loopback(load_scenario("selftest/faults"))
    clock = fakes.clock

    async def scenario() -> None:
        async with httpx.AsyncClient(base_url=fake.base_url) as client:
            async with client.stream("POST", "/api/chat", json={}) as response:
                assert response.headers["content-type"] == "application/x-ndjson"
                assert response.headers["transfer-encoding"] == "chunked"
                chunks = response.aiter_raw().__aiter__()
                received = bytearray()
                await _read_until(chunks, received, b"\xc3")
                await clock.until_waiting()
                assert bytes(received) == EVENT_A + CUT
                clock.advance(1)
                await _read_until(chunks, received, b"\n")
                await clock.until_waiting()
                assert bytes(received).decode("utf-8") == (EVENT_A + CUT + REST).decode("utf-8")
                clock.advance(1)
                with pytest.raises(httpx.RemoteProtocolError):
                    await chunks.__anext__()
            with pytest.raises(httpx.RemoteProtocolError, match="Server disconnected without sending a response"):
                await client.post("/api/generate", json={})

    try:
        asyncio.run(scenario())
    finally:
        fakes.close()
    assert fakes.problems() == []


def test_in_process_mode_delivers_each_body_part_as_one_chunk(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.asgi(load_scenario("selftest/faults"), host="faults.fk.test")
    clock = fakes.clock

    async def scenario() -> list[bytes]:
        received: list[bytes] = []
        async with httpx.AsyncClient(base_url=fake.base_url) as client:
            async with client.stream("POST", "/api/chat", json={}) as response:
                with pytest.raises(httpx.RemoteProtocolError):
                    async for chunk in response.aiter_raw():
                        received.append(chunk)
                        if chunk in (b"\xc3", b'"},"done":false}\n'):
                            await clock.until_waiting()
                            clock.advance(1)
            with pytest.raises(httpx.RemoteProtocolError, match="Server disconnected without sending a response"):
                await client.post("/api/generate", json={})
        return received

    try:
        received = asyncio.run(scenario())
    finally:
        fakes.close()
    assert received == [EVENT_A, b'{"message":{"content":"caf', b"\xc3", b"\xa9", b'"},"done":false}\n']
    assert fakes.problems() == []


def test_virtual_clock_releases_waiters_only_when_the_test_advances_it() -> None:
    clock = VirtualClock()

    async def scenario() -> list[str]:
        order: list[str] = []

        async def sleeper(name: str, seconds: float) -> None:
            await clock.sleep(seconds)
            order.append(name)

        short = asyncio.create_task(sleeper("short", 1))
        long = asyncio.create_task(sleeper("long", 5))
        forever = asyncio.create_task(sleeper("forever", float("inf")))
        await clock.until_waiting(3)
        clock.advance(0.5)
        await asyncio.sleep(0)
        order.append("after 0.5")
        clock.advance(0.5)
        await short
        clock.advance(10)
        await long
        clock.close()
        with pytest.raises(ClockClosed):
            await forever
        return order

    assert asyncio.run(scenario()) == ["after 0.5", "short", "long"]
    assert clock.now() == 11
    assert clock.waiting == 0
