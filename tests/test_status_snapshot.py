"""The owner's status page and Home read one background snapshot, so no request waits for a probe.

One full status build runs on a schedule, in the background, and each section has its own budget. Every reader
gets the latest snapshot, and each section in it carries its age. Shutdown cancels a build that is still running
and waits for it to end, as Home's refreshes did before. These tests use events rather than clocks: a build that
is blocked stays blocked until the test releases it. The timeouts only stop a hang from hanging the suite.
"""
import asyncio
import logging
import time

import pytest

import proxy
import status_endpoint


@pytest.fixture
def snapshot_state(monkeypatch):
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT", {})
    monkeypatch.setattr(status_endpoint, "_SNAPSHOT_TASK", None)
    monkeypatch.setattr(status_endpoint, "_SECTION_AT", {})
    monkeypatch.setattr(proxy, "config", {"backends": {}})


def test_a_reader_is_answered_at_once_while_the_first_build_is_still_running(snapshot_state):
    never = asyncio.Event()

    async def blocked_build():
        await never.wait()
        return {}

    async def scenario():
        status_endpoint.start_snapshot(blocked_build)
        payload = await asyncio.wait_for(proxy._status_payload_with_availability(), timeout=5)
        await status_endpoint.stop_snapshot()
        return payload

    payload = asyncio.run(scenario())

    assert payload["checking"] is True
    assert payload["snapshot"] == {"built_at": None, "age_s": None, "section_age_s": {}}


def test_readers_never_start_a_probe(monkeypatch, snapshot_state):
    """Once a snapshot exists, the status and Home reads take it as it is: no status build, no engine probe."""
    def probe_must_not_run(*args, **kwargs):
        raise AssertionError("a request started a probe")

    monkeypatch.setattr(status_endpoint, "build_status_payload", probe_must_not_run)
    monkeypatch.setattr(status_endpoint, "_backend_grid", probe_must_not_run)
    monkeypatch.setattr(proxy, "_models_catalog_health_evidence", probe_must_not_run)
    status_endpoint._store_snapshot({"generated_at": 5, "backends": [{"key": "chat", "status": "up"}]})

    payload = asyncio.run(proxy._status_payload_with_availability())

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


def test_the_build_repeats_on_the_schedule(monkeypatch, snapshot_state):
    monkeypatch.setattr(status_endpoint, "SNAPSHOT_INTERVAL_S", 0.001)
    built: list[int] = []

    async def build():
        built.append(len(built) + 1)
        return {"generated_at": len(built)}

    async def scenario():
        status_endpoint.start_snapshot(build)
        while len(built) < 3:
            await asyncio.sleep(0.001)
        await status_endpoint.stop_snapshot()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))

    assert status_endpoint._SNAPSHOT["payload"]["generated_at"] >= 3


def test_shutdown_cancels_a_running_build_and_waits_for_it_to_end(snapshot_state):
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
        status_endpoint.start_snapshot(build)
        task = status_endpoint._SNAPSHOT_TASK
        await asyncio.wait_for(entered.wait(), timeout=5)
        await status_endpoint.stop_snapshot()
        return task

    task = asyncio.run(scenario())

    assert outcome == ["cancelled"]
    assert task.done(), "shutdown returned before the build ended"
    assert status_endpoint._SNAPSHOT_TASK is None


def test_shutdown_with_no_build_running_is_a_no_op(snapshot_state):
    asyncio.run(status_endpoint.stop_snapshot())

    assert status_endpoint._SNAPSHOT_TASK is None


def test_a_failed_build_keeps_the_last_snapshot_and_logs_only_the_error_type(monkeypatch, snapshot_state, caplog):
    monkeypatch.setattr(status_endpoint, "SNAPSHOT_INTERVAL_S", 0.001)
    status_endpoint._store_snapshot({"generated_at": 7})
    failures: list[int] = []

    async def build():
        failures.append(1)
        raise OSError("cannot read /var/private/secret-path")

    async def scenario():
        with caplog.at_level(logging.ERROR, logger=status_endpoint.__name__):
            status_endpoint.start_snapshot(build)
            while len(failures) < 2:
                await asyncio.sleep(0.001)
            await status_endpoint.stop_snapshot()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))

    assert status_endpoint._SNAPSHOT["payload"]["generated_at"] == 7
    assert "OSError" in caplog.text
    assert "secret-path" not in caplog.text, "the message can carry a path or a secret; only the type is logged"


def test_the_app_starts_the_snapshot_at_startup_and_joins_it_at_shutdown(monkeypatch, snapshot_state):
    """The lifespan starts the background build before it serves, and at shutdown cancels it and waits for it to end."""
    from fastapi.testclient import TestClient

    built: list[int] = []

    async def build():
        built.append(1)
        return {"generated_at": 1}

    monkeypatch.setattr(proxy, "_build_status_snapshot", build)
    with TestClient(proxy.app):
        deadline = time.monotonic() + 10  # a hang guard, not a bound
        while not built and time.monotonic() < deadline:
            time.sleep(0.01)
        task = status_endpoint._SNAPSHOT_TASK
        assert built and task is not None and not task.done()

    assert task.done(), "shutdown returned before the snapshot build ended"
    assert status_endpoint._SNAPSHOT_TASK is None
