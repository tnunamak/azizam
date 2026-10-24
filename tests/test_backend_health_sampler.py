"""The 24-hour backend history is sampled whether or not anyone reads, by one light sampler of its own.

The sampler probes each HTTP backend's health URL, with the probe the status grid uses, and records the history from
those rows. It starts no process. The grid's systemctl batch stays with the snapshot, which runs only while it is
read. A sample is taken at once when the gateway starts, then every minute, and shutdown joins the sampler.
"""
import asyncio
import subprocess
import threading
import unittest

import pytest

import status_endpoint
import test_status_build as _base
from gateway import lifecycle_drivers

CONFIG = {
    "backends": {
        "chat": {"health_endpoint": "http://127.0.0.1:9/health", "role": "required"},
        "speech": {"health_endpoint": "http://127.0.0.1:9/speech", "role": "optional", "residency": "on_demand"},
        "unit": {"lifecycle": {"driver": "systemd", "unit": "example.service"}, "role": "optional"},
    }
}


@pytest.fixture
def spawns(monkeypatch):
    started: list[list[str]] = []

    class RecordingPopen:
        """Records each start and refuses it: the test counts process starts and runs none."""

        def __init__(self, args, *popen_args, **popen_kwargs):
            started.append(list(args))
            raise OSError("process starts are recorded, not run, in this test")

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return started


def test_the_sampler_probes_the_health_urls_and_starts_no_process(monkeypatch, spawns):
    probed: list[str] = []
    recorded: list[list[dict]] = []

    async def fake_probe(client, key, name, url, role, note, service=None):
        probed.append(key)
        return {"key": key, "name": name, "role": role, "status": "up", "live_status": "up",
                "latency_ms": 4, "last_error": None}

    async def no_lifecycle_probe(*args, **kwargs):
        raise AssertionError("the sampler asked a lifecycle driver")

    monkeypatch.setattr(status_endpoint, "_probe", fake_probe)
    monkeypatch.setattr(status_endpoint, "_record_backend_health_samples", recorded.append)
    monkeypatch.setattr(lifecycle_drivers.SystemdDriver, "statuses", no_lifecycle_probe)

    asyncio.run(asyncio.wait_for(status_endpoint._sample_backend_health(CONFIG), timeout=30))

    assert probed == ["chat", "speech"], "the HTTP backends, and not the unit that has no health URL"
    assert [row["key"] for row in recorded[0]] == ["chat", "speech"]
    assert spawns == []


def test_the_sampler_samples_once_a_minute_and_joins_at_shutdown(monkeypatch, spawns):
    sampled: list[float] = []
    sleeps: list[float] = []
    now = [1_000.0]

    async def fake_sample(config):
        sampled.append(now[0])

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(status_endpoint, "_sample_backend_health", fake_sample)
    monkeypatch.setattr(status_endpoint, "_sleep", fake_sleep, raising=False)

    async def scenario():
        status_endpoint.start_backend_samples(lambda: CONFIG)
        task = status_endpoint._SAMPLE_TASK
        while len(sampled) < 3:
            await asyncio.sleep(0)
        await status_endpoint.stop_backend_samples()
        return task

    task = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert status_endpoint.BACKEND_SAMPLE_INTERVAL_S == 60.0
    assert sampled == [1_000.0, 1_060.0, 1_120.0], "one sample at start, then one a minute"
    assert set(sleeps) == {60.0}
    assert task.done()
    assert status_endpoint._SAMPLE_TASK is None
    assert spawns == []


@pytest.fixture
def wedged_write(monkeypatch):
    """A sample write that enters, then holds until the test releases it. Each write it finishes is recorded."""
    entered = threading.Event()
    release = threading.Event()
    finished: list[list[dict]] = []

    def write(rows):
        entered.set()
        release.wait(timeout=10)
        finished.append(rows)

    async def fake_probe(client, key, name, url, role, note, service=None):
        return {"key": key, "name": name, "role": role, "status": "up", "live_status": "up",
                "latency_ms": 4, "last_error": None}

    monkeypatch.setattr(status_endpoint, "_probe", fake_probe)
    monkeypatch.setattr(status_endpoint, "_record_backend_health_samples", write)
    yield entered, release, finished
    release.set()


def test_shutdown_waits_for_a_sample_write_that_is_already_running(spawns, wedged_write):
    """A write that has started cannot be cancelled. Shutdown waits for it, so no sample is written after shutdown returns."""
    entered, release, finished = wedged_write

    async def scenario():
        status_endpoint.start_backend_samples(lambda: CONFIG)
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), timeout=15)
        stop = asyncio.ensure_future(status_endpoint.stop_backend_samples())
        await asyncio.sleep(0.05)
        returned_while_writing = stop.done()
        release.set()
        await asyncio.wait_for(stop, timeout=10)
        written_at_return = len(finished)
        await asyncio.sleep(0.05)
        return returned_while_writing, written_at_return, len(finished)

    returned_while_writing, written_at_return, written_later = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert not returned_while_writing, "shutdown returned while a sample write was still running"
    assert written_at_return == 1, "the running write should have finished before shutdown returned"
    assert written_later == written_at_return, "a sample was written after shutdown returned"


def test_shutdown_stops_waiting_for_a_wedged_write_after_its_bound_and_says_so(spawns, wedged_write, monkeypatch, caplog):
    entered, _release, _finished = wedged_write
    monkeypatch.setattr(status_endpoint, "BACKEND_SAMPLE_JOIN_S", 0.05, raising=False)

    async def scenario():
        status_endpoint.start_backend_samples(lambda: CONFIG)
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), timeout=15)
        await asyncio.wait_for(status_endpoint.stop_backend_samples(), timeout=10)

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert "still running" in caplog.text, "shutdown should say that the write outlived its bound"
    assert status_endpoint._SAMPLE_WRITE is None, "shutdown should drop the handle of the write it stopped waiting for"


class StatusBuildLeavesBackendSamplesToTheSampler(unittest.IsolatedAsyncioTestCase):
    """The build reads the sampler's samples and writes none. Its section isolation comes from the build tests."""

    setUp = _base.StatusBuildTests.setUp
    isolate_sections = _base.StatusBuildTests.isolate_sections

    async def test_the_status_build_does_not_write_backend_samples(self):
        self.isolate_sections()

        payload = await status_endpoint.build_status_payload({})

        self.assertTrue(payload["generated_at"])
        status_endpoint._record_backend_health_samples.assert_not_called()
