"""The owner's status page, Home and availability read one background snapshot. No read waits for a probe.

The snapshot is refreshed only while something reads it. A read keeps it wanted for an idle window. The refresher
builds every interval while it is wanted, and stops once no read has come for the whole window. The next read starts
it again and gets the last snapshot with its age, or `checking` before the first build. Shutdown cancels a build that
is running and waits for it to end. These tests drive time with a fake clock, so a tick costs no real time. The
timeouts only stop a hang from hanging the suite.
"""
import asyncio
import logging
import subprocess
import time

import pytest

import proxy
import status_endpoint

INTERVAL = status_endpoint.SNAPSHOT_INTERVAL_S
WINDOW = 300.0  # the idle window the owner asked for: five minutes, asserted below
CONFIG = {"backends": {}}


class FakeClock:
    """Time moves only when the refresher sleeps, so a schedule of minutes runs in a few passes of the loop."""

    def __init__(self) -> None:
        self.now = 1_000.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(status_endpoint, "_now", lambda: fake.now, raising=False)
    monkeypatch.setattr(status_endpoint, "_sleep", fake.sleep, raising=False)
    return fake


@pytest.fixture
def snapshot_state(monkeypatch):
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT", {})
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT_TASK", None)
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT_BUILD", None, raising=False)
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT_WANTED_UNTIL", 0.0, raising=False)
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT_CLOSED", False, raising=False)
    monkeypatch.setattr(status_endpoint, "_SECTION_AT", {})
    monkeypatch.setattr(proxy, "config", {"backends": {}})


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario, timeout=30))


async def passes(count):
    for _ in range(count):
        await asyncio.sleep(0)


async def until(condition, limit=100_000):
    for _ in range(limit):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


def test_the_refresh_interval_is_fifteen_seconds_and_the_idle_window_five_minutes():
    assert status_endpoint.SNAPSHOT_INTERVAL_S == 15.0
    assert status_endpoint.SNAPSHOT_IDLE_WINDOW_S == WINDOW


def test_a_reader_is_answered_at_once_while_the_first_build_is_still_running(snapshot_state):
    never = asyncio.Event()

    async def blocked_build():
        await never.wait()
        return {}

    async def scenario():
        status_endpoint.open_snapshot(blocked_build)
        payload = await asyncio.wait_for(proxy._status_payload_with_availability(), timeout=5)
        await status_endpoint.stop_snapshot()
        return payload

    payload = asyncio.run(scenario())

    assert payload["checking"] is True
    assert payload["snapshot"] == {"built_at": None, "age_s": None, "section_age_s": {}}


def test_a_reader_never_waits_for_a_probe(clock, snapshot_state):
    """The read takes the stored snapshot at once, even when the refresh it starts is blocked."""
    never = asyncio.Event()
    started: list[int] = []

    async def blocked_build():
        started.append(1)
        await never.wait()
        return {}

    status_endpoint._store_snapshot({"generated_at": 5, "backends": [{"key": "chat", "status": "up"}]})

    async def scenario():
        status_endpoint.open_snapshot(blocked_build)
        payload = await asyncio.wait_for(proxy._status_payload_with_availability(), timeout=5)
        await until(lambda: started)
        await status_endpoint.stop_snapshot()
        return payload

    payload = run(scenario())

    assert payload["generated_at"] == 5
    assert payload["backends"] == [{"key": "chat", "status": "up"}]


def test_each_section_reports_its_age_in_the_snapshot(snapshot_state):
    now = time.monotonic()
    status_endpoint._SECTION_AT.update({"backends": now - 2.0, "gpus": now - 40.0})
    status_endpoint._store_snapshot({"generated_at": 1})

    payload = asyncio.run(status_endpoint.get_cached_status({"backends": {}}))

    ages = payload["snapshot"]["section_age_s"]
    assert set(ages) == {"backends", "gpus"}
    assert 0 <= ages["backends"] < ages["gpus"], "a section that answered earlier is older"
    assert payload["cache_age_s"] == payload["snapshot"]["age_s"]


