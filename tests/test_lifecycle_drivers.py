"""Tests for the LifecycleDriver abstraction (ENGINE_REGISTRY_DESIGN §3.1, Step 1).

Verifies:
- driver_for_service() returns ExternalDriver for a service with no lifecycle.driver
  (the default, which covers every backend in the current 8-service config).
- driver_for_service() returns DockerDriver when lifecycle.driver == "docker".
- ExternalDriver.ensure_ready() returns True for a reachable (mocked-healthy) backend.
- ExternalDriver.ensure_ready() returns False for an unreachable backend.
- ExternalDriver.stop() is a no-op (never spawns a process).
- THE BUG-FIX PROOF: ensure_target("my-vllm", ...) for a new external
  openai-compatible backend returns True (was: False/503) when the health check
  passes.
- Kobold/tabby regression guard: ensure_target("kobold"/"tabby", ...) still calls
  the swap path (not the driver path).
- DockerDriver.ensure_ready() issues `docker start` (asserts command, no run/pull/build).
- DockerDriver.preflight() is read-only (docker inspect only).
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import lifecycle_drivers
from gateway.lifecycle_drivers import (
    CommandDriver,
    DockerDriver,
    ExternalDriver,
    HealthState,
    ProcessState,
    driver_for_service,
    supports_swap,
)


# ---------------------------------------------------------------------------
# Async test helper
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine synchronously (compatible with Python 3.10+).

    Uses asyncio.run() to create a fresh event loop each time so that
    prior tests that close the default loop do not break us.
    """
    return asyncio.run(coro)


class _PollingClock:
    """Advance only the driver's clock, leaving the real event loop untouched."""

    def __init__(self):
        self.now = 0.0
        self.delays = []

    def time(self):
        return self.now

    async def sleep(self, delay):
        # Bound a missing-deadline mutant instead of hanging the test runner.
        if len(self.delays) >= 10:
            raise AssertionError("health polling did not stop at its deadline")
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Minimal backend config dicts (mirror the proxy_config.yml schema)
# ---------------------------------------------------------------------------

def _external_svc(
    host: str = "127.0.0.1",
    port: int = 8080,
    health_endpoint: str = "/health",
    *,
    lifecycle_driver: str | None = None,
) -> dict:
    """Build a minimal config["backends"][id] dict (external / no lifecycle key)."""
    svc: dict[str, Any] = {
        "host": host,
        "port": port,
        "health_endpoint": health_endpoint,
        "enabled": True,
        "capabilities": ["chat", "completions"],
    }
    if lifecycle_driver is not None:
        svc["lifecycle"] = {"driver": lifecycle_driver}
    return svc


def _docker_svc(
    host: str = "127.0.0.1",
    port: int = 8080,
    container: str = "my-container",
) -> dict:
    return {
        "host": host,
        "port": port,
        "health_endpoint": "/health",
        "enabled": True,
        "capabilities": ["chat", "completions"],
        "lifecycle": {
            "driver": "docker",
            "driver_config": {"container": container},
        },
    }


# ---------------------------------------------------------------------------
# 1. driver_for_service() resolution
# ---------------------------------------------------------------------------

