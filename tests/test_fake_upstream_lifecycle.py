"""Self-tests: fixture teardown, expected-failure scope, task lifecycle, mode parity (FK review round 2)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from tests.support.fake_upstream import FakeUpstreams, ScenarioError, VirtualClock, load_scenario, scenario_from_data
from tests.support.fake_upstream.clock import ClockClosed

pytest_plugins = ["pytester"]

_REPO = Path(__file__).resolve().parent.parent

# Child tests run in their own interpreter: the real ``fake_upstreams`` fixture,
# the real teardown. (An in-process pytester run re-imports the gateway and
# corrupts the parent's modules.)
_CHILD = '''
import asyncio
import pytest
from tests.support.fake_upstream import KnownBug, expect_fixed, fake_upstreams, load_scenario  # noqa: F401
from tests.support.gateway import gateway_client, register_litellm, use_admin_db


def _serve(fake_upstreams, monkeypatch, tmp_path, wire=False):
    scenario = load_scenario("openai-compatible/chat-text", model="m", content="x")
    fake = fake_upstreams.loopback(scenario) if wire else fake_upstreams.asgi(scenario, host="litellm.fk.test")
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    return fake


async def _get(path):
    async with gateway_client() as client:
        return await client.get(path)


def test_absorbed_599_asgi(fake_upstreams, monkeypatch, tmp_path):
    _serve(fake_upstreams, monkeypatch, tmp_path)
    assert asyncio.run(_get("/v1/models")).status_code == 200


def test_absorbed_599_loopback(fake_upstreams, monkeypatch, tmp_path):
    _serve(fake_upstreams, monkeypatch, tmp_path, wire=True)
    assert asyncio.run(_get("/v1/models")).status_code == 200


def test_clean(fake_upstreams, monkeypatch, tmp_path):
    _serve(fake_upstreams, monkeypatch, tmp_path)

    async def call():
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})

    assert asyncio.run(call()).status_code == 200


@pytest.mark.xfail(strict=True, raises=KnownBug)
def test_bug_alone():
    expect_fixed("FK-X", 1, 2)


@pytest.mark.xfail(strict=True, raises=KnownBug)
def test_bug_hides_an_assertion_failure():
    assert 1 == 2


@pytest.mark.xfail(strict=True, raises=KnownBug)
def test_bug_hides_a_hang_bound():
    raise TimeoutError()


@pytest.mark.xfail(strict=True, raises=KnownBug)
def test_bug_hides_an_unexpected_call(fake_upstreams, monkeypatch, tmp_path):
    _serve(fake_upstreams, monkeypatch, tmp_path)
    asyncio.run(_get("/v1/models"))
    expect_fixed("FK-X", 1, 2)


@pytest.mark.xfail(strict=True, raises=KnownBug)
def test_bug_fixed_is_xpass():
    expect_fixed("FK-X", 1, 1)
'''

# test name -> every outcome pytest reports for it (call and teardown phases)
_EXPECTED = {
    "test_absorbed_599_asgi": {"PASSED", "ERROR"},
    "test_absorbed_599_loopback": {"PASSED", "ERROR"},
    "test_clean": {"PASSED"},
    "test_bug_alone": {"XFAIL"},
    "test_bug_hides_an_assertion_failure": {"FAILED"},
    "test_bug_hides_a_hang_bound": {"FAILED"},
    "test_bug_hides_an_unexpected_call": {"XFAIL", "ERROR"},
    "test_bug_fixed_is_xpass": {"FAILED"},
}


def test_real_fixture_teardown_and_expected_failure_scope(pytester: pytest.Pytester, monkeypatch) -> None:
    """A swallowed 599 fails the real teardown in both modes; an xfail covers only its KnownBug."""
    monkeypatch.setenv("PYTHONPATH", str(_REPO))
    pytester.makepyfile(test_child=_CHILD)
    result = pytester.runpytest_subprocess("-rA", "-p", "no:randomly", "-p", "no:cacheprovider")
    seen: dict[str, set[str]] = {}
    for line in result.outlines:
        match = re.match(r"(PASSED|FAILED|ERROR|XFAIL|XPASS) test_child\.py::(\w+)", line)
        if match:
            seen.setdefault(match.group(2), set()).add(match.group(1))
    assert seen == _EXPECTED, "\n".join(result.outlines[-40:])


def test_cancelling_a_request_before_its_headers_leaves_no_task_or_waiter(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.asgi(load_scenario("openai-compatible/slow-first-byte", model="m"), host="slow.fk.test")

    async def scenario() -> tuple[int, int]:
        before = len(asyncio.all_tasks())
        async with httpx.AsyncClient() as client:
            call = asyncio.create_task(client.post(f"{fake.base_url}/v1/chat/completions", json={"model": "m"}))
            await fakes.clock.until_waiting()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        await asyncio.sleep(0)
        return len(asyncio.all_tasks()) - before, fakes.clock.waiting

    try:
        leaked_tasks, waiters = asyncio.run(scenario())
    finally:
        fakes.close()
    assert (leaked_tasks, waiters) == (0, 0)
    assert fakes.problems() == []


def test_cancelling_between_chunks_leaves_no_task_or_waiter(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.asgi(load_scenario("selftest/faults"), host="faults.fk.test")

    async def scenario() -> tuple[int, int]:
        before = len(asyncio.all_tasks())

        async def read() -> None:
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", f"{fake.base_url}/v1/chat/completions", json={}) as response:
                    async for _ in response.aiter_raw():
                        pass

        call = asyncio.create_task(read())
        await fakes.clock.until_waiting()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.sleep(0)
        return len(asyncio.all_tasks()) - before, fakes.clock.waiting

    try:
        assert asyncio.run(scenario()) == (0, 0)
    finally:
        fakes.close()


@pytest.mark.parametrize("mode", ["asgi", "loopback"])
def test_percent_encoded_paths_reach_the_fake_decoded_in_both_modes(monkeypatch, mode: str) -> None:
    fakes = FakeUpstreams(monkeypatch)
    scenario = load_scenario("selftest/encoded-path")
    fake = fakes.asgi(scenario, host="enc.fk.test") if mode == "asgi" else fakes.loopback(scenario)

    async def calls() -> list[httpx.Response]:
        async with httpx.AsyncClient(base_url=fake.base_url) as client:
            return [await client.get("/v1/models/café"), await client.get("/v1/models/a%20b"), await client.get("/v1/models/x%252Fy")]

    try:
        responses = asyncio.run(calls())
    finally:
        fakes.close()
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert [r.json()["id"] for r in responses] == ["café", "a b", "x%2Fy"]
    assert [request.path for request in fake.requests] == ["/v1/models/café", "/v1/models/a b", "/v1/models/x%2Fy"]
    assert fakes.problems() == []


def _exchange(**extra) -> dict:
    return {"id": "e", "match": {"method": "GET", "path": "/health"}, "response": {"json": {}}, **extra}


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"repeat": "false"}, "repeat must be a JSON boolean"),
        ({"required": "no"}, "required must be a JSON boolean"),
        ({"required": 0}, "required must be a JSON boolean"),
        ({"times": True}, "times must be a positive integer"),
        ({"times": 2.0}, "times must be a positive integer"),
        ({"repeat": True, "times": 2}, "a repeat exchange has no count"),
    ],
)
def test_scenario_flags_must_be_exact_json_types(extra: dict, message: str) -> None:
    data = {"protocol": "openai-compatible", "name": "n", "provenance": {"source": "hand-written", "reference": "x"}, "exchanges": [_exchange(**extra)]}
    with pytest.raises(ScenarioError, match=message):
        scenario_from_data(data)


def test_an_optional_exchange_allows_up_to_its_count_and_no_more(monkeypatch) -> None:
    fakes = FakeUpstreams(monkeypatch)
    fake = fakes.asgi(load_scenario("selftest/ordered-pair"), host="pair.fk.test")

    async def calls() -> list[int]:
        async with httpx.AsyncClient(base_url=fake.base_url) as client:
            await client.get("/v1/models")
            return [(await client.post("/v1/embeddings", json={"input": value})).status_code for value in ("a", "b", "b", "b", "c")]

    codes = asyncio.run(calls())
    fakes.close()
    assert codes == [200, 200, 200, 599, 201]
    assert [p.split(": {")[0] for p in fakes.problems()] == ["ordered-pair@pair.fk.test: exchange 'second' called more than 2 time(s)"]


def test_until_waiting_after_close_raises_instead_of_hanging() -> None:
    clock = VirtualClock()
    clock.close()

    async def listen() -> None:
        async with asyncio.timeout(5):
            await clock.until_waiting()

    with pytest.raises(ClockClosed):
        asyncio.run(listen())


def test_a_cancelled_listener_is_removed_from_the_clock() -> None:
    clock = VirtualClock()

    async def scenario() -> int:
        listener = asyncio.create_task(clock.until_waiting())
        await asyncio.sleep(0)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        return len(clock._blocked_listeners)

    assert asyncio.run(scenario()) == 0
