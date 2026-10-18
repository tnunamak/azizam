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
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)


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


def test_load_is_skipped_when_the_engine_already_reports_the_model_resident():
    from gateway.engine_residency import ResidencyController

    calls = []

    async def invoke(op, _args):
        calls.append(op)

    async def observe(name):
        return True if name == "resident" else None

    controller = ResidencyController(invoke=invoke, observe=observe)
    assert run(controller.load({"residency": {"load": {"op": "load"}}})) is True
    assert calls == []


# -- finding 5: the permission lives in the live admin database ---------------


def test_command_permission_is_read_from_the_bound_live_admin_database(tmp_path):
    from gateway import admin_store, host_commands

    live = tmp_path / "live-admin.db"
    admin_store.init_db(live)
    admin_store.put_operator_setting_with_audit(
        "allow_host_commands", {"enabled": True}, updated_by="owner",
        audit_action="operator.host_commands.update", audit_actor_kind="owner", audit_actor_id="owner",
        audit_actor_label="owner", audit_target_kind="operator_setting",
        audit_target_id="allow_host_commands", path=live)
    other = tmp_path / "other-admin.db"
    admin_store.init_db(other)
    try:
        # Another database without the setting answers no; the shared default
        # store is not used here, because other tests in the worker write to it.
        host_commands.bind_admin_db(other)
        assert host_commands.commands_allowed() is False
        host_commands.bind_admin_db(live)
        assert host_commands.commands_allowed() is True
        from gateway.engine_process import _operator_allows_commands
        assert _operator_allows_commands() is True
    finally:
        host_commands.bind_admin_db(None)


def test_proxy_binds_its_admin_database_for_the_command_permission():
    source = (Path(__file__).resolve().parent.parent / "proxy.py").read_text()
    assert "host_commands.bind_admin_db(ADMIN_DB_PATH)" in source


# -- finding 6: D13 migration, off-switch behaviour, driver resolution --------

_LEGACY = {
    "engine-a": {"lifecycle": {"driver": "systemd", "driver_config": {"unit": "a.service"}}},
    "engine-b": {"config": {"lifecycle": {"driver": "systemd"}}, "lifecycle": {"driver": "external"}},
    "engine-c": {"lifecycle": {"driver": "command", "driver_config": {"start": "run-c"}}},
    "engine-d": {"process": {"driver": "process", "start": ["run-d"]}},
    "engine-e": {"lifecycle": {"driver": "external"}},
    "engine-f": {},
}


def _audit(path):
    from gateway import admin_store

    return admin_store.list_audit(action_prefix="operator.host_commands", path=path)


def test_migration_enables_commands_once_for_existing_lifecycles_and_audits_them(tmp_path):
    from gateway import admin_store, host_commands

    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    found = host_commands.migrate_legacy_command_lifecycles(path, _LEGACY)
    assert sorted(item["id"] for item in found) == ["engine-a", "engine-b", "engine-c", "engine-d"]
    setting = admin_store.get_operator_setting("allow_host_commands", path=path)
    assert setting["value"] == {"enabled": True}
    events = _audit(path)
    assert len(events) == 1
    listed = {item["id"]: item["driver"] for item in events[0]["metadata"]["lifecycles"]}
    assert listed == {"engine-a": "systemd", "engine-b": "systemd", "engine-c": "command",
                      "engine-d": "process"}
    # The owner turns it off; the migration never runs a second time.
    admin_store.put_operator_setting_with_audit(
        "allow_host_commands", {"enabled": False}, updated_by="owner",
        audit_action="operator.host_commands.update", audit_actor_kind="owner", audit_actor_id="owner",
        audit_actor_label="owner", audit_target_kind="operator_setting",
        audit_target_id="allow_host_commands", path=path)
    assert host_commands.migrate_legacy_command_lifecycles(path, _LEGACY) == []
    assert admin_store.get_operator_setting("allow_host_commands", path=path)["value"] == {"enabled": False}
    assert len(_audit(path)) == 2