def test_no_build_runs_while_nobody_reads(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {}

    async def scenario():
        status_endpoint.open_snapshot(build)
        clock.now += 3600
        await passes(50)
        await status_endpoint.stop_snapshot()

    run(scenario())

    assert built == []


def test_a_read_starts_the_refresher_and_the_first_build_runs_after_the_read_returns(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {"generated_at": 1}

    async def scenario():
        status_endpoint.open_snapshot(build)
        start = clock.now
        payload = await status_endpoint.get_cached_status(CONFIG)
        assert built == [], "the read must not wait for the build"
        await until(lambda: built)
        await status_endpoint.stop_snapshot()
        return start, payload

    start, payload = run(scenario())

    assert payload["checking"] is True
    assert built == [start]


def test_the_refresher_builds_each_interval_and_stops_once_the_idle_window_has_passed(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {"generated_at": int(clock.now)}

    async def scenario():
        status_endpoint.open_snapshot(build)
        start = clock.now
        await status_endpoint.get_cached_status(CONFIG)
        task = status_endpoint._SNAPSHOT_TASK
        await task
        return start, task

    start, task = run(scenario())

    assert task.done()
    assert built == [start + INTERVAL * i for i in range(int(WINDOW // INTERVAL))]


def test_a_read_inside_the_window_keeps_the_refresher_going(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {"generated_at": int(clock.now)}

    async def scenario():
        status_endpoint.open_snapshot(build)
        start = clock.now
        await status_endpoint.get_cached_status(CONFIG)
        task = status_endpoint._SNAPSHOT_TASK
        reads_at = [start + 240, start + 480]
        while not task.done():
            if reads_at and clock.now >= reads_at[0]:
                reads_at.pop(0)
                await status_endpoint.get_cached_status(CONFIG)
            await asyncio.sleep(0)
        return start

    start = run(scenario())

    # The last read was at start + 480, so the window runs to start + 780. The last build is the one before that.
    assert built[-1] == start + 765


def test_a_read_after_the_refresher_stopped_gets_the_stale_snapshot_and_its_age_at_once(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {"generated_at": int(clock.now)}

    async def scenario():
        status_endpoint.open_snapshot(build)
        await status_endpoint.get_cached_status(CONFIG)
        await status_endpoint._SNAPSHOT_TASK
        clock.now += 1_000
        builds_before = len(built)
        now = clock.now
        payload = await asyncio.wait_for(status_endpoint.get_cached_status(CONFIG), timeout=5)
        builds_at_return = len(built)
        await passes(1)
        builds_after_one_pass = len(built)
        await status_endpoint.stop_snapshot()
        return payload, now, builds_before, builds_at_return, builds_after_one_pass

    payload, now, before, at_return, after_one_pass = run(scenario())

    last_built = built[before - 1]
    assert at_return == before, "the read returned only after a build ran"
    assert payload["generated_at"] == int(last_built)
    assert payload["snapshot"]["age_s"] == now - last_built
    assert after_one_pass == before + 1, "the read after the stop started the refresher again"


def test_concurrent_reads_start_one_refresher(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {"generated_at": 1}

    async def scenario():
        status_endpoint.open_snapshot(build)
        start = clock.now
        await asyncio.gather(*(status_endpoint.get_cached_status(CONFIG) for _ in range(20)))
        await status_endpoint._SNAPSHOT_TASK
        return start

    start = run(scenario())

    assert built == [start + INTERVAL * i for i in range(int(WINDOW // INTERVAL))], "one build per interval, not per read"


def test_an_idle_gateway_spawns_no_status_process(clock, snapshot_state, monkeypatch):
    spawned: list[list[str]] = []

    class RecordingPopen:
        """Records each start and refuses it: the test counts process starts and runs none."""

        def __init__(self, args, *popen_args, **popen_kwargs):
            spawned.append(list(args))
            raise OSError("process starts are recorded, not run, in this test")

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)

    async def build():
        try:
            subprocess.run(["systemctl", "--user", "show", "chat.service"], capture_output=True, timeout=1)
        except OSError:
            pass
        return {"generated_at": int(clock.now)}

    async def scenario():
        status_endpoint.open_snapshot(build)
        await status_endpoint.get_cached_status(CONFIG)
        await status_endpoint._SNAPSHOT_TASK
        during_window = len(spawned)
        clock.now += 3600
        await passes(50)
        await status_endpoint.stop_snapshot()
        return during_window

    during_window = run(scenario())

    assert during_window == WINDOW // INTERVAL, "the refresh starts a process each build (a positive control)"
    assert len(spawned) == during_window, "an idle gateway started a status process"


def test_shutdown_cancels_the_refresher_joins_it_and_no_later_read_restarts_it(clock, snapshot_state):
    built: list[float] = []

    async def build():
        built.append(clock.now)
        return {"generated_at": 1}

    async def scenario():
        status_endpoint.open_snapshot(build)
        await status_endpoint.get_cached_status(CONFIG)
        task = status_endpoint._SNAPSHOT_TASK
        await until(lambda: len(built) >= 2)
        await status_endpoint.stop_snapshot()
        done_at_return = task.done()
        stopped_at = len(built)
        await status_endpoint.get_cached_status(CONFIG)
        await passes(20)
        return done_at_return, stopped_at, len(built), status_endpoint._SNAPSHOT_TASK

    done, stopped_at, built_after, task_after = run(scenario())

    assert done, "shutdown returned before the refresher ended"
    assert built_after == stopped_at, "a read after shutdown started a build"
    assert task_after is None


def test_shutdown_cancels_a_running_build_and_waits_for_it_to_end(clock, snapshot_state):
    entered = asyncio.Event()
    outcome: list[str] = []

    async def build():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        return {}

    async def scenario():
        status_endpoint.open_snapshot(build)
        await status_endpoint.get_cached_status(CONFIG)
        task = status_endpoint._SNAPSHOT_TASK
        await asyncio.wait_for(entered.wait(), timeout=5)
        await status_endpoint.stop_snapshot()
        return task

    task = run(scenario())

    assert outcome == ["cancelled"]
    assert task.done(), "shutdown returned before the build ended"
    assert status_endpoint._SNAPSHOT_TASK is None


def test_shutdown_with_no_build_running_is_a_no_op(snapshot_state):
    asyncio.run(status_endpoint.stop_snapshot())

    assert status_endpoint._SNAPSHOT_TASK is None


def test_a_failed_build_keeps_the_last_snapshot_and_logs_only_the_error_type(clock, snapshot_state, caplog):
    status_endpoint._store_snapshot({"generated_at": 7})
    failures: list[float] = []

    async def build():
        failures.append(clock.now)
        raise OSError("cannot read /var/private/secret-path")

    async def scenario():
        with caplog.at_level(logging.ERROR, logger=status_endpoint.__name__):
            status_endpoint.open_snapshot(build)
            await status_endpoint.get_cached_status(CONFIG)
            await status_endpoint._SNAPSHOT_TASK

    run(scenario())

    assert len(failures) == WINDOW // INTERVAL
    assert status_endpoint._SNAPSHOT["payload"]["generated_at"] == 7
    assert "OSError" in caplog.text
    assert "secret-path" not in caplog.text, "the message can carry a path or a secret; only the type is logged"


def test_availability_does_not_trust_a_snapshot_older_than_the_freshness_bound(clock, snapshot_state):
    status_endpoint._store_snapshot({"generated_at": 1, "backends": [{"key": "chat", "status": "up"}]})

    fresh = dict(status_endpoint.latest_payload())
    clock.now += status_endpoint.SNAPSHOT_FRESH_S + 1
    stale = dict(status_endpoint.latest_payload())

    assert fresh["backends"] == [{"key": "chat", "status": "up"}]
    assert stale == {}, "a row that has not been refreshed is probed, not trusted"


def test_the_lifespan_opens_the_snapshot_and_its_sampler_before_serving_and_joins_both_after():
    """Checked on the source, not by running the lifespan: its startup probes ComfyUI, which the hermetic sandbox
    refuses. The order is what matters: each opens before the app serves, and shutdown joins it."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(proxy.__file__).read_text(encoding="utf-8"))
    serve = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_serve")
    order: list[str] = []
    for statement in serve.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Yield):
                order.append("yield")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                order.append(node.func.attr)
    for opened, closed in (("open_snapshot", "stop_snapshot"), ("start_backend_samples", "stop_backend_samples")):
        assert order.index(opened) < order.index("yield") < order.index(closed), order
    assert "_stop_home_refreshes" not in order
