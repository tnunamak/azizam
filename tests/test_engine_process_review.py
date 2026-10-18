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


# -- findings 3, 4 and 8: the switch transaction ------------------------------

from gateway.engine_switch import SwitchController, SwitchUnavailable  # noqa: E402
from gateway.swap_drain import SwapDrain  # noqa: E402

_SVC = {"id": "scope", "offerings": [{"id": "old"}, {"id": "new"}]}


def _selection(model="new", **switch):
    return {"mode": "switch", "switch": {"call": "select", "args": {"model": model},
            "observe_selected": "selected", "exclusive_scope": "scope", **switch}}


def _engine(selected, calls):
    async def invoke(op, args):
        calls.append((op, args))
        selected[0] = args["model"]

    async def observe(name):
        return selected[0] if name == "selected" else True

    return invoke, observe


def test_failed_preparation_frees_the_scope_when_on_release_raises():
    selected, calls = ["old"], []
    invoke, observe = _engine(selected, calls)
    lock = asyncio.Lock()
    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe,
                                  lock_for=lambda _scope: lock)

    def boom():
        raise RuntimeError("release hook failed")

    async def refuse():
        return False

    async def scenario():
        with pytest.raises(Exception):
            await controller.select(_SVC, "new", _selection(), on_acquired=lambda: None,
                                    on_release=boom, prepare=refuse)
        assert not lock.locked()
        lease = await controller.select(_SVC, "new", _selection(queue_wait_s=0.05))
        lease.release()

    run(scenario())


def test_raising_release_hook_still_frees_the_lease_and_scope():
    selected, calls = ["old"], []
    invoke, observe = _engine(selected, calls)
    lock = asyncio.Lock()
    drain = SwapDrain()
    controller = SwitchController(drain=drain, invoke=invoke, observe=observe,
                                  lock_for=lambda _scope: lock)

    def boom():
        raise RuntimeError("release hook failed")

    async def scenario():
        lease = await controller.select(_SVC, "new", _selection(), on_release=boom)
        with pytest.raises(RuntimeError):
            lease.release()
        assert not lock.locked()
        assert drain.in_flight("scope") == 0
        lease.release()  # idempotent after the failure
        again = await controller.select(_SVC, "old", _selection("old", queue_wait_s=0.05))
        again.release()

    run(scenario())


def test_idle_stop_value_is_validated_and_scheduling_never_raises(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_stop_s"] = "soon"
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    assert "idle_stop_s" in (run(driver.preflight(svc)) or "")
    assert run(driver.ensure_ready(svc, 0.2)) is False
    driver.request_started(svc)
    driver.request_finished(svc)  # no running loop, bad value: must not raise
    svc["residency"]["idle_stop_s"] = 5
    driver.request_started(svc)
    driver.request_finished(svc)  # no running loop: must not raise either


def test_requests_for_the_selected_offering_share_the_scope_without_switching():
    selected, calls = ["new"], []
    invoke, observe = _engine(selected, calls)
    lock = asyncio.Lock()
    drain = SwapDrain()
    controller = SwitchController(drain=drain, invoke=invoke, observe=observe,
                                  lock_for=lambda _scope: lock)

    async def scenario():
        first = await controller.select(_SVC, "new", _selection(queue_wait_s=0.05))
        second = await controller.select(_SVC, "new", _selection(queue_wait_s=0.05))
        assert calls == []
        assert drain.in_flight("scope") == 2
        assert not lock.locked()
        first.release()
        second.release()
        assert drain.in_flight("scope") == 0

    run(scenario())


def test_switch_waits_for_shared_leases_and_blocks_new_arrivals_while_it_drains():
    selected, calls = ["new"], []
    invoke, observe = _engine(selected, calls)
    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)

    async def scenario():
        shared = await controller.select(_SVC, "new", _selection())
        switching = asyncio.create_task(
            controller.select(_SVC, "old", _selection("old", drain_timeout_s=2)))
        await asyncio.sleep(0.05)
        assert not switching.done() and calls == []
        with pytest.raises(SwitchUnavailable):  # writer preference: no new shared admission
            await controller.select(_SVC, "new", _selection(queue_wait_s=0.02))
        shared.release()
        switched = await switching
        assert calls == [("select", {"model": "old"})]
        switched.release()

    run(scenario())


def test_selected_but_not_ready_offering_is_not_admitted_as_shared():
    selected, calls = ["new"], []

    async def invoke(op, args):
        calls.append((op, args))

    async def observe(name):
        return selected[0] if name == "selected" else False

    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    svc = {**_SVC, "observations": {"offering_ready": {"http": {"path": "/ready"}}}}
    with pytest.raises(SwitchUnavailable):
        run(controller.select(svc, "new", _selection()))
    assert calls == [("select", {"model": "new"})]


def test_restore_reapplies_the_recorded_previous_selection_whole():
    selected, calls = ["old"], []

    async def invoke(op, args):
        calls.append(args)
        if args.get("model") == "new":
            selected[0] = "wrong"
        else:
            selected[0] = args["model"]

    async def observe(name):
        return selected[0]

    old_args = {"model": "old", "options": {"alias": "old", "layers": [{"name": "old"}]}, "keep": "x"}
    svc = {"id": "scope", "offerings": [
        {"id": "old", "selection": _selection("old", args=old_args)},
        {"id": "new"}]}
    new_args = {"model": "new", "options": {"alias": "new", "layers": [{"name": "new"}]}, "keep": "x"}
    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    with pytest.raises(SwitchUnavailable):
        run(controller.select(svc, "new", _selection(args=new_args, on_failure="restore")))
    assert calls == [new_args, old_args]


def test_restore_without_a_recorded_selection_rewrites_nested_values():
    selected, calls = ["old"], []

    async def invoke(op, args):
        calls.append(args)
        selected[0] = "wrong" if args["model"] == "new" else args["model"]

    async def observe(name):
        return selected[0]

    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    args = {"model": "new", "options": {"alias": "new", "layers": [{"name": "new"}]}}
    with pytest.raises(SwitchUnavailable):
        run(controller.select(_SVC, "new", _selection(args=args, on_failure="restore")))
    assert calls[1] == {"model": "old", "options": {"alias": "old", "layers": [{"name": "old"}]}}


def test_restore_fails_closed_when_the_previous_selection_is_unknown():
    calls = []

    async def invoke(op, args):
        calls.append(args)

    async def observe(name):
        return None if name == "selected" else True

    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    with pytest.raises(SwitchUnavailable) as caught:
        run(controller.select(_SVC, "new", _selection(on_failure="restore")))
    assert "selection unknown" in str(caught.value.detail)
    assert calls == [{"model": "new"}]  # no guess at a restore


def test_cancellation_is_never_replaced_by_a_restore_failure():
    selected = ["old"]

    async def invoke(op, args):
        if args["model"] == "new":
            raise asyncio.CancelledError
        raise RuntimeError("restore failed")

    async def observe(name):
        return selected[0]

    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    with pytest.raises(asyncio.CancelledError):
        run(controller.select(_SVC, "new", _selection(on_failure="restore")))
