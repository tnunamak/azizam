"""Lifecycle drivers for the Engine Registry (ENGINE_REGISTRY_DESIGN §3.1).

Step 1 of the migration plan: implement the LifecycleDriver Protocol and the TWO
simplest drivers (external + docker).

Design axioms (§0):
- No driver installs, builds, or provisions anything.
- Drivers may ONLY operate resources the operator already created.
- `external` is the default when no lifecycle.driver is declared — preserving
  the behaviour of every backend that exists today (all have managed: false).

Drivers implemented here
------------------------
ExternalDriver (default)
    The engine is always on; we do not own its lifecycle.
    ensure_ready  → health-check GET only; no process spawn.
    stop          → no-op; not ours to stop.
    status        → "healthy" / "unhealthy" / "unknown" from health.
    preflight     → no-op (we can't inspect an external process).

DockerDriver
    ensure_ready  → `docker start <container>` then poll health.
    stop          → `docker stop <container>`.
    status        → `docker inspect` exit/running state + health.
    preflight     → `docker inspect` (read-only existence check).
    NEVER: `docker run`, `docker pull`, `docker build`.
    driver_config keys: container (required), compose_file, compose_service.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Health states & process states (§3.1 vocabulary)
# ---------------------------------------------------------------------------

class HealthState:
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"


class ProcessState:
    STOPPED   = "stopped"
    STARTING  = "starting"
    RUNNING   = "running"
    DRAINING  = "draining"
    SWAPPING  = "swapping"
    FAILED    = "failed"
    UNMANAGED = "unmanaged"


# ---------------------------------------------------------------------------
# Protocol (§3.1 minimal interface)
# ---------------------------------------------------------------------------

@runtime_checkable
class LifecycleDriver(Protocol):
    """Minimal interface every driver must satisfy."""

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        """Ensure the backend is reachable and ready to serve requests.

        Returns True if the backend is healthy, False if unreachable/failed.
        Managed drivers (docker, systemd) may start the process first.
        """
        ...

    def stop(self, svc: Mapping[str, Any]) -> None:
        """Stop the backend (no-op for unmanaged drivers)."""
        ...

    async def status(self, svc: Mapping[str, Any]) -> str:
        """Return a ProcessState string."""
        ...

    async def health(self, svc: Mapping[str, Any]) -> str:
        """Return a HealthState string (always safe to call)."""
        ...

    async def preflight(self, svc: Mapping[str, Any]) -> Optional[str]:
        """Read-only pre-flight check. Returns a warning string or None."""
        ...


# ---------------------------------------------------------------------------
# Shared health-check helper
# ---------------------------------------------------------------------------

async def _poll_health(svc: Mapping[str, Any], timeout_s: float) -> bool:
    """Poll the backend health endpoint until it responds 200 or timeout_s elapses.

    Reads host, port, health_endpoint from the svc (config["backends"][id]) dict.
    Polls every 0.5 s.  Returns False immediately if host/port are missing.
    """
    import httpx  # local import to avoid circular at module level

    host = svc.get("host", "127.0.0.1")
    port = svc.get("port")
    if not port:
        return False
    endpoint = svc.get("health_endpoint", "/health")
    url = f"http://{host}:{port}{endpoint}"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    interval = 0.5
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=min(interval * 2, 5.0))
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# ExternalDriver — the default (managed: false, existing always-on backends)
# ---------------------------------------------------------------------------

class ExternalDriver:
    """Driver for always-on backends the operator manages externally.

    ensure_ready  = health GET; no process spawn, ever.
    stop          = no-op.
    preflight     = no-op.
    """

    # A short timeout for the initial health check is intentional: external
    # backends are expected to already be running, so if they don't respond
    # quickly the call should fail fast rather than block the request path.
    _DEFAULT_HEALTH_TIMEOUT_S: float = 5.0

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        """Health-check the backend.  Never spawns a process."""
        effective_timeout = min(timeout_s, self._DEFAULT_HEALTH_TIMEOUT_S)
        return await _poll_health(svc, effective_timeout)

    def stop(self, svc: Mapping[str, Any]) -> None:
        """No-op: we do not own this backend."""

    async def status(self, svc: Mapping[str, Any]) -> str:
        ok = await _poll_health(svc, self._DEFAULT_HEALTH_TIMEOUT_S)
        if ok:
            return ProcessState.RUNNING
        return ProcessState.UNMANAGED

    async def health(self, svc: Mapping[str, Any]) -> str:
        ok = await _poll_health(svc, self._DEFAULT_HEALTH_TIMEOUT_S)
        return HealthState.HEALTHY if ok else HealthState.UNHEALTHY

    async def preflight(self, svc: Mapping[str, Any]) -> Optional[str]:
        """No read-only check available for an external process."""
        return None


# ---------------------------------------------------------------------------
# DockerDriver — drives a container the operator already created
# ---------------------------------------------------------------------------

class DockerDriver:
    """Driver for backends running inside a Docker container.

    Required driver_config key: ``container`` — the Docker container name or id.
    Optional: ``compose_file`` + ``compose_service`` to use docker compose.

    Boundary (§3.3):
    - NEVER `docker run`, `docker pull`, `docker build`.
    - NEVER removes or recreates a container.
    - `start`/`stop` only drive a container that already exists.
    """

    _DOCKER_POLL_INTERVAL_S: float = 1.0
    _DOCKER_CMD_TIMEOUT_S: float = 30.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        """Start the container (if needed) then poll health until ready."""
        driver_cfg = self._driver_config(svc)
        ok = await self._start(driver_cfg)
        if not ok:
            return False
        return await _poll_health(svc, timeout_s)

    def stop(self, svc: Mapping[str, Any]) -> None:
        """Stop the container synchronously."""
        driver_cfg = self._driver_config(svc)
        cmd = self._stop_cmd(driver_cfg)
        try:
            subprocess.run(
                cmd,
                timeout=self._DOCKER_CMD_TIMEOUT_S,
                check=False,
                capture_output=True,
            )
        except Exception:
            pass  # best-effort

    async def status(self, svc: Mapping[str, Any]) -> str:
        driver_cfg = self._driver_config(svc)
        running = await self._is_running(driver_cfg)
        if running is None:
            return ProcessState.UNKNOWN  # type: ignore[attr-defined]
        if running:
            ok = await _poll_health(svc, self._DOCKER_POLL_INTERVAL_S)
            return ProcessState.RUNNING if ok else ProcessState.STARTING
        return ProcessState.STOPPED

    async def health(self, svc: Mapping[str, Any]) -> str:
        driver_cfg = self._driver_config(svc)
        running = await self._is_running(driver_cfg)
        if not running:
            return HealthState.UNHEALTHY
        ok = await _poll_health(svc, self._DOCKER_POLL_INTERVAL_S)
        return HealthState.HEALTHY if ok else HealthState.DEGRADED

    async def preflight(self, svc: Mapping[str, Any]) -> Optional[str]:
        """Read-only: docker inspect to confirm container exists."""
        driver_cfg = self._driver_config(svc)
        container = driver_cfg.get("container")
        if not container:
            return "docker driver_config.container is required but not set"
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}}", container],
                capture_output=True,
                timeout=self._DOCKER_CMD_TIMEOUT_S,
                check=False,
            )
            if result.returncode != 0:
                return (
                    f"docker inspect '{container}' failed (container may not exist): "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
        except FileNotFoundError:
            return "docker executable not found; DockerDriver requires Docker to be installed"
        except Exception as exc:
            return f"docker inspect error: {exc}"
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _driver_config(svc: Mapping[str, Any]) -> Mapping[str, Any]:
        lifecycle = svc.get("lifecycle") or {}
        if isinstance(lifecycle, Mapping):
            return lifecycle.get("driver_config") or {}
        return {}

    def _start_cmd(self, driver_cfg: Mapping[str, Any]) -> list[str]:
        """Build the `docker start` or `docker compose up -d` command.

        Never produces `docker run`, `docker pull`, or `docker build`.
        """
        compose_file = driver_cfg.get("compose_file")
        compose_svc = driver_cfg.get("compose_service")
        container = driver_cfg.get("container")

        if compose_file and compose_svc:
            cmd = ["docker", "compose", "-f", compose_file, "up", "-d", compose_svc]
        elif compose_svc:
            cmd = ["docker", "compose", "up", "-d", compose_svc]
        elif container:
            cmd = ["docker", "start", container]
        else:
            raise ValueError(
                "DockerDriver requires driver_config.container or "
                "driver_config.compose_service to be set"
            )
        return cmd

    def _stop_cmd(self, driver_cfg: Mapping[str, Any]) -> list[str]:
        compose_file = driver_cfg.get("compose_file")
        compose_svc = driver_cfg.get("compose_service")
        container = driver_cfg.get("container")

        if compose_file and compose_svc:
            return ["docker", "compose", "-f", compose_file, "stop", compose_svc]
        if compose_svc:
            return ["docker", "compose", "stop", compose_svc]
        if container:
            return ["docker", "stop", container]
        return []

    async def _start(self, driver_cfg: Mapping[str, Any]) -> bool:
        """Run `docker start` (or compose up) in a thread to avoid blocking the event loop."""
        try:
            cmd = self._start_cmd(driver_cfg)
        except ValueError:
            return False

        def _run() -> bool:
            try:
                result = subprocess.run(
                    cmd,
                    timeout=self._DOCKER_CMD_TIMEOUT_S,
                    check=False,
                    capture_output=True,
                )
                return result.returncode == 0
            except Exception:
                return False

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)

    async def _is_running(self, driver_cfg: Mapping[str, Any]) -> Optional[bool]:
        """Return True/False/None (None = inspect failed)."""
        container = driver_cfg.get("container")
        if not container:
            return None

        def _inspect() -> Optional[bool]:
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", container],
                    capture_output=True,
                    timeout=self._DOCKER_CMD_TIMEOUT_S,
                    check=False,
                )
                if result.returncode != 0:
                    return None
                return result.stdout.decode().strip() == "true"
            except Exception:
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _inspect)


# ---------------------------------------------------------------------------
# Driver registry — driver_for_service()
# ---------------------------------------------------------------------------

_DRIVER_MAP: dict[str, Any] = {
    "external": ExternalDriver,
    "docker":   DockerDriver,
}

# Module-level singletons (stateless drivers; safe to share across calls)
_external_driver = ExternalDriver()
_docker_driver   = DockerDriver()


def driver_for_service(service_id: str, config_backends: Optional[Mapping[str, Any]] = None) -> LifecycleDriver:
    """Return the appropriate LifecycleDriver for service_id.

    Reads ``lifecycle.driver`` from config["backends"][service_id].
    Defaults to ExternalDriver when the field is absent (preserving the
    current behaviour for every service in the existing 8-backend config).

    Parameters
    ----------
    service_id:
        Key in config["backends"].
    config_backends:
        Optional pre-extracted dict of backends.  When None, the caller is
        expected to pass the service config directly via ``svc`` to the
        driver methods.  This parameter exists mainly for testing without
        importing proxy directly.
    """
    if config_backends is not None:
        bcfg = config_backends.get(service_id) or {}
    else:
        # Lazy import to avoid circular dependency — proxy imports this module,
        # this module must NOT import proxy at module level.
        try:
            import proxy as _proxy  # type: ignore[import]
            bcfg = (_proxy.config.get("backends") or {}).get(service_id) or {}
        except ImportError:
            bcfg = {}

    lifecycle = bcfg.get("lifecycle") or {}
    if not isinstance(lifecycle, Mapping):
        lifecycle = {}

    driver_name = (lifecycle.get("driver") or "external").strip().lower()

    if driver_name == "docker":
        return _docker_driver
    # Default (including "external" and any unrecognised value) → external.
    # Unrecognised values get external so we fail safe rather than crash.
    return _external_driver