class TestDriverForService(unittest.TestCase):
    """driver_for_service() picks the right driver from lifecycle.driver."""

    def _call(self, service_id: str, backends: dict) -> object:
        return driver_for_service(service_id, config_backends=backends)

    def test_no_lifecycle_key_returns_external(self):
        """A backend with no lifecycle key gets ExternalDriver (the default)."""
        backends = {"my-vllm": _external_svc()}
        driver = self._call("my-vllm", backends)
        self.assertIsInstance(driver, ExternalDriver)

    def test_lifecycle_driver_external_explicit(self):
        """lifecycle.driver == 'external' returns ExternalDriver."""
        backends = {"my-vllm": _external_svc(lifecycle_driver="external")}
        driver = self._call("my-vllm", backends)
        self.assertIsInstance(driver, ExternalDriver)

    def test_v2_none_and_process_resolution(self):
        from gateway.engine_process import ProcessDriver

        backends = {
            "none": {"process": {"driver": "none"}},
            "owned": {"process": {"driver": "process"}},
        }
        self.assertIsInstance(self._call("none", backends), ExternalDriver)
        self.assertIsInstance(self._call("owned", backends), ProcessDriver)

    def test_lifecycle_driver_docker_returns_docker(self):
        """lifecycle.driver == 'docker' returns DockerDriver."""
        backends = {"ollama": _docker_svc()}
        driver = self._call("ollama", backends)
        self.assertIsInstance(driver, DockerDriver)

    def test_unknown_service_returns_external(self):
        """A service_id not present in config returns ExternalDriver (fail-safe)."""
        driver = self._call("nonexistent", {})
        self.assertIsInstance(driver, ExternalDriver)

    def test_kobold_no_lifecycle_key_gets_external(self):
        """kobold without a lifecycle key (legacy config) gets ExternalDriver."""
        backends = {
            "kobold": {
                "host": "127.0.0.1",
                "port": 5051,
                "health_endpoint": "/v1/models",
                "enabled": True,
                "managed": False,
                "capabilities": ["openai", "chat", "completions"],
            }
        }
        driver = self._call("kobold", backends)
        self.assertIsInstance(driver, ExternalDriver)

    def test_case_insensitive_driver_name(self):
        """lifecycle.driver value is case-insensitive."""
        backends = {"box": {**_external_svc(), "lifecycle": {"driver": "Docker"}}}
        driver = self._call("box", backends)
        self.assertIsInstance(driver, DockerDriver)

    def test_unrecognised_driver_falls_back_to_external(self):
        """An unknown driver name falls back safely to ExternalDriver."""
        backends = {"box": {**_external_svc(), "lifecycle": {"driver": "unknown-engine-xyz"}}}
        driver = self._call("box", backends)
        # unrecognised driver → safe fallback to ExternalDriver
        self.assertIsInstance(driver, ExternalDriver)


# ---------------------------------------------------------------------------
# 2. ExternalDriver behaviour
# ---------------------------------------------------------------------------

class TestExternalDriver(unittest.TestCase):
    """ExternalDriver health-checks and never spawns a process."""

    def setUp(self):
        self.driver = ExternalDriver()
        self.svc = _external_svc(port=8080)

    # -- ensure_ready returns True when backend responds 200 ----------------

    def test_ensure_ready_returns_true_when_healthy(self):
        """ensure_ready returns True for a backend responding 200 OK."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _fake_get(*args, **kwargs):
            return mock_resp

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = _run(self.driver.ensure_ready(self.svc, timeout_s=5.0))

        self.assertTrue(result)

    # -- ensure_ready returns False when backend is unreachable --------------

    def test_ensure_ready_returns_false_when_unreachable(self):
        """ensure_ready returns False when the health endpoint is unreachable."""
        import httpx as _httpx

        async def _fake_get(*args, **kwargs):
            raise _httpx.ConnectError("connection refused")

        clock = _PollingClock()
        # Replace the module's clock/sleep namespace, never loop.time or the
        # shared asyncio module: HTTP and the test runner keep their real loop.
        polling = SimpleNamespace(get_running_loop=lambda: clock, sleep=clock.sleep)
        with patch("httpx.AsyncClient") as mock_cls, \
             patch.object(lifecycle_drivers, "asyncio", polling):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = _run(self.driver.ensure_ready(self.svc, timeout_s=0.1))

        self.assertFalse(result)
        self.assertEqual(clock.delays, [0.5])
        self.assertEqual(mock_client.get.await_args_list, [
            call("http://127.0.0.1:8080/health", timeout=1.0),
            call("http://127.0.0.1:8080/health", timeout=1.0),
        ])

    # -- stop is a no-op — must never spawn a process -----------------------

    def test_stop_is_no_op(self):
        """stop() is a no-op; subprocess.run must never be called."""
        with patch("subprocess.run") as mock_run:
            self.driver.stop(self.svc)
            mock_run.assert_not_called()

    def test_ensure_ready_never_spawns_subprocess(self):
        """ensure_ready() must never call subprocess.run / subprocess.Popen."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen, \
             patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            _run(self.driver.ensure_ready(self.svc, timeout_s=5.0))

            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    # -- missing port → False immediately ------------------------------------

    def test_ensure_ready_false_when_no_port(self):
        """ensure_ready returns False immediately when port is missing."""
        svc = dict(self.svc)
        del svc["port"]
        result = _run(self.driver.ensure_ready(svc, timeout_s=5.0))
        self.assertFalse(result)

    # -- health() returns HealthState strings --------------------------------

    def test_health_healthy_when_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            state = _run(self.driver.health(self.svc))

        self.assertEqual(state, HealthState.HEALTHY)

    def test_health_unhealthy_when_unreachable(self):
        import httpx as _httpx

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # One refused probe is enough; skip the real 5 s polling window.
            with patch.object(ExternalDriver, "_DEFAULT_HEALTH_TIMEOUT_S", 0.0):
                state = _run(self.driver.health(_external_svc(port=9999)))

        self.assertEqual(state, HealthState.UNHEALTHY)

    # -- preflight is a no-op -----------------------------------------------

    def test_preflight_returns_none(self):
        warning = _run(self.driver.preflight(self.svc))
        self.assertIsNone(warning)


