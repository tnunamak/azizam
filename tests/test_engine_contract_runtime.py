"""Contract v2.1 runtime cases. All process state stays in tmp_path."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest
import httpx

from gateway.engine_observations import ObservationReader
from gateway.engine_process import ProcessDriver
from gateway.engine_residency import ResidencyController
from gateway.engine_switch import SwitchController, SwitchUnavailable
from gateway.swap_drain import SwapDrain
from tests.support.fake_upstream.observation_client import observation_client


def run(coro):
    return asyncio.run(coro)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def engine_script(tmp_path: Path) -> Path:
    script = tmp_path / "engine.py"
    script.write_text(
        "import http.server, os, sys, time\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'{}')\n"
        " def log_message(self, *args): pass\n"
        "if os.environ.get('TEST_SECRET'): print(os.environ['TEST_SECRET'], flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()\n"
    )
    return script


def process_service(tmp_path: Path, *, delay: float = 0) -> dict:
    port = free_port()
    return {
        "id": "fixture",
        "connection": {"address": f"http://127.0.0.1:{port}"},
        "process": {
            "driver": "process",
            "start": [sys.executable, str(engine_script(tmp_path)), str(port), str(delay)],
            "port": {"fixed": port},
            "ready_timeout_s": 1,
            "stop_timeout_s": 0.2,
            "restart": {"max_attempts": 2, "window_s": 30, "backoff_s": 0.01},
        },
        "observations": {"process_ready": {"http": {"path": "/health", "expect": 200}}},
        "residency": {"idle_owner": "gateway", "idle_stop_s": 0.1},
    }


def test_process_argv_concurrent_start_and_bounded_stop(tmp_path):
    svc = process_service(tmp_path)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        try:
            assert await asyncio.gather(*(driver.ensure_ready(svc, 2) for _ in range(4))) == [True] * 4
            state = json.loads((tmp_path / "state" / "fixture.json").read_text())
            assert state["pid"] > 0 and state["start_time"]
            assert state["port"] == svc["process"]["port"]["fixed"]
            assert await driver.ensure_ready(svc, 2)
        finally:
            driver.stop(svc)
        assert await driver.status(svc) == "stopped"

    run(scenario())


def test_process_missing_probe_is_unknown_and_does_not_claim_ready(tmp_path):
    svc = process_service(tmp_path)
    svc.pop("observations")
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    try:
        assert run(driver.ensure_ready(svc, 0.2)) is False
        assert run(driver.health(svc)) == "unknown"
    finally:
        driver.stop(svc)


def test_process_readiness_timeout_and_disabled_command_gate(tmp_path):
    svc = process_service(tmp_path, delay=2)
    svc["process"]["ready_timeout_s"] = 0.1
    denied = ProcessDriver(state_dir=tmp_path / "denied", commands_allowed=lambda: False)
    assert run(denied.ensure_ready(svc, 0.1)) is False
    assert not (tmp_path / "denied").exists()
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    assert run(driver.ensure_ready(svc, 0.1)) is False
    assert run(driver.status(svc)) == "stopped"


def test_process_idle_stop_only_with_gateway_owner_and_shutdown(tmp_path):
    svc = process_service(tmp_path)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        assert await driver.ensure_ready(svc, 2)
        driver.request_started(svc)
        await asyncio.sleep(0.16)
        assert await driver.status(svc) == "running"
        driver.request_finished(svc)
        await asyncio.sleep(0.16)
        assert await driver.status(svc) == "stopped"
        svc["residency"]["idle_owner"] = "engine"
        assert await driver.ensure_ready(svc, 2)
        await asyncio.sleep(0.16)
        assert await driver.status(svc) == "running"
        driver.shutdown()
        assert await driver.status(svc) == "stopped"

    run(scenario())


def test_process_log_redacts_declared_secret_and_stays_capped(tmp_path):
    svc = process_service(tmp_path)
    svc["process"]["env"] = {"TEST_SECRET": "test-secret-value"}
    svc["process"]["log_max_bytes"] = 1024
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        assert await driver.ensure_ready(svc, 2)
        await asyncio.sleep(0.05)
        driver.shutdown()

    run(scenario())
    log = (tmp_path / "state" / "fixture.log").read_bytes()
    assert b"test-secret-value" not in log
    assert b"[redacted]" in log
    assert len(log) <= 1024


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object boundary")
def test_windows_process_driver_fails_closed_without_job_object(tmp_path):
    svc = process_service(tmp_path)
    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
    assert run(driver.ensure_ready(svc, 0.1)) is False
    assert not (tmp_path / "state").exists()


def test_process_crash_restart_budget_and_orphan_adoption(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    first = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        assert await first.ensure_ready(svc, 2)
        pid = json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"]
        second = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
        assert await second.ensure_ready(svc, 2)
        assert json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"] == pid
        os.killpg(pid, signal.SIGKILL)
        await asyncio.sleep(0.05)
        assert await second.ensure_ready(svc, 2)
        restarted = json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"]
        os.killpg(restarted, signal.SIGKILL)
        await asyncio.sleep(0.05)
        assert await second.ensure_ready(svc, 2)
        restarted = json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"]
        os.killpg(restarted, signal.SIGKILL)
        await asyncio.sleep(0.05)
        assert await second.ensure_ready(svc, 2) is False

    try:
        run(scenario())
    finally:
        first.stop(svc)


def test_process_boot_recovery_adopts_matching_recipe_and_terminates_disabled_one(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    original = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        assert await original.ensure_ready(svc, 2)
        adopted = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
        adopted.recover({"fixture": svc})
        assert await adopted.status(svc) == "running"
        disabled = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: False)
        disabled.recover({"fixture": svc})
        assert await disabled.status(svc) == "stopped"

    try:
        run(scenario())
    finally:
        original.stop(svc)


def test_adopted_process_that_never_passes_readiness_is_stopped(tmp_path):
    svc = process_service(tmp_path)
    svc["residency"]["idle_owner"] = "engine"
    original = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)

    async def scenario():
        assert await original.ensure_ready(svc, 2)
        svc["observations"]["process_ready"]["http"]["expect"] = 201
        adopted = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True)
        assert await adopted.ensure_ready(svc, 0.05) is False
        assert await adopted.status(svc) == "stopped"

    try:
        run(scenario())
    finally:
        original.stop(svc)


def test_process_readiness_probe_respects_total_deadline(tmp_path):
    svc = process_service(tmp_path)

    class SlowProbe:
        async def read(self, *_args):
            await asyncio.sleep(0.08)
            return type("Fact", (), {"known": True, "value": True})()

    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True,
                           observations=SlowProbe())
    started = time.monotonic()
    assert run(driver._ready(svc, 0.01)) is False
    assert time.monotonic() - started < 0.06


@pytest.mark.skipif(os.name != "posix", reason="Linux process groups")
def test_stop_kills_term_ignoring_descendant_after_leader_exits(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "group.py"
    script.write_text(
        "import os, signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    svc = process_service(tmp_path)
    svc["process"]["start"] = [sys.executable, str(script), str(child_pid_file)]

    class Ready:
        async def read(self, *_args):
            return type("Fact", (), {"known": True, "value": True})()

    driver = ProcessDriver(state_dir=tmp_path / "state", commands_allowed=lambda: True,
                           observations=Ready())
    child_pid = None
    group_pid = None
    try:
        assert run(driver.ensure_ready(svc, 1))
        group_pid = json.loads((tmp_path / "state" / "fixture.json").read_text())["pid"]
        for _ in range(100):
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
                break
            time.sleep(0.01)
        assert child_pid is not None
        driver.stop(svc)
        for _ in range(100):
            status = Path(f"/proc/{child_pid}/stat")
            if not status.exists() or status.read_text().split(") ", 1)[1].startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("TERM-ignoring descendant survived gateway stop")
    finally:
        driver.stop(svc)
        if group_pid:
            try:
                os.killpg(group_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_residency_deadline_includes_control_and_observation_wait():
    async def slow_invoke(_op, _args):
        await asyncio.sleep(0.08)

    async def slow_observe(_name):
        await asyncio.sleep(0.08)
        return True

    controller = ResidencyController(invoke=slow_invoke, observe=slow_observe)
    service = {"residency": {"load": {"op": "load"},
                             "load_deadline": {"stall_s": 0.01, "max_s": 0.02}}}
    started = time.monotonic()
    assert run(controller.load(service)) is False
    assert time.monotonic() - started < 0.06


def test_observations_are_unknown_without_probe_and_inventory_is_not_residency():
    reader = ObservationReader()
    assert run(reader.read({"connection": {"address": "http://127.0.0.1:1"}}, "resident")).known is False
    assert reader.inventory_means({"inventory": {"means": "available"}}) == "available"
    assert reader.inventory_means({"inventory": {"http": {"path": "/models"}}}) == "unknown"


def test_http_observation_uses_expected_status_and_json_pointer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}], "resident": False})

    reader = ObservationReader(client_factory=observation_client(handler))
    svc = {"connection": {"address": "http://fixture.test:18091"}, "observations": {
        "inventory": {"http": {"path": "/v1/models", "expect": 200, "select": "/data/*/id"},
                      "means": "available"},
        "resident": {"http": {"path": "/state", "expect": 200, "select": "/resident"}},
        "selected": {"http": {"path": "/state", "expect": 201, "select": "/selected"}},
    }}

    async def scenario():
        assert (await reader.read(svc, "inventory")).value == ["a", "b"]
        assert (await reader.read(svc, "resident")).value is False
        assert (await reader.read(svc, "selected")).known is False

    run(scenario())
    assert seen == [("GET", "http://fixture.test:18091/v1/models"),
                    ("GET", "http://fixture.test:18091/state"),
                    ("GET", "http://fixture.test:18091/state")]


def test_switch_refuses_unknown_name_and_holds_lease_through_stream():
    calls = []
    selected = ["old"]
    drain = SwapDrain()

    async def invoke(op, args):
        calls.append((op, args))
        selected[0] = args["model"]

    async def observe(name):
        return selected[0] if name == "selected" else True

    controller = SwitchController(drain=drain, invoke=invoke, observe=observe)
    svc = {"id": "scope", "offerings": [{"id": "old"}, {"id": "new"}]}
    selection = {"mode": "switch", "switch": {"call": "select", "args": {"model": "new"},
                 "observe_selected": "selected", "exclusive_scope": "scope",
                 "drain_timeout_s": 0.2, "queue_wait_s": 0.2, "on_failure": "restore"}}

    async def stream():
        yield b"first"
        yield b"last"

    async def scenario():
        with pytest.raises(ValueError):
            await controller.select(svc, "missing", selection)
        assert calls == []
        lease = await controller.select(svc, "new", selection)
        chunks = lease.hand_off(stream())
        assert await chunks.__anext__() == b"first"
        with pytest.raises(SwitchUnavailable) as caught:
            await controller.select(svc, "old", {**selection, "switch": {**selection["switch"],
                "args": {"model": "old"}, "queue_wait_s": 0.01}})
        assert caught.value.retry_after_s > 0
        assert caught.value.status_code == 503
        assert caught.value.headers["Retry-After"]
        assert [chunk async for chunk in chunks] == [b"last"]
        second = await controller.select(svc, "old", {**selection, "switch": {**selection["switch"],
            "args": {"model": "old"}}})
        second.release()
        assert calls == [("select", {"model": "new"}), ("select", {"model": "old"})]

    run(scenario())


def test_switch_restores_previous_selection_after_failed_verification():
    selected = ["old"]
    calls = []

    async def invoke(_op, args):
        calls.append(args["model"])
        selected[0] = args["model"]

    async def observe(_name):
        return "wrong" if selected[0] == "new" else selected[0]

    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    svc = {"id": "scope", "offerings": [{"id": "old"}, {"id": "new"}]}
    selection = {"mode": "switch", "switch": {"call": "select", "args": {"model": "new"},
                 "observe_selected": "selected", "on_failure": "restore"}}
    with pytest.raises(SwitchUnavailable):
        run(controller.select(svc, "new", selection))
    assert calls == ["new", "old"]


def test_switch_restores_after_a_control_call_that_raises():
    calls = []
    selected = ["old"]

    async def invoke(_op, args):
        calls.append(args["model"])
        selected[0] = args["model"]
        if args["model"] == "new":
            raise RuntimeError("reply lost after switch")

    async def observe(_name):
        return selected[0]

    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
    svc = {"id": "scope", "offerings": [{"id": "old"}, {"id": "new"}]}
    selection = {"mode": "switch", "switch": {"call": "select", "args": {"model": "new"},
                 "observe_selected": "selected", "on_failure": "restore"}}
    with pytest.raises(SwitchUnavailable):
        run(controller.select(svc, "new", selection))
    assert calls == ["new", "old"]


def test_residency_unload_requires_resident_false_and_load_has_max_deadline():
    calls = []
    resident = [True]

    async def invoke(op, _args):
        calls.append(op)

    async def observe(name):
        return resident[0] if name == "resident" else None

    controller = ResidencyController(invoke=invoke, observe=observe)
    svc = {"residency": {"unload": {"op": "unload"}, "load": {"op": "load"},
                         "load_deadline": {"stall_s": 0.01, "max_s": 0.03, "progress": "none"}}}
    assert run(controller.unload(svc)) is False
    resident[0] = False
    assert run(controller.unload(svc)) is True
    assert run(controller.load(svc)) is False
    assert calls == ["unload", "unload", "load"]
