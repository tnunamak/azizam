"""The 24-hour backend history is sampled whether or not anyone reads, by one light sampler of its own.

The sampler probes each HTTP backend's health URL, with the probe the status grid uses, and records the history from
those rows. It starts no process. The grid's systemctl batch stays with the snapshot, which runs only while it is
read. A sample is taken at once when the gateway starts, then every minute, and shutdown joins the sampler.
"""
import asyncio
import subprocess

import pytest

import status_endpoint
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


def test_the_status_build_leaves_the_backend_samples_to_the_sampler(monkeypatch):
    written: list[object] = []
    monkeypatch.setattr(status_endpoint, "_record_backend_health_samples", written.append)

    payload = asyncio.run(asyncio.wait_for(status_endpoint.build_status_payload({"backends": {}}), timeout=60))

    assert payload["generated_at"]
    assert written == [], "a build wrote backend samples too, so they would come at uneven times"