# ---------------------------------------------------------------------------
# 3. Bug-fix proof: ensure_target() for a new external backend → True
# ---------------------------------------------------------------------------

class TestEnsureTargetBugFix(unittest.TestCase):
    """THE KEY PROOF: ensure_target now routes new backends through their driver.

    Before the fix: `if backend not in ("kobold", "tabby"): return False`
    After the fix:  delegates to driver.ensure_ready() → True when healthy.
    """

    def setUp(self):
        # Inject a fake "my-vllm" backend into proxy.config so the driver
        # and health-check logic can resolve host/port.
        self._orig_config = copy.deepcopy(dict(proxy.config))
        backends = dict(proxy.config.get("backends") or {})
        backends["my-vllm"] = {
            "host": "127.0.0.1",
            "port": 9191,
            "health_endpoint": "/health",
            "enabled": True,
            "capabilities": ["chat", "completions"],
        }
        proxy.config["backends"] = backends

    def tearDown(self):
        proxy.config.clear()
        proxy.config.update(self._orig_config)

    # -- healthy external backend → True (was False/503 before fix) ----------

    def test_external_backend_returns_true_when_healthy(self):
        """ensure_target('my-vllm', None) returns True for a reachable backend."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = _run(proxy.ensure_target("my-vllm", None))

        self.assertTrue(
            result,
            "ensure_target should return True for a reachable external backend "
            "(this was the 503 bug — it returned False before the fix)",
        )

    # -- unreachable external backend → False (correct honest failure) -------

    def test_external_backend_returns_false_when_unreachable(self):
        """ensure_target('my-vllm', None) returns False when backend is down."""
        import httpx as _httpx

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            # One refused probe is enough; skip the real 5 s polling window.
            with patch.object(ExternalDriver, "_DEFAULT_HEALTH_TIMEOUT_S", 0.0):
                result = _run(proxy.ensure_target("my-vllm", None))

        self.assertFalse(
            result,
            "ensure_target should return False when the backend is genuinely unreachable",
        )


# ---------------------------------------------------------------------------
# 4. DockerDriver behaviour
# ---------------------------------------------------------------------------

class TestDockerDriver(unittest.TestCase):
    """DockerDriver issues `docker start`, never `docker run`/`--pull`/`build`."""

    def setUp(self):
        self.driver = DockerDriver()
        self.svc = _docker_svc(port=8090, container="my-container")

    # -- ensure_ready issues docker start, then polls health ----------------

    def test_ensure_ready_calls_docker_start(self):
        """ensure_ready must call `docker start <container>` (not run/pull/build)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        docker_result = MagicMock()
        docker_result.returncode = 0

        with patch("subprocess.run", return_value=docker_result) as mock_run, \
             patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = _run(self.driver.ensure_ready(self.svc, timeout_s=5.0))

        self.assertTrue(result)

        mock_run.assert_called_once_with(
            ["docker", "start", "my-container"],
            timeout=30.0, check=False, capture_output=True,
        )
        mock_client.get.assert_awaited_once_with(
            "http://127.0.0.1:8090/health", timeout=1.0,
        )

        # Extract all subprocess.run calls and their first positional arg (the command)
        all_cmds = [str(c) for c in mock_run.call_args_list]
        cmd_strs = " ".join(all_cmds)

        # Must contain "docker start"
        self.assertIn("docker", cmd_strs)
        self.assertIn("start", cmd_strs)

        # Must NEVER contain "run", "pull", or "build"
        for call_item in mock_run.call_args_list:
            args = call_item.args[0] if call_item.args else call_item.kwargs.get("args", [])
            args_str = " ".join(str(a) for a in args)
            self.assertNotIn("run", args_str.split(),
                             f"docker 'run' must never appear; got: {args_str}")
            self.assertNotIn("pull", args_str.split(),
                             f"docker 'pull' must never appear; got: {args_str}")
            self.assertNotIn("build", args_str.split(),
                             f"docker 'build' must never appear; got: {args_str}")

    def test_ensure_ready_returns_false_when_docker_start_fails(self):
        """ensure_ready returns False when docker start exits non-zero."""
        docker_result = MagicMock()
        docker_result.returncode = 1

        with patch("subprocess.run", return_value=docker_result):
            result = _run(self.driver.ensure_ready(self.svc, timeout_s=0.1))

        self.assertFalse(result)

    # -- start_cmd never includes docker run / pull / build -----------------

    def test_start_cmd_uses_docker_start(self):
        """_start_cmd produces `docker start <container>`, never docker run."""
        driver_cfg = {"container": "my-box"}
        cmd = self.driver._start_cmd(driver_cfg)
        self.assertEqual(cmd, ["docker", "start", "my-box"])
        self.assertNotIn("run", cmd)
        self.assertNotIn("pull", cmd)
        self.assertNotIn("build", cmd)

    def test_start_cmd_compose_service(self):
        """_start_cmd uses docker compose up -d for compose_service."""
        driver_cfg = {"compose_file": "/app/docker-compose.yml", "compose_service": "llm"}
        cmd = self.driver._start_cmd(driver_cfg)
        self.assertEqual(cmd, ["docker", "compose", "-f", "/app/docker-compose.yml", "up", "-d", "llm"])
        self.assertNotIn("run", cmd)

    def test_start_cmd_no_container_raises(self):
        """_start_cmd raises ValueError when neither container nor compose_service is set."""
        with self.assertRaises(ValueError):
            self.driver._start_cmd({})

    # -- preflight is read-only: only docker inspect -------------------------

    def test_preflight_calls_docker_inspect_only(self):
        """preflight() must only call `docker inspect`, never run/start/stop."""
        inspect_result = MagicMock()
        inspect_result.returncode = 0
        inspect_result.stdout = b"my-container\n"

        with patch("subprocess.run", return_value=inspect_result) as mock_run:
            warning = _run(self.driver.preflight(self.svc))

        self.assertIsNone(warning)
        mock_run.assert_called_once_with(
            ["docker", "inspect", "--format", "{{.Name}}", "my-container"],
            capture_output=True, timeout=30.0, check=False,
        )
        # All subprocess calls must be docker inspect
        for c in mock_run.call_args_list:
            args = c.args[0] if c.args else c.kwargs.get("args", [])
            args_str = " ".join(str(a) for a in args)
            self.assertIn("inspect", args_str,
                          f"preflight must only call docker inspect; got: {args_str}")
            self.assertNotIn("start", args_str.split())
            self.assertNotIn("stop", args_str.split())
            self.assertNotIn("run", args_str.split())

    def test_preflight_returns_warning_when_container_missing(self):
        """preflight returns a warning string when docker inspect fails."""
        inspect_result = MagicMock()
        inspect_result.returncode = 1
        inspect_result.stderr = b"Error: No such container: nonexistent\n"

        with patch("subprocess.run", return_value=inspect_result):
            warning = _run(self.driver.preflight(self.svc))

        self.assertIsNotNone(warning)
        self.assertIsInstance(warning, str)

    def test_preflight_returns_warning_when_no_container_cfg(self):
        """preflight returns a warning when driver_config.container is absent."""
        svc_no_container = {
            "host": "127.0.0.1",
            "port": 8080,
            "lifecycle": {"driver": "docker", "driver_config": {}},
        }
        with patch("subprocess.run") as mock_run:
            warning = _run(self.driver.preflight(svc_no_container))
            mock_run.assert_not_called()  # no subprocess without a container name

        self.assertIsNotNone(warning)

    # -- stop issues docker stop, not run/build/pull -------------------------

    def test_stop_calls_docker_stop(self):
        """stop() calls `docker stop <container>`."""
        stop_result = MagicMock()
        stop_result.returncode = 0

        with patch("subprocess.run", return_value=stop_result) as mock_run:
            self.driver.stop(self.svc)

        called_args = mock_run.call_args.args[0]
        self.assertEqual(called_args, ["docker", "stop", "my-container"])


