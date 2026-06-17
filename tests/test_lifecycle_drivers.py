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
from unittest.mock import AsyncMock, MagicMock, patch, call

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.lifecycle_drivers import (
    DockerDriver,
    ExternalDriver,
    HealthState,
    ProcessState,
    driver_for_service,
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

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = _run(self.driver.ensure_ready(self.svc, timeout_s=0.1))

        self.assertFalse(result)

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

            result = _run(proxy.ensure_target("my-vllm", None))

        self.assertFalse(
            result,
            "ensure_target should return False when the backend is genuinely unreachable",
        )

    # -- kobold: swap path still taken (no regression) ----------------------

    def test_kobold_still_uses_swap_path_not_driver(self):
        """ensure_target('kobold', ...) still calls the kobold swap path, not the driver.

        This proves zero regression for the managed engines.
        """
        with patch.object(proxy, "get_active_backend", new=AsyncMock(return_value="kobold")), \
             patch.object(proxy, "swap_kobold_model", new=AsyncMock(return_value=True)) as mock_swap, \
             patch.object(proxy, "currently_loaded_kobold_model", return_value="/models/other.gguf"), \
             patch.object(proxy, "currently_loaded_kobold_mmproj", return_value=None), \
             patch.object(proxy, "is_literal_gguf_path", return_value=True):
            # Need os.path to pass the gguf validation; use is_literal_gguf_path=True
            result = _run(proxy.ensure_target("kobold", "/models/target.gguf"))

        # swap was called — proving the kobold branch ran, not the driver branch
        mock_swap.assert_called_once()
        self.assertTrue(result, "ensure_target('kobold', ...) should return True after swap")

    def test_tabby_still_uses_swap_path_not_driver(self):
        """ensure_target('tabby', ...) still calls swap_tabby_model, not the driver."""
        with patch.object(proxy, "get_active_backend", new=AsyncMock(return_value="tabby")), \
             patch.object(proxy, "swap_tabby_model", new=AsyncMock(return_value=True)) as mock_swap, \
             patch.object(proxy, "currently_loaded_tabby_model", new=AsyncMock(return_value="other-model")):
            result = _run(proxy.ensure_target("tabby", "qwen3-vl-30b"))

        mock_swap.assert_called_once_with("qwen3-vl-30b")
        self.assertTrue(result, "ensure_target('tabby', ...) should return True after swap")

    def test_driver_path_not_taken_for_kobold(self):
        """The lifecycle driver is NOT instantiated for kobold (byte-identical path)."""
        with patch("gateway.lifecycle_drivers.driver_for_service") as mock_dfs, \
             patch.object(proxy, "get_active_backend", new=AsyncMock(return_value="kobold")), \
             patch.object(proxy, "currently_loaded_kobold_model", return_value=None), \
             patch.object(proxy, "currently_loaded_kobold_mmproj", return_value=None):
            # No model provided, so no swap needed — just health check after activation
            _run(proxy.ensure_target("kobold", None))

        mock_dfs.assert_not_called()

    def test_driver_path_not_taken_for_tabby(self):
        """The lifecycle driver is NOT instantiated for tabby (byte-identical path)."""
        with patch("gateway.lifecycle_drivers.driver_for_service") as mock_dfs, \
             patch.object(proxy, "get_active_backend", new=AsyncMock(return_value="tabby")), \
             patch.object(proxy, "currently_loaded_tabby_model", new=AsyncMock(return_value=None)):
            _run(proxy.ensure_target("tabby", None))

        mock_dfs.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
