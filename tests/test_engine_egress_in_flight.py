"""The egress client counts the requests it has sent to an address and not yet finished: the busy signal for engines
(speech servers) that hold no serving lease."""

from __future__ import annotations

import asyncio
import contextvars
import gc

import httpx
import pytest

from gateway import engine_egress
from tests.support.fake_upstream import HandlerTransport

ADDRESS = "http://127.0.0.1:9101"
KEY = "127.0.0.1:9101"


@pytest.fixture(autouse=True)
def published():
    engine_egress.install(engine_egress.project({
        "speech-a": {"enabled": True, "base_url": ADDRESS},
        "speech-b": {"enabled": True, "base_url": "http://127.0.0.1:9102"},
    }))
    yield


class Body(httpx.AsyncByteStream):
    """An open body, as a real transport returns it (a response built from ``content`` is read and closed already)."""

    async def __aiter__(self):
        yield b"abc"

    async def aclose(self) -> None:
        pass


def client(handler):
    return engine_egress.async_client(transport=HandlerTransport(handler))


def test_a_request_counts_from_the_send_to_the_end_of_its_response() -> None:
    seen: list[int] = []

    async def handler(request):
        seen.append(engine_egress.in_flight("speech-a"))
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            await c.post(f"{ADDRESS}/v1/audio/speech", json={})
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0
    assert seen == [1]


def test_only_the_service_at_that_address_is_busy() -> None:
    seen: list[tuple[int, int]] = []

    async def handler(request):
        seen.append((engine_egress.in_flight("speech-a"), engine_egress.in_flight("speech-b")))
        return httpx.Response(200)

    async def go():
        async with client(handler) as c:
            await c.get(f"{ADDRESS}/x")

    asyncio.run(go())
    assert seen == [(1, 0)]


def test_a_streamed_response_counts_until_it_is_closed() -> None:
    during: list[int] = []

    async def handler(request):
        return httpx.Response(200, stream=Body())

    async def go():
        async with client(handler) as c:
            async with c.stream("POST", f"{ADDRESS}/v1/audio/speech") as response:
                during.append(engine_egress.in_flight("speech-a"))
                await response.aread()
                during.append(engine_egress.in_flight("speech-a"))
            during.append(engine_egress.in_flight("speech-a"))

    asyncio.run(go())
    assert during[0] == 1 and during[-1] == 0


def test_a_streamed_response_left_unread_still_ends_when_closed() -> None:
    async def handler(request):
        return httpx.Response(200, stream=Body())

    async def go():
        async with client(handler) as c:
            request = c.build_request("POST", f"{ADDRESS}/x")
            response = await c.send(request, stream=True)
            assert engine_egress.in_flight("speech-a") == 1
            await response.aclose()
            await response.aclose()  # closing twice must not count twice
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0


def test_a_request_that_fails_does_not_stay_counted() -> None:
    async def handler(request):
        raise httpx.ConnectError("refused")

    async def go():
        async with client(handler) as c:
            with pytest.raises(httpx.ConnectError):
                await c.get(f"{ADDRESS}/x")
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0


def test_a_cancelled_request_does_not_stay_counted() -> None:
    gate = asyncio.Event()

    async def handler(request):
        await gate.wait()
        return httpx.Response(200)

    async def go():
        async with client(handler) as c:
            task = asyncio.create_task(c.get(f"{ADDRESS}/x"))
            for _ in range(20):
                await asyncio.sleep(0)
                if engine_egress.in_flight("speech-a"):
                    break
            assert engine_egress.in_flight("speech-a") == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0


def test_a_health_probe_is_not_work() -> None:
    seen: list[int] = []

    async def handler(request):
        seen.append(engine_egress.in_flight("speech-a"))
        return httpx.Response(200)

    async def go():
        async with client(handler) as c:
            await c.get(f"{ADDRESS}/health", extensions=engine_egress.probe())

    asyncio.run(go())
    assert seen == [0]


def test_a_service_the_gateway_does_not_know_has_nothing_in_flight() -> None:
    assert engine_egress.in_flight("nobody") == 0


# --- the unload exclusion -------------------------------------------------------------------------------------------


def _signal_gate_arrivals(monkeypatch) -> asyncio.Event:
    """An event set when a request reaches a held gate (``_enter`` hands it the gate to wait on)."""
    arrived = asyncio.Event()
    enter = engine_egress._enter

    def watched(key):
        gate = enter(key)
        if gate is not None:
            arrived.set()
        return gate

    monkeypatch.setattr(engine_egress, "_enter", watched)
    return arrived


