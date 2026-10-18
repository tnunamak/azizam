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


# -- finding 2: a state file is a claim, not proof ---------------------------


def _write_raw_state(state_dir: Path, ident: str, payload: dict) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / f"{ident}.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def _start_tick(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rfind(")") + 2:].split()[19]


@pytest.mark.parametrize("payload", [
    {"pid": 0, "start_time": None},
    {"pid": 0},
    {"pid": 2 ** 22 + 12345, "start_time": None},  # ghost pid: /proc has nothing to compare
    {"pid": True, "start_time": "1"},
    {"pid": -1, "start_time": "1"},
    {"pid": 1, "start_time": "1"},
    {"pid": 12345, "start_time": 7},
])
def test_untrustworthy_state_never_signals_anything(tmp_path, payload):
    svc = process_service(tmp_path)
    _write_raw_state(tmp_path / "state", "fixture", payload)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    signalled = []
    with patch("gateway.engine_process.os.killpg", side_effect=lambda *a: signalled.append(a)), \
         patch("gateway.engine_process.os.kill", side_effect=lambda *a: signalled.append(a)):
        driver.stop(svc)
        driver.recover({"fixture": svc})
        assert run(driver.status(svc)) == "stopped"
    assert signalled == []


def test_state_naming_the_gateways_own_process_group_is_refused(tmp_path):
    svc = process_service(tmp_path)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    signalled = []
    for pid in {os.getpid(), os.getpgrp()}:
        _write_raw_state(tmp_path / "state", "fixture", {"pid": pid, "start_time": _start_tick(pid)})
        with patch("gateway.engine_process.os.killpg", side_effect=lambda *a: signalled.append(a)):
            driver.stop(svc)
            assert run(driver.status(svc)) == "stopped"
    assert signalled == []


def test_state_naming_a_live_unrelated_process_leader_is_not_ours(tmp_path):
    svc = process_service(tmp_path)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                start_new_session=True)
    try:
        _write_raw_state(tmp_path / "state", "fixture", {
            "pid": stranger.pid, "start_time": _start_tick(stranger.pid),
            "port": svc["process"]["port"]["fixed"], "argv_sha256": "0" * 64})
        driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
        driver.stop(svc)
        driver.recover({"fixture": svc})
        assert stranger.poll() is None, "a process whose command line is not ours was signalled"
    finally:
        os.killpg(stranger.pid, signal.SIGKILL)
        stranger.wait()


def test_state_directory_and_files_are_private(tmp_path):
    svc = process_service(tmp_path)
    svc["process"]["env"] = {"TEST_SECRET": "log-me"}
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)
    state_dir.chmod(0o755)
    driver = ProcessDriver(state_dir=state_dir, commands_allowed=lambda: True)

    async def scenario():
        assert await driver.ensure_ready(svc, 2)
        await asyncio.sleep(0.1)

    try:
        run(scenario())
        assert state_dir.stat().st_mode & 0o777 == 0o700
        files = list(state_dir.iterdir())
        assert files and all(f.stat().st_mode & 0o777 == 0o600 for f in files), files
    finally:
        driver.stop(svc)


def test_state_directory_that_is_a_symlink_is_refused(tmp_path):
    svc = process_service(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "state").symlink_to(elsewhere)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    assert run(driver.ensure_ready(svc, 0.2)) is False
    assert list(elsewhere.iterdir()) == []


def test_group_alive_never_signals_group_zero_or_own_group():
    calls = []
    with patch("gateway.engine_process.os.killpg", side_effect=lambda *a: calls.append(a)):
        assert ProcessDriver._group_alive(0) is False
        assert ProcessDriver._group_alive(os.getpgrp()) is False
    assert calls == []