def test_new_install_keeps_commands_off_even_if_a_command_engine_is_added_later(tmp_path):
    from gateway import admin_store, host_commands

    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    assert host_commands.migrate_legacy_command_lifecycles(path, {"engine-e": _LEGACY["engine-e"]}) == []
    assert admin_store.get_operator_setting("allow_host_commands", path=path) is None
    assert host_commands.migrate_legacy_command_lifecycles(path, _LEGACY) == []
    assert admin_store.get_operator_setting("allow_host_commands", path=path) is None
    assert _audit(path) == []


def test_turning_commands_off_blocks_new_starts_but_leaves_a_running_engine_alone(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    allowed = {"on": True}
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: allowed["on"])

    async def scenario():
        assert await driver.ensure_ready(svc, 2)
        pid = state_pid(tmp_path)
        allowed["on"] = False
        assert await driver.status(svc) == "running"
        assert await driver.ensure_ready(svc, 2) is True  # still served, never respawned
        assert state_pid(tmp_path) == pid
        driver.stop(svc)  # an explicit stop is still possible
        assert await driver.status(svc) == "stopped"
        assert await driver.ensure_ready(svc, 0.2) is False  # no new start while off
        assert not (tmp_path / "state" / "fixture.json").exists()

    try:
        run(scenario())
    finally:
        driver.stop(svc)


def test_disabling_the_setting_reports_that_running_processes_continue(gateway):
    from unittest.mock import patch as _patch

    owner, _, _, _admin = owner_and_admin(gateway)
    path = "/admin/api/operator/host-commands"
    on = owner.put(path, headers={**csrf_for(owner), "If-Match": "0"}, json={"enabled": True})
    assert on.status_code == 200, on.text
    with _patch("gateway.lifecycle_drivers._process_driver.stop") as stop, \
         _patch("gateway.lifecycle_drivers._process_driver.shutdown") as shutdown:
        off = owner.put(path, headers={**csrf_for(owner), "If-Match": on.json()["revision"]},
                        json={"enabled": False})
    assert off.status_code == 200, off.text
    body = off.json()
    assert body["enabled"] is False
    assert body["running_processes"] == "unchanged"
    assert "keep running" in body["note"]
    stop.assert_not_called()
    shutdown.assert_not_called()


def _record(**fields):
    base = {"id": "svc", "config": {}, "lifecycle": {
        "driver": "external", "driver_config": {}, "managed": False, "service_name": None, "type": None}}
    base.update(fields)
    return base


def _systemd(**extra):
    wrapper = {"driver": "systemd", "driver_config": {"unit": "unit.service"}, "managed": True,
               "service_name": "unit.service", "type": "systemd"}
    return _record(lifecycle=wrapper, **extra)


_PROCESS_MAPPING = {"start": ["run"], "port": {"fixed": 8080}}