def test_a_request_that_arrives_during_an_unload_waits_for_it_then_runs(monkeypatch) -> None:
    reached: list[str] = []

    async def handler(request):
        reached.append(request.url.path)
        return httpx.Response(200, json={})

    async def go():
        arrived = _signal_gate_arrivals(monkeypatch)
        async with client(handler) as c:
            assert engine_egress.begin_unload("speech-a") is True
            # another request's task: it does not inherit the unload's claim
            sent = asyncio.get_running_loop().create_task(
                c.post(f"{ADDRESS}/v1/audio/speech", json={}), context=contextvars.Context())
            async with asyncio.timeout(5):
                await arrived.wait()  # it reached the gate and was held there
            assert reached == []
            assert engine_egress.in_flight("speech-a") == 0
            engine_egress.end_unload("speech-a")
            async with asyncio.timeout(5):
                assert (await sent).status_code == 200
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0
    assert reached == ["/v1/audio/speech"]


def test_begin_unload_refuses_while_a_request_is_in_flight() -> None:
    answers: list[bool] = []

    async def handler(request):
        answers.append(engine_egress.begin_unload("speech-a"))
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            await c.post(f"{ADDRESS}/v1/audio/speech", json={})
        ok = engine_egress.begin_unload("speech-a")
        engine_egress.end_unload("speech-a")
        return ok

    assert asyncio.run(go()) is True  # idle again after the request
    assert answers == [False]


def test_begin_unload_refuses_while_another_unload_holds_the_address() -> None:
    async def go():
        first = engine_egress.begin_unload("speech-a")
        second = await asyncio.ensure_future(_begin_in_new_task("speech-a"))
        other_engine = await asyncio.ensure_future(_begin_in_new_task("speech-b"))
        engine_egress.end_unload("speech-a")
        engine_egress.end_unload("speech-b")
        after = await asyncio.ensure_future(_begin_in_new_task("speech-a"))
        return first, second, other_engine, after

    assert asyncio.run(go()) == (True, False, True, True)


def test_begin_unload_outside_a_task_is_refused_before_any_gate_exists() -> None:
    with pytest.raises(RuntimeError):
        engine_egress.begin_unload("speech-a")
    assert engine_egress._UNLOADING == {}
    assert engine_egress._UNLOAD_OWNER.get() == ()
    # The address stays free for a real unload.
    assert asyncio.run(_begin_in_new_task("speech-a")) is True


async def _begin_in_new_task(service_id: str) -> bool:
    ok = engine_egress.begin_unload(service_id)
    if ok:
        engine_egress.end_unload(service_id)
    return ok


def test_the_unloads_own_request_neither_waits_nor_counts() -> None:
    seen: list[int] = []

    async def handler(request):
        seen.append(engine_egress.in_flight("speech-a"))
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            assert engine_egress.begin_unload("speech-a") is True
            try:
                async with asyncio.timeout(5):
                    await c.post(f"{ADDRESS}/unload", json={})
            finally:
                engine_egress.end_unload("speech-a")
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0
    assert seen == [0]


def test_end_unload_is_safe_twice_and_a_task_after_it_is_counted_again() -> None:
    seen: list[int] = []

    async def handler(request):
        seen.append(engine_egress.in_flight("speech-a"))
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            engine_egress.begin_unload("speech-a")
            engine_egress.end_unload("speech-a")
            engine_egress.end_unload("speech-a")
            await c.post(f"{ADDRESS}/v1/audio/speech", json={})

    asyncio.run(go())
    assert seen == [1]


def test_the_claim_is_ended_by_a_task_that_overlaps_nothing_else() -> None:
    """A second task's request is held only for the address that is claimed."""
    reached: list[str] = []

    async def handler(request):
        reached.append(request.url.port)
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            engine_egress.begin_unload("speech-a")
            await asyncio.ensure_future(c.post("http://127.0.0.1:9102/x", json={}))  # speech-b is not claimed
            engine_egress.end_unload("speech-a")

    asyncio.run(go())
    assert reached == [9102]


def test_end_unload_frees_the_claimed_gate_after_the_registry_moved_the_service() -> None:
    """The lease keeps the addresses it claimed; a registry update meanwhile cannot strand a gate."""
    reached: list[int] = []

    async def handler(request):
        reached.append(request.url.port)
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            assert engine_egress.begin_unload("speech-a") is True
            engine_egress.install(engine_egress.project({  # speech-a now lives at another address
                "speech-a": {"enabled": True, "base_url": "http://127.0.0.1:9103"},
                "speech-b": {"enabled": True, "base_url": "http://127.0.0.1:9102"},
            }))
            engine_egress.end_unload("speech-a")
            sent = asyncio.get_running_loop().create_task(
                c.post(f"{ADDRESS}/v1/audio/speech", json={}), context=contextvars.Context())
            async with asyncio.timeout(5):
                await sent

    asyncio.run(go())
    assert reached == [9101]
    assert engine_egress._UNLOADING == {}