# ---------------------------------------------------------------------------
# 5. CommandDriver — the OS-agnostic, supervisor-agnostic escape hatch
# ---------------------------------------------------------------------------

def _command_svc(
    *,
    start: Any = "supervisorctl start myengine",
    stop: Any = None,
    status: Any = None,
    cwd: str | None = None,
    env: dict | None = None,
    timeout_s: Any = None,
    port: int = 8080,
) -> dict:
    driver_config: dict[str, Any] = {"start": start}
    if stop is not None:
        driver_config["stop"] = stop
    if status is not None:
        driver_config["status"] = status
    if cwd is not None:
        driver_config["cwd"] = cwd
    if env is not None:
        driver_config["env"] = env
    if timeout_s is not None:
        driver_config["timeout_s"] = timeout_s
    return {
        "host": "127.0.0.1",
        "port": port,
        "health_endpoint": "/health",
        "enabled": True,
        "capabilities": ["chat", "completions"],
        "lifecycle": {"driver": "command", "driver_config": driver_config},
    }


def _proc(returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    return m


class TestCommandDriverResolution(unittest.TestCase):
    """driver_for_service resolves 'command' to CommandDriver."""

    def test_resolves_to_command_driver(self):
        backends = {"myengine": _command_svc()}
        driver = driver_for_service("myengine", config_backends=backends)
        self.assertIsInstance(driver, CommandDriver)

    def test_command_driver_does_not_support_swap(self):
        """CommandDriver is a process-lifecycle driver, not a single-slot swapper."""
        self.assertFalse(supports_swap(CommandDriver()))

    def test_build_activation_is_noop(self):
        self.assertEqual(
            CommandDriver().build_activation(_command_svc(), model="x"), {}
        )


class TestCommandDriverEnsureReady(unittest.TestCase):
    """ensure_ready orchestration: status-gate the start, then poll health."""

    def setUp(self):
        self.driver = CommandDriver(commands_allowed=lambda: True)

    def test_command_driver_gate_refuses_all_host_calls(self):
        denied = CommandDriver(commands_allowed=lambda: False)
        svc = _command_svc(start=["systemctl", "start", "fixture"])
        with patch("subprocess.run") as run_command:
            self.assertFalse(_run(denied.ensure_ready(svc, timeout_s=1)))
            denied.stop(svc)
            self.assertEqual(_run(denied.status(svc)), ProcessState.UNKNOWN)
        run_command.assert_not_called()

    def test_v2_command_argv_runs_without_claiming_a_pid(self):
        svc = {"process": {"driver": "command", "start": ["supervisor", "start", "fixture"]},
               "host": "127.0.0.1", "port": 1}
        with patch("subprocess.run", return_value=_proc(0)) as command, patch(
            "gateway.lifecycle_drivers._poll_health", new=AsyncMock(return_value=True)
        ):
            self.assertTrue(_run(self.driver.ensure_ready(svc, timeout_s=1)))
        self.assertEqual(command.call_args.args[0], ["supervisor", "start", "fixture"])
        self.assertFalse(hasattr(self.driver, "pid"))

    def test_start_runs_when_status_says_stopped(self):
        """status exits non-zero (stopped) -> start command runs, then health poll."""
        svc = _command_svc(
            start="sv up myengine", status="sv check myengine"
        )
        # status -> rc 1 (stopped); start -> rc 0
        runs = [_proc(1), _proc(0)]
        with patch("subprocess.run", side_effect=runs) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)) as mock_poll:
            ok = _run(self.driver.ensure_ready(svc, timeout_s=5.0))

        self.assertTrue(ok)
        # two subprocess calls: status then start
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[0].args[0], ["sv", "check", "myengine"])
        self.assertEqual(mock_run.call_args_list[1].args[0], ["sv", "up", "myengine"])
        mock_poll.assert_awaited_once()

    def test_start_skipped_when_status_says_running(self):
        """status exits 0 (running) -> start is NOT run; just health-poll."""
        svc = _command_svc(start="sv up myengine", status="sv check myengine")
        with patch("subprocess.run", side_effect=[_proc(0)]) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)):
            ok = _run(self.driver.ensure_ready(svc, timeout_s=5.0))

        self.assertTrue(ok)
        # only the status check ran — no start
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(mock_run.call_args_list[0].args[0], ["sv", "check", "myengine"])

    def test_no_status_command_starts_idempotently(self):
        """No status command -> start runs (idempotent), health poll decides ready."""
        svc = _command_svc(start="systemctl --user start myengine")  # no status
        with patch("subprocess.run", side_effect=[_proc(0)]) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)):
            ok = _run(self.driver.ensure_ready(svc, timeout_s=5.0))

        self.assertTrue(ok)
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(
            mock_run.call_args_list[0].args[0],
            ["systemctl", "--user", "start", "myengine"],
        )

    def test_start_failure_returns_false_without_polling(self):
        """If the start command exits non-zero, ensure_ready returns False."""
        svc = _command_svc(start="false")  # no status -> start runs, rc 1
        with patch("subprocess.run", side_effect=[_proc(1)]), \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)) as mock_poll:
            ok = _run(self.driver.ensure_ready(svc, timeout_s=5.0))

        self.assertFalse(ok)
        mock_poll.assert_not_awaited()

    def test_missing_start_command_returns_false(self):
        """A command service with no start command fails closed."""
        svc = _command_svc()
        svc["lifecycle"]["driver_config"] = {}  # strip start
        with patch("subprocess.run") as mock_run:
            ok = _run(self.driver.ensure_ready(svc, timeout_s=5.0))
        self.assertFalse(ok)
        mock_run.assert_not_called()


