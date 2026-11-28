"""The pool's idle release over declared engines: free what a record says it can unload, never what is serving."""

from __future__ import annotations

import asyncio

import pytest

from gateway import idle_release
from gateway.exclusive_pool import ENGINE_BUSY


def record(devices, *, unload: bool = True, enabled: bool = True, where: str = "config") -> dict:
    body: dict = {"resources": {"gpu": {"devices": devices}}}
    if unload:
        body["residency"] = {"unload": {"op": "unload_all", "verify": "none"}}
    if where == "config":
        return {"enabled": enabled, "config": body}
    return {"enabled": enabled, **body}


SERVICES = {
    "speech-a": record([0]),
    "speech-b": record([0, 1]),
    "elsewhere": record([1]),
    "no-unload": record([0], unload=False),
    "off": record([0], enabled=False),
    "no-devices": {"enabled": True, "config": {"residency": {"unload": {"op": "x"}}}},
    "top-level": record([0], where="top"),
}


def test_the_engines_asked_are_those_that_declare_an_unload_on_one_of_the_pool_devices() -> None:
    assert idle_release.declared_unloaders(SERVICES, (0,)) == ["speech-a", "speech-b", "top-level"]
    assert idle_release.declared_unloaders(SERVICES, (1,)) == ["elsewhere", "speech-b"]
    assert idle_release.declared_unloaders(SERVICES, (2,)) == []


def test_a_pool_without_devices_asks_no_declared_engine() -> None:
    assert idle_release.declared_unloaders(SERVICES, ()) == []


@pytest.mark.parametrize("devices", [[], [-1], [True], ["0"], "0", [0.5], None, {"0": 1}])
def test_malformed_declared_devices_match_nothing(devices) -> None:
    assert idle_release.declared_unloaders({"x": record(devices)}, (0,)) == []


def release(service_id: str, *, busy: set[str] = frozenset(), unload=None):
    seen: list[str] = []

    async def default_unload(sid: str) -> bool:
        seen.append(sid)
        return True

    async def go():
        return await idle_release.release_engine(
            service_id, begin_unload=lambda sid: sid not in busy, end_unload=ended.append,
            unload=unload or default_unload)

    ended: list[str] = []
    return asyncio.run(go()), seen


def test_an_idle_engine_is_unloaded_and_reported_freed() -> None:
    answer, seen = release("speech-a")
    assert (answer, seen) == (True, ["speech-a"])


def test_a_serving_engine_is_not_unloaded_and_is_reported_busy() -> None:
    answer, seen = release("speech-a", busy={"speech-a"})
    assert (answer, seen) == (ENGINE_BUSY, [])


def test_an_unload_that_fails_or_is_not_confirmed_frees_nothing() -> None:
    async def refused(_sid):
        return False

    async def broken(_sid):
        raise RuntimeError("engine unreachable")

    assert release("speech-a", unload=refused)[0] is False
    with pytest.raises(RuntimeError):
        release("speech-a", unload=broken)  # release_engine itself does not hide it; release_together does


def run_members(*members):
    return asyncio.run(idle_release.release_together(list(members)))


def member(answer, calls: list[str], name: str):
    async def run():
        calls.append(name)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return run


def test_every_member_is_asked_and_one_failure_does_not_stop_the_others() -> None:
    calls: list[str] = []
    answer = run_members(member(RuntimeError("boom"), calls, "a"), member(True, calls, "b"), member(False, calls, "c"))
    assert answer is True
    assert sorted(calls) == ["a", "b", "c"]


@pytest.mark.parametrize(("answers", "expected"), [
    ([True, ENGINE_BUSY], True),
    ([False, ENGINE_BUSY], ENGINE_BUSY),
    ([False, False], False),
    ([RuntimeError("x"), False], False),
    ([RuntimeError("x"), ENGINE_BUSY], ENGINE_BUSY),
    ([], False),
])
def test_the_members_answers_combine_freed_then_busy_then_nothing(answers, expected) -> None:
    calls: list[str] = []
    assert run_members(*(member(a, calls, str(i)) for i, a in enumerate(answers))) == expected


def exclusion(service_id: str, unload):
    ended: list[str] = []

    async def go():
        return await idle_release.release_engine(
            service_id, begin_unload=lambda sid: True, end_unload=ended.append, unload=unload)

    return go, ended


def test_the_exclusion_ends_after_a_freed_unload_and_not_at_all_when_busy() -> None:
    async def done(_sid):
        return True

    go, ended = exclusion("speech-a", done)
    assert asyncio.run(go()) is True
    assert ended == ["speech-a"]
    ended_busy: list[str] = []

    async def busy():
        return await idle_release.release_engine(
            "speech-a", begin_unload=lambda sid: False, end_unload=ended_busy.append, unload=done)

    assert asyncio.run(busy()) == ENGINE_BUSY
    assert ended_busy == []  # nothing was claimed


def test_the_exclusion_ends_when_the_unload_raises() -> None:
    async def broken(_sid):
        raise RuntimeError("engine unreachable")

    go, ended = exclusion("speech-a", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert ended == ["speech-a"]


def test_the_exclusion_ends_when_the_unload_is_cancelled() -> None:
    async def run():
        started = asyncio.Event()

        async def hangs(_sid):
            started.set()
            await asyncio.Event().wait()

        go, ended = exclusion("speech-a", hangs)
        task = asyncio.ensure_future(go())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ended

    assert asyncio.run(run()) == ["speech-a"]