def test_a_task_copied_during_an_unload_is_gated_once_the_lease_ends() -> None:
    """A child task inherits the owner's context; after end_unload that copy grants nothing, so it is counted."""
    seen: list[int] = []

    async def handler(request):
        seen.append(engine_egress.in_flight("speech-a"))
        return httpx.Response(200, json={})

    async def go():
        async with client(handler) as c:
            assert engine_egress.begin_unload("speech-a") is True
            copied = contextvars.copy_context()  # what create_task takes from the owner
            engine_egress.end_unload("speech-a")
            later = asyncio.get_running_loop().create_task(
                c.post(f"{ADDRESS}/v1/audio/speech", json={}), context=copied)
            async with asyncio.timeout(5):
                await later

    asyncio.run(go())
    assert seen == [1]  # counted as serving, not passed as the unload's own request


def test_two_services_unloading_in_one_task_end_independently() -> None:
    async def go():
        assert engine_egress.begin_unload("speech-a") is True
        assert engine_egress.begin_unload("speech-b") is True
        engine_egress.end_unload("speech-a")
        held_after_a = set(engine_egress._UNLOADING)
        refused_b = await asyncio.ensure_future(_begin_in_new_task("speech-b"))
        engine_egress.end_unload("speech-b")
        engine_egress.end_unload("speech-b")
        return held_after_a, refused_b, set(engine_egress._UNLOADING)

    assert asyncio.run(go()) == ({"127.0.0.1:9102"}, False, set())


def test_a_task_started_during_an_unload_waits_like_any_other_request(monkeypatch) -> None:
    """The lease belongs to the task that took it; a task it starts shares the context, not the bypass."""
    reached: list[str] = []

    async def handler(request):
        reached.append(request.url.path)
        return httpx.Response(200, json={})

    async def go():
        arrived = _signal_gate_arrivals(monkeypatch)
        async with client(handler) as c:
            assert engine_egress.begin_unload("speech-a") is True
            child = asyncio.get_running_loop().create_task(c.post(f"{ADDRESS}/v1/audio/speech", json={}))  # inherits the context
            async with asyncio.timeout(5):
                await arrived.wait()
            assert reached == []
            await c.post(f"{ADDRESS}/unload", json={})  # the owner's own request still passes
            assert reached == ["/unload"]
            engine_egress.end_unload("speech-a")
            async with asyncio.timeout(5):
                await child
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0
    assert reached == ["/unload", "/v1/audio/speech"]


# --- a response dropped without closing -------------------------------------------------------------------------------


def test_a_streamed_response_dropped_without_closing_ends_its_count() -> None:
    async def handler(request):
        return httpx.Response(200, stream=Body())

    async def go():
        c = client(handler)
        request = c.build_request("POST", f"{ADDRESS}/v1/audio/speech")
        response = await c.send(request, stream=True)
        assert engine_egress.in_flight("speech-a") == 1
        del response, request
        gc.collect()
        left = engine_egress.in_flight("speech-a")
        await c.aclose()
        return left

    assert asyncio.run(go()) == 0


def test_a_close_and_the_finalizer_count_down_once() -> None:
    async def handler(request):
        return httpx.Response(200, stream=Body())

    async def go():
        async with client(handler) as c:
            other = c.build_request("POST", "http://127.0.0.1:9101/other")
            held = await c.send(other, stream=True)  # a second request that stays open
            request = c.build_request("POST", f"{ADDRESS}/v1/audio/speech")
            response = await c.send(request, stream=True)
            assert engine_egress.in_flight("speech-a") == 2
            await response.aclose()
            assert engine_egress.in_flight("speech-a") == 1
            del response, request
            gc.collect()
            assert engine_egress.in_flight("speech-a") == 1  # the finalizer did not count the same request twice
            await held.aclose()
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0


def test_one_client_reused_for_several_streamed_requests_leaves_none_counted() -> None:
    async def handler(request):
        return httpx.Response(200, stream=Body())

    async def go():
        async with client(handler) as c:
            for _ in range(3):
                async with c.stream("POST", f"{ADDRESS}/v1/audio/speech") as response:
                    await response.aread()
            dropped = await c.send(c.build_request("POST", f"{ADDRESS}/v1/audio/speech"), stream=True)
            del dropped
            gc.collect()
        return engine_egress.in_flight("speech-a")

    assert asyncio.run(go()) == 0