# Every live lifecycle shape from the reality audit (section 4) plus the v2 shapes.
# The registry projection carries ``config`` and the lifecycle wrapper onto the
# flat backend, so a v2 ``process`` mapping lives in ``config``; the flat shapes
# (``flat=True``) are the backend dicts the driver factory also accepts directly.
_SHAPES = {
    "systemd wrapper plus nested copy (13 live)": (
        _systemd(config={"lifecycle": {"driver": "systemd", "driver_config": {"unit": "unit.service"}}}),
        "systemd", False),
    "systemd wrapper only": (_systemd(), "systemd", False),
    "external wrapper without nested copy (7 live)": (_record(), "external", False),
    "no lifecycle at all": ({"id": "svc", "config": {}}, "external", False),
    "empty driver string": (_record(lifecycle={"driver": ""}), "external", False),
    "unknown driver": (_record(lifecycle={"driver": "mystery"}), "external", False),
    "legacy command string": (
        _record(lifecycle={"driver": "command", "driver_config": {"start": "run"}}), "command", False),
    "docker": (_record(lifecycle={"driver": "docker", "driver_config": {"container": "c"}}), "docker", False),
    "http-load-unload": (_record(lifecycle={"driver": "http-load-unload"}), "http-load-unload", False),
    "systemd plus process mapping without a driver (config)": (
        _systemd(config={"process": dict(_PROCESS_MAPPING)}), "systemd", False),
    "systemd plus process driver none (config)": (
        _systemd(config={"process": {"driver": "none", **_PROCESS_MAPPING}}), "systemd", False),
    "systemd plus blank process driver (config)": (
        _systemd(config={"process": {"driver": "", **_PROCESS_MAPPING}}), "systemd", False),
    "v2 process driver wins over systemd (config)": (
        _systemd(config={"process": {"driver": "process", **_PROCESS_MAPPING}}), "process", False),
    "v2 command driver (config)": (
        _record(config={"process": {"driver": "command", "start": ["run"]}}), "command", False),
    "v2 process alone (config)": (
        _record(config={"process": {"driver": "process", **_PROCESS_MAPPING}}), "process", False),
    "flat systemd plus top-level process mapping without a driver": (
        {"lifecycle": {"driver": "systemd"}, "process": dict(_PROCESS_MAPPING)}, "systemd", True),
    "flat v2 process driver": ({"process": {"driver": "process", **_PROCESS_MAPPING}}, "process", True),
    "flat systemd nested copy only": (
        {"config": {"lifecycle": {"driver": "systemd"}}, "process": dict(_PROCESS_MAPPING)}, "systemd", True),
}


@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_driver_resolution_keeps_every_live_lifecycle_shape(name):
    from gateway import lifecycle_drivers, service_registry

    record, expected, flat = _SHAPES[name]
    drivers = {
        "systemd": lifecycle_drivers._systemd_driver, "command": lifecycle_drivers._command_driver,
        "docker": lifecycle_drivers._docker_driver, "process": lifecycle_drivers._process_driver,
        "http-load-unload": lifecycle_drivers._http_load_unload_driver,
        "external": lifecycle_drivers._external_driver,
    }
    backend = record if flat else service_registry.runtime_backend_from_service(record)
    assert lifecycle_drivers.driver_for_service("svc", {"svc": backend}) is drivers[expected]


# -- finding 7: every path-like control argument stays under the allowlist ----

import httpx  # noqa: E402

from gateway.engine_control import EngineControlRuntime  # noqa: E402
from tests.support.fake_upstream.observation_client import observation_client  # noqa: E402


def _control(roots, calls=None, **operation):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content or b"{}"))
        return httpx.Response(204)

    service = {"connection": {"address": "http://fixture.test:18091"},
               "control_file_roots": roots,
               "protocol_operations": {"load": {"method": "POST", "path": "/load", **operation}}}
    return EngineControlRuntime(service, drain=SwapDrain(), lock_for=lambda _scope: asyncio.Lock(),
                                client_factory=observation_client(handler))


@pytest.mark.parametrize("value", [
    "../../etc/passwd", "/etc/passwd", "~/private/model.gguf", "./model.gguf", "file:///etc/passwd",
])
@pytest.mark.parametrize("shape", ["plain key", "nested list"])
def test_any_path_like_string_is_checked_whatever_its_key_is_called(tmp_path, value, shape):
    calls = []
    runtime = _control([str(tmp_path / "allowed")], calls)
    args = {"prompt": value} if shape == "plain key" else {"items": [{"x": [value]}]}
    with pytest.raises(ValueError, match="allowlisted"):
        run(runtime.invoke("load", args))
    assert calls == []


@pytest.mark.parametrize("model", ["org/model", "Qwen/Qwen3-8B"])
def test_org_slash_model_ids_pass_as_plain_ids(tmp_path, model):
    calls = []
    runtime = _control([str(tmp_path / "allowed")], calls)
    run(runtime.invoke("load", {"model": model, "layers": 3}))
    assert calls == [{"model": model, "layers": 3}]