class TestCommandDriverCommands(unittest.TestCase):
    """Command parsing, stop, preflight, env/cwd, no-shell."""

    def setUp(self):
        self.driver = CommandDriver(commands_allowed=lambda: True)

    def test_string_command_is_shlex_split(self):
        svc = _command_svc(start='nssm start "My Engine"', status="true")
        with patch("subprocess.run", side_effect=[_proc(1), _proc(0)]) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)):
            _run(self.driver.ensure_ready(svc, timeout_s=1.0))
        # shlex keeps the quoted arg as one token
        self.assertEqual(
            mock_run.call_args_list[1].args[0], ["nssm", "start", "My Engine"]
        )

    def test_list_command_passed_through(self):
        svc = _command_svc(start=["ssh", "box", "systemctl start eng"], status="true")
        with patch("subprocess.run", side_effect=[_proc(1), _proc(0)]) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)):
            _run(self.driver.ensure_ready(svc, timeout_s=1.0))
        self.assertEqual(
            mock_run.call_args_list[1].args[0],
            ["ssh", "box", "systemctl start eng"],
        )

    def test_never_uses_shell(self):
        """No subprocess.run call may pass shell=True (no shell-injection surface)."""
        svc = _command_svc(start="start me", stop="stop me", status="check me")
        with patch("subprocess.run", side_effect=[_proc(1), _proc(0)]) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)):
            _run(self.driver.ensure_ready(svc, timeout_s=1.0))
        for c in mock_run.call_args_list:
            self.assertNotIn("shell", c.kwargs)

    def test_stop_runs_stop_command(self):
        svc = _command_svc(start="x", stop="supervisorctl stop myengine")
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            self.driver.stop(svc)
        self.assertEqual(
            mock_run.call_args.args[0], ["supervisorctl", "stop", "myengine"]
        )

    def test_stop_noop_when_no_stop_command(self):
        svc = _command_svc(start="x")  # no stop
        with patch("subprocess.run") as mock_run:
            self.driver.stop(svc)
        mock_run.assert_not_called()

    def test_preflight_requires_start(self):
        svc = _command_svc()
        svc["lifecycle"]["driver_config"] = {}
        self.assertIsNotNone(_run(self.driver.preflight(svc)))
        self.assertIsNone(_run(self.driver.preflight(_command_svc())))

    def test_cwd_and_env_passed_to_subprocess(self):
        svc = _command_svc(
            start="run", status="check", cwd="/opt/engine", env={"FOO": "bar"}
        )
        with patch("subprocess.run", side_effect=[_proc(1), _proc(0)]) as mock_run, \
             patch("gateway.lifecycle_drivers._poll_health",
                   new=AsyncMock(return_value=True)):
            _run(self.driver.ensure_ready(svc, timeout_s=1.0))
        start_call = mock_run.call_args_list[1]
        self.assertEqual(start_call.kwargs.get("cwd"), "/opt/engine")
        self.assertEqual(start_call.kwargs["env"]["FOO"], "bar")
        # merged over the real environment, not a bare {FOO: bar}
        self.assertIn("PATH", start_call.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
