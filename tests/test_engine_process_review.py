"""Review fixes for the owned-process driver, switch transaction and control runtime."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.engine_process import ProcessDriver
from tests.test_engine_contract_runtime import free_port, process_service, run


class Probe:
    """Readiness observation whose answer the test controls."""

    def __init__(self, ready=True):
        self.ready = ready

    async def read(self, *_args):
        value = self.ready() if callable(self.ready) else self.ready
        return type("Fact", (), {"known": True, "value": value})()


def state_pid(tmp_path: Path) -> int:
    return json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"]


# -- finding 1: readiness failure never kills a live engine -------------------


def test_failed_probe_never_stops_an_engine_with_active_requests(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    svc["process"]["ready_timeout_s"] = 0.05
    probe = Probe()
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True, observations=probe)

    async def scenario():
        assert await driver.ensure_ready(svc, 1)
        pid = state_pid(tmp_path)
        driver.request_started(svc)
        probe.ready = False
        assert await driver.ensure_ready(svc, 1) is False
        assert await driver.status(svc) == "running"
        assert state_pid(tmp_path) == pid
        os.killpg(pid, 0)  # the stream's process group is still alive
        driver.request_finished(svc)
        probe.ready = True
        assert await driver.ensure_ready(svc, 1) is True
        assert state_pid(tmp_path) == pid

    try:
        run(scenario())
    finally:
        driver.stop(svc)


def test_unhealthy_live_engine_is_replaced_by_the_restart_policy_after_drain(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    svc["process"]["ready_timeout_s"] = 0.05
    holder = {}
    first = {}

    def ready():
        pid = json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"]
        first.setdefault("pid", pid)
        return pid != first["pid"]

    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True,
                           observations=Probe(lambda: True))
    holder["driver"] = driver

    async def scenario():
        assert await driver.ensure_ready(svc, 1)
        old = state_pid(tmp_path)
        first["pid"] = old
        driver._observations = Probe(ready)
        assert await driver.ensure_ready(svc, 1) is True
        assert state_pid(tmp_path) != old
        assert len(driver._starts["fixture"]) == 2

    try:
        run(scenario())
    finally:
        driver.stop(svc)


def test_client_disconnect_during_cold_start_keeps_engine_and_budget(tmp_path):
    svc = process_service(tmp_path, delay=0.4)
    svc["residency"]["idle_owner"] = "engine"
    svc["process"]["ready_timeout_s"] = 3
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        waiter = asyncio.create_task(driver.ensure_ready(svc, 3))
        await asyncio.sleep(0.15)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert await driver.status(svc) == "running"
        pid = state_pid(tmp_path)
        assert len(driver._starts["fixture"]) == 1
        assert await driver.ensure_ready(svc, 3) is True
        assert state_pid(tmp_path) == pid
        assert len(driver._starts["fixture"]) == 1

    try:
        run(scenario())
    finally:
        driver.stop(svc)