def test_a_bare_name_is_an_identifier_even_when_a_file_of_that_name_is_here(tmp_path, monkeypatch):
    (tmp_path / "weights.bin").write_text("x")
    monkeypatch.chdir(tmp_path)
    calls = []
    runtime = _control([str(tmp_path / "allowed")], calls)
    run(runtime.invoke("load", {"name": "weights.bin"}))
    assert calls == [{"name": "weights.bin"}]


def test_an_absolute_path_outside_the_roots_is_refused(tmp_path):
    outside = tmp_path / "outside.gguf"
    outside.write_text("x")
    calls = []
    with pytest.raises(ValueError, match="allowlisted"):
        run(_control([str(tmp_path / "allowed")], calls).invoke("load", {"model_path": str(outside)}))
    assert calls == []


def test_a_parent_relative_path_is_refused(tmp_path):
    calls = []
    with pytest.raises(ValueError, match="allowlisted"):
        run(_control([str(tmp_path / "allowed")], calls).invoke("load", {"model_path": "../x"}))
    assert calls == []


@pytest.mark.parametrize("value", ["models/../../x", "org/..", "a\\..\\b"])
def test_an_undeclared_value_with_a_parent_segment_is_a_path_and_refused(tmp_path, value):
    calls = []
    with pytest.raises(ValueError, match="allowlisted"):
        run(_control([str(tmp_path / "allowed")], calls).invoke("load", {"model_path": value}))
    assert calls == []


@pytest.mark.parametrize("value", ["weights.gguf", "org/model"])
def test_a_relative_value_of_a_declared_path_argument_is_checked_and_refused(tmp_path, value):
    calls = []
    runtime = _control([str(tmp_path / "allowed")], calls, arguments={"model_path": {"type": "path"}})
    with pytest.raises(ValueError, match="allowlisted"):
        run(runtime.invoke("load", {"model_path": value}))
    assert calls == []


def test_a_declared_path_argument_inside_a_root_is_sent_resolved(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    weights = root / "m.gguf"
    weights.write_text("x")
    calls = []
    runtime = _control([str(root)], calls, arguments={"model_path": {"type": "path"}})
    run(runtime.invoke("load", {"model_path": str(weights), "model": "qwen"}))
    assert calls == [{"model_path": str(weights.resolve()), "model": "qwen"}]


@pytest.mark.parametrize("declaration", [{"type": "Path"}, {"type": "string"}, "path", {}])
def test_an_argument_declaration_other_than_type_path_is_refused(tmp_path, declaration):
    calls = []
    runtime = _control([str(tmp_path / "allowed")], calls, arguments={"model_path": declaration})
    with pytest.raises(ValueError, match="only type: path is supported"):
        run(runtime.invoke("load", {"model": "qwen"}))
    assert calls == []


def test_relative_allowlist_roots_are_refused(tmp_path):
    calls = []
    runtime = _control(["allowed/models"], calls)
    with pytest.raises(ValueError, match="control file roots"):
        run(runtime.invoke("load", {"model": "plain"}))
    assert calls == []


def test_allowed_paths_are_sent_resolved_and_plain_values_untouched(tmp_path):
    root = tmp_path / "allowed"
    (root / "real").mkdir(parents=True)
    weights = root / "real" / "m.gguf"
    weights.write_text("x")
    (root / "link.gguf").symlink_to(weights)
    calls = []
    runtime = _control([str(root)], calls)
    run(runtime.invoke("load", {"model": "qwen", "layers": 3, "model_path": str(root / "link.gguf"),
                                "extra": [str(weights), True, None]}))
    assert calls == [{"model": "qwen", "layers": 3, "model_path": str(weights.resolve()),
                      "extra": [str(weights.resolve()), True, None]}]


def test_symlink_inside_the_root_that_points_outside_is_refused(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret.gguf"
    outside.write_text("x")
    (root / "m.gguf").symlink_to(outside)
    calls = []
    with pytest.raises(ValueError, match="allowlisted"):
        run(_control([str(root)], calls).invoke("load", {"model": "x", "model_path": str(root / "m.gguf")}))
    assert calls == []


def test_a_symlink_swapped_in_after_the_check_is_refused_before_the_request(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "m.gguf"
    target.write_text("x")
    outside = tmp_path / "secret.gguf"
    outside.write_text("y")
    calls = []

    def swap_after_check(*_args, **_kwargs):
        target.unlink()
        target.symlink_to(outside)
        return {}

    runtime = _control([str(root)], calls)
    with patch("gateway.engine_control.credential_headers", side_effect=swap_after_check):
        with pytest.raises(ValueError, match="changed|allowlisted"):
            run(runtime.invoke("load", {"model_path": str(target)}))
    assert calls == []


def test_a_file_replaced_by_another_file_after_the_check_is_refused(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "m.gguf"
    target.write_text("x")
    calls = []

    def replace_after_check(*_args, **_kwargs):
        replacement = root / "other"
        replacement.write_text("z")
        os.replace(replacement, target)
        return {}

    runtime = _control([str(root)], calls)
    with patch("gateway.engine_control.credential_headers", side_effect=replace_after_check):
        with pytest.raises(ValueError, match="changed"):
            run(runtime.invoke("load", {"model_path": str(target)}))
    assert calls == []


# -- finding 9: the process, its output, and its state outlive the gateway ----


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def _ticker(tmp_path: Path) -> Path:
    return _script(tmp_path, "ticker.py", (
        "import sys, time\n"
        "marker = sys.argv[1]\n"
        "n = 0\n"
        "while True:\n"
        "    try:\n"
        "        print('tick', n, flush=True)\n"
        "    except BaseException as exc:\n"
        "        open(marker + '.err', 'w').write(repr(exc))\n"
        "        raise\n"
        "    open(marker, 'w').write(str(n))\n"
        "    n += 1\n"
        "    time.sleep(0.03)\n"))


def _owned(tmp_path: Path, script: Path, *args: str, **process) -> dict:
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    svc["process"]["start"] = [sys.executable, str(script), *args]
    svc["process"].update(process)
    return svc


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_adopted_engine_keeps_a_working_stdout_after_the_gateway_lets_go(tmp_path):
    marker = tmp_path / "ticks"
    svc = _owned(tmp_path, _ticker(tmp_path), str(marker))
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True,
                           observations=Probe())

    async def scenario():
        assert await driver.ensure_ready(svc, 2)
        child = driver._children["fixture"]
        for pipe in (child.stdout, child.stderr):  # what a gateway restart does to its end of a pipe
            if pipe:
                pipe.close()
        assert _wait_for(lambda: marker.exists() and marker.read_text() != "")
        before = int(marker.read_text())
        await asyncio.sleep(0.4)
        assert int(marker.read_text()) > before + 3
        assert not Path(str(marker) + ".err").exists(), Path(str(marker) + ".err").read_text()

    try:
        run(scenario())
    finally:
        driver.stop(svc)


def test_an_unwritable_log_does_not_stop_the_engine_from_starting(tmp_path):
    marker = tmp_path / "ticks"
    state = tmp_path / "state"
    (state).mkdir(mode=0o700)
    (state / "fixture.log").mkdir()  # the log path cannot be opened for writing
    svc = _owned(tmp_path, _ticker(tmp_path), str(marker))
    driver = ProcessDriver(state_dir=state, commands_allowed=lambda: True, observations=Probe())
    try:
        assert run(driver.ensure_ready(svc, 2)) is True
        assert _wait_for(lambda: marker.exists() and int(marker.read_text() or 0) > 2)
        assert not Path(str(marker) + ".err").exists()
    finally:
        driver.stop(svc)


def test_log_pump_drains_the_engine_even_when_every_write_fails(tmp_path):
    import io

    from gateway import engine_supervisor

    source = io.BytesIO(b"".join(b"line %d %s\n" % (n, b"x" * 200) for n in range(5000)))
    pump = engine_supervisor.LogPump(tmp_path / "missing-dir" / "engine.log", limit=4096, secrets=[])
    assert pump.run(source) == 5000  # every line consumed; the failing writes never block the reader


def test_secrets_in_argv_are_redacted_from_the_log(tmp_path):
    script = _script(tmp_path, "argv.py", "import sys, time\nprint(sys.argv, flush=True)\ntime.sleep(30)\n")
    svc = _owned(tmp_path, script, "--api-key", "sk-live-0123456789", "--token=tok-abcdef-9876",
                 "MODEL_PASSWORD=hunter2hunter2", "--model", "visible-model")
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True,
                           observations=Probe())
    log = tmp_path / "state" / "fixture.log"
    try:
        assert run(driver.ensure_ready(svc, 2))
        assert _wait_for(lambda: log.exists() and b"visible-model" in log.read_bytes())
        text = log.read_text()
        for secret in ("sk-live-0123456789", "tok-abcdef-9876", "hunter2hunter2"):
            assert secret not in text
        assert text.count("[redacted]") >= 3
    finally:
        driver.stop(svc)


def test_log_rotates_instead_of_truncating(tmp_path):
    script = _script(tmp_path, "chatty.py", (
        "import time\n"
        "for n in range(12):\n"
        "    print('entry %02d ' % n + 'x' * 200, flush=True)\n"
        "print('done', flush=True)\n"
        "time.sleep(30)\n"))
    svc = _owned(tmp_path, script, log_max_bytes=1024)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True,
                           observations=Probe())
    log = tmp_path / "state" / "fixture.log"
    rotated = tmp_path / "state" / "fixture.log.1"
    try:
        assert run(driver.ensure_ready(svc, 2))
        assert _wait_for(lambda: log.exists() and b"done" in log.read_bytes())
        assert rotated.exists(), "an over-limit log was truncated, not rotated"
        assert len(log.read_bytes()) <= 1024 and len(rotated.read_bytes()) <= 1024
        kept = (rotated.read_bytes() + log.read_bytes()).decode()
        assert "entry 11" in kept and "entry 10" in kept  # recent history survives the rollover
        assert all(f.stat().st_mode & 0o777 == 0o600 for f in (log, rotated))
    finally:
        driver.stop(svc)


def test_a_gateway_crash_right_after_spawn_leaves_a_state_file_for_the_orphan(tmp_path):
    real_popen = subprocess.Popen
    marker = tmp_path / "ticks"
    svc = _owned(tmp_path, _ticker(tmp_path), str(marker))

    class GatewayCrash(BaseException):
        pass

    def crash_after_spawn(*args, **kwargs):
        real_popen(*args, **kwargs)
        raise GatewayCrash

    first = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True, observations=Probe())
    with patch("gateway.engine_process.subprocess.Popen", side_effect=crash_after_spawn):
        with pytest.raises(GatewayCrash):
            run(first.ensure_ready(svc, 2))
    second = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True, observations=Probe())
    try:
        assert _wait_for(lambda: (tmp_path / "state" / "fixture.json").exists()), "orphan has no state"
        second.recover({"fixture": svc})
        assert run(second.status(svc)) == "running"
    finally:
        second.stop(svc)
    assert run(second.status(svc)) == "stopped"


@pytest.mark.parametrize("cwd", [["a", "list"], 7, {"path": "/"}, "bad\x00dir"])
def test_process_cwd_must_be_a_string_path(tmp_path, cwd):
    svc = process_service(tmp_path)
    svc["process"]["cwd"] = cwd
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    assert "cwd" in (run(driver.preflight(svc)) or "")
    assert run(driver.ensure_ready(svc, 0.2)) is False


def test_process_docs_explain_systemd_kill_mode():
    docs = (Path(__file__).resolve().parent.parent / "docs" / "design" / "ENGINE_PROCESS_DRIVER.md").read_text()
    assert "KillMode" in docs and "control-group" in docs
