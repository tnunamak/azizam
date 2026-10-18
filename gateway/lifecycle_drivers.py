"""Lifecycle drivers for the Engine Registry (ENGINE_REGISTRY_DESIGN §3.1).

Step 1 of the migration plan: implement the LifecycleDriver Protocol and the TWO
simplest drivers (external + docker).

SystemdDriver and HttpLoadUnloadDriver are the two swap-capable drivers that
replaced the per-engine hard-coded swap branches (now deleted). They are the
sole swap path in ensure_target — there is no flag and no legacy fallback.

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

SystemdDriver  (swap_strategy: dropin-env-restart)
    Drives a systemd unit the operator already wrote.
    swap()        → write drop-in env file → daemon-reload → restart → poll health;
                    on failure, restore the previous drop-in and restart on it.
    stop()        → systemctl stop <unit> (only fires when idle_timeout_s > 0).
    ensure_ready  → systemctl start <unit> then poll health.
    driver_config keys: scope, unit, dropin, swap_strategy, health_endpoint,
                        health_timeout_s, activation_env_keys,
                        activation_profiles.
    NEVER writes anything except the single declared drop-in file.
    NEVER installs or modifies the unit itself.

HttpLoadUnloadDriver  (http-load-unload)
    Drives a model-server that exposes load/unload HTTP endpoints.
    swap()        → verify current → unload if needed → load target → confirm.
    stop()        → POST unload_url.
    driver_config keys: load_url, unload_url, admin_header, load_body_params,
                        load_timeout_s, success_marker, verify_endpoint.
    The load_body_params value names a registered params-provider (option A from
    LIFECYCLE_DRIVER_PORT.md §3.2) — the driver calls
    PARAMS_PROVIDERS[load_body_params](svc) to get live model params at swap time.
"""

from __future__ import annotations

from gateway.compat import get_env
from gateway import runtime_secrets

import asyncio
import contextlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, TypeVar, runtime_checkable

_T = TypeVar("_T")


def _health_endpoint(driver_config: Mapping[str, Any], default: str) -> str:
    """A driver's health path, with the query credentials its record stores for it put back (resolved now)."""
    try:
        return runtime_secrets.url(driver_config, "health_endpoint") or default
    except runtime_secrets.SecretUnavailable:
        return default


class SwapRecoveryFailed(RuntimeError):
    """A swap failed, and putting back the activation the unit ran before failed too.

    The unit may be down or on the failed activation; an operator must look.
    Raised by ``swap()`` instead of returning False, so callers can tell
    "the target could not be loaded" from "the backend may now be broken".
    """

    def __init__(self, unit: str, detail: str) -> None:
        self.unit = unit
        self.detail = detail
        super().__init__(f"unit {unit!r}: swap failed and recovery failed: {detail}")


def _replace_file_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``: readers see the old bytes or the new, never a mix.

    Writes a temporary file in the same directory, flushes and fsyncs it, then
    renames it over ``path``. On any failure the temporary file is removed and
    ``path`` is unchanged. The rename is then made durable with a directory
    fsync, best effort: if that fails, the new bytes are in place but the rename
    may not survive a power loss, and a warning is logged.
    """
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        print(
            f"[lifecycle] WARN: {path} was replaced, but the directory fsync failed ({exc}); "
            "the change may not survive a power loss",
            flush=True,
        )


async def _finish_even_if_cancelled(operation: Awaitable[_T]) -> _T:
    """Await ``operation`` to its end, even if the caller is cancelled meanwhile.

    A cancelled caller keeps waiting until ``operation`` finishes, then the
    cancellation is raised. Use it for a transaction that must not stop half
    way (a changed drop-in with no restart, or a restart with no rollback).
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():
            task.exception()  # retrieved; the operation logged its own failure
        raise asyncio.CancelledError
    return task.result()


# ---------------------------------------------------------------------------
# SwapContext — engine-agnostic state passed from ensure_target into swap()
# ---------------------------------------------------------------------------

@dataclass
class SwapContext:
    """Engine-agnostic context the caller supplies to driver.swap().

    The driver uses this to apply the already-loaded short-circuit and
    pin-loaded policy WITHOUT requiring the driver to know about any engine's
    probe functions.  The caller (ensure_target) collects the current state
    once and passes it in.

    Fields
    ------
    loaded_model:
        Model currently loaded on the backend (from the relevant probe), or
        None if nothing is loaded / backend is unreachable.
    target_model:
        The model we want loaded after the swap.  The driver uses this for the
        already-loaded comparison instead of inferring it from activation.
    loaded_aux:
        Currently-loaded aux file (mmproj) for drivers that support aux, or
        None.  Ignored by drivers that have no aux concept.
    target_aux:
        Desired aux file (mmproj) after the swap, or None for text-only.
        Ignored by drivers that have no aux concept.
    pin_loaded:
        If True the driver must NOT restart the service when there is a
        mismatch — warn once and return True (serve with whatever is loaded).
    target_profile:
        Optional named engine profile selected by model data. For systemd
        activations the profile is part of the loaded identity: matching the
        model and aux file is not enough to short-circuit when the managed
        drop-in was written for a different profile.
    """

    loaded_model: Optional[str]
    target_model: str
    loaded_aux: Optional[str] = None
    target_aux: Optional[str] = None
    pin_loaded: bool = False
    target_profile: Optional[str] = None

    # Per-SwapContext set tracking which models we have already warned about so
    # the warn-once behaviour is mirrored faithfully without needing a global
    # set in proxy.py.  In practice ensure_target creates a fresh SwapContext
    # per call, so the global proxy._pin_mismatch_warned set carries across
    # calls — but the driver only has this one call to emit the warning.
    # We handle warn-once by delegating the warning-set to the caller via the
    # `warned_models` parameter of the driver's swap method.  See SystemdDriver.
    pass


# ---------------------------------------------------------------------------
# Health states & process states (§3.1 vocabulary)
# ---------------------------------------------------------------------------

class HealthState:
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"


class ProcessState:
    UNKNOWN   = "unknown"
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

    def build_activation(
        self,
        svc: Mapping[str, Any],
        *,
        model: str,
        aux: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[dict]:
        """OPTIONAL — single-slot drivers only.  Build and precheck the activation dict.

        The activation dict is the resolved per-model block passed to swap():
          - systemd (dropin-env-restart): {"env": {KEY: VALUE, ...},
                                           "profile": str | None}
          - http-load-unload:             {"model_name": str}

        Reads all engine-shaped knowledge (env key names, load body key, precheck
        type) from ``driver_config`` so that no engine literals appear in the
        caller (ensure_target).

        Returns the activation dict on success, or None if a precheck fails
        (e.g. the requested GGUF path does not exist on disk).

        ExternalDriver / DockerDriver do NOT implement this; supports_swap()
        gates the call so they are never reached in normal operation.
        """
        ...

    async def swap(
        self,
        svc: Mapping[str, Any],
        activation: Mapping[str, Any],
        ctx: Optional[SwapContext] = None,
        warned_models: Optional[set] = None,
    ) -> bool:
        """OPTIONAL — single-slot drivers only.  Make `activation` the loaded model.

        `activation` is the resolved per-model block:
          - systemd (dropin-env-restart): {"env": {KEY: VALUE, ...}}
          - http-load-unload:             {"model_name": str, "params": {...}}

        `ctx` carries the engine-agnostic state needed to short-circuit when
        the target is already loaded, or to skip the restart when pin_loaded
        is set.  When None the driver falls back to its own runtime checks
        (backward-compatible with existing tests that don't pass ctx).

        `warned_models` is the caller-owned set used for warn-once behaviour
        under pin_loaded.  Pass proxy._pin_mismatch_warned (or a test set) so
        the warning fires at most once per model path across calls.  If None a
        fresh set is used (so warnings always fire — safe for isolated tests).

        Returns True iff the engine is serving the target after the swap.
        Raises SwapRecoveryFailed if the swap failed and the driver could not
        put back the previous activation either.
        ExternalDriver / DockerDriver do NOT implement this (no single slot).
        Callers MUST check supports_swap(driver) before calling this method.
        """
        ...

    def swap_required(
        self,
        svc: Mapping[str, Any],
        activation: Mapping[str, Any],
        ctx: SwapContext,
    ) -> bool:
        """OPTIONAL — swap drivers only. Would ``swap(svc, activation, ctx=ctx)``
        restart or reload the engine?

        False when ``swap`` would short-circuit (already loaded) or skip the
        switch (pin_loaded). No side effects. Callers use it to let requests in
        flight finish before a swap interrupts them.
        """
        ...


def supports_swap(driver: Any) -> bool:
    """Return True if the driver implements the optional swap() method.

    Dispatch branches on this flag rather than isinstance/engine literals.
    Only SystemdDriver and HttpLoadUnloadDriver set _SWAPPABLE = True.
    """
    return bool(getattr(driver, "_SWAPPABLE", False))


# ---------------------------------------------------------------------------
# Shared health-check helper
# ---------------------------------------------------------------------------

async def _poll_health(svc: Mapping[str, Any], timeout_s: float) -> bool:
    """Poll the backend health endpoint until it responds 200 or timeout_s elapses.

    Reads host, port, health_endpoint from the svc (config["backends"][id]) dict.
    Polls every 0.5 s.  Returns False immediately if host/port are missing.
    """
    from gateway.engine_contract_view import observations_config

    if observations_config(svc).get("process_ready"):
        from gateway.engine_observations import ObservationReader

        reader = ObservationReader()
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            result = await reader.read(svc, "process_ready")
            if result.known and result.value is True:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(min(0.5, deadline - asyncio.get_running_loop().time()))

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

    def build_activation(
        self,
        svc: Mapping[str, Any],
        *,
        model: str,
        aux: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[dict]:
        """No-op: ExternalDriver does not support swap; returns empty dict."""
        return {}


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
            return ProcessState.UNKNOWN
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
            result = await asyncio.to_thread(
                subprocess.run,
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

    def build_activation(
        self,
        svc: Mapping[str, Any],
        *,
        model: str,
        aux: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[dict]:
        """No-op: DockerDriver does not support swap; returns empty dict."""
        return {}

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
# CommandDriver — drive a service via operator-declared shell commands
# ---------------------------------------------------------------------------

class CommandDriver:
    """Drive a backend via operator-declared start/stop/status commands.

    The OS-agnostic, supervisor-agnostic escape hatch. The operator wraps
    WHATEVER manages their process — systemd, supervisord, s6, runit, PM2, a
    bare ``nohup``, a Windows ``sc``/``nssm``, even ``ssh host 'systemctl ...'``
    for a remote engine — as plain commands in ``driver_config``. This driver
    knows nothing about which supervisor it is talking to; it just runs the
    commands the operator gave it. That is the whole point: it adds zero
    engine- or OS-specific knowledge to the gateway (the binding principle —
    track engines as DATA, never as CODE), yet covers every platform a single
    coupled SystemdDriver could not.

    driver_config keys (all DATA, operator-authored):
      - ``start``  (required): command to start the service. String (parsed with
        shlex) or an argv list. Run only when ``status`` reports not-running
        (or when no ``status`` is given).
      - ``stop``   (optional): command to stop the service. Used by stop().
      - ``status`` (optional): command whose exit code 0 means "running",
        non-zero means "not running". When absent, ensure_ready runs ``start``
        unconditionally (it must be idempotent — most supervisor "start"/"up"
        verbs are) and relies on the health poll for readiness.
      - ``cwd``    (optional): working directory for the commands.
      - ``env``    (optional): dict of extra environment variables (merged over
        the current environment) for the commands.
      - ``timeout_s`` (optional, default 30): per-command timeout.

    Boundary: like ExternalDriver/DockerDriver this is a process-lifecycle
    driver — it does NOT support model swap (no single-slot semantics), so
    build_activation returns {} and supports_swap() is False for it.

    Ownership: the operator (and their supervisor) own the process's existence
    and crash-restart policy. The gateway only drives start/stop/status — it
    never daemonizes, never owns a PID, never resurrects a crashed process. If
    ``start`` backgrounds a process and the gateway exits, that process is the
    supervisor's to manage, exactly as the operator configured.
    """

    _DEFAULT_CMD_TIMEOUT_S: float = 30.0

    def __init__(self, *, commands_allowed: Callable[[], bool] | None = None) -> None:
        from gateway.engine_process import _operator_allows_commands

        self._commands_allowed = commands_allowed or _operator_allows_commands

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        """Start the service (if status says it isn't running) then poll health."""
        if not self._commands_allowed():
            return False
        driver_cfg = self._driver_config(svc)
        if not self._command(driver_cfg, "start"):
            return False

        running = await self._status_ok(driver_cfg)
        if running is False:
            started = await self._run(driver_cfg, "start")
            if not started:
                return False
        # running is True  -> already up, skip start; just poll health.
        # running is None   -> no status command; start already ran above only
        #   if running is False, so when None we DID NOT start. Start now
        #   (idempotently) since we cannot tell — most "start"/"up" verbs are
        #   idempotent, and the health poll is the real readiness gate.
        elif running is None:
            started = await self._run(driver_cfg, "start")
            if not started:
                return False

        return await _poll_health(svc, timeout_s)

    def stop(self, svc: Mapping[str, Any]) -> None:
        """Run the operator's stop command synchronously (best-effort)."""
        if not self._commands_allowed():
            return
        driver_cfg = self._driver_config(svc)
        argv = self._command(driver_cfg, "stop")
        if not argv:
            return
        try:
            subprocess.run(
                argv,
                cwd=driver_cfg.get("cwd") or None,
                env=self._env(driver_cfg),
                timeout=self._timeout(driver_cfg),
                check=False,
                capture_output=True,
            )
        except Exception:
            pass  # best-effort; the operator's supervisor owns the process

    async def status(self, svc: Mapping[str, Any]) -> str:
        if not self._commands_allowed():
            return ProcessState.UNKNOWN
        driver_cfg = self._driver_config(svc)
        running = await self._status_ok(driver_cfg)
        if running is None:
            # No status command — fall back to a health probe for a useful answer.
            ok = await _poll_health(svc, self._DEFAULT_CMD_TIMEOUT_S)
            return ProcessState.RUNNING if ok else ProcessState.UNMANAGED
        if running:
            ok = await _poll_health(svc, 1.0)
            return ProcessState.RUNNING if ok else ProcessState.STARTING
        return ProcessState.STOPPED

    async def health(self, svc: Mapping[str, Any]) -> str:
        ok = await _poll_health(svc, 1.0)
        return HealthState.HEALTHY if ok else HealthState.UNHEALTHY

    async def preflight(self, svc: Mapping[str, Any]) -> Optional[str]:
        """Read-only: confirm a start command is configured."""
        if not self._commands_allowed():
            return "Allow Azizam to run commands on this computer is off"
        driver_cfg = self._driver_config(svc)
        if not self._command(driver_cfg, "start"):
            return "command driver_config.start is required but not set"
        return None

    def build_activation(
        self,
        svc: Mapping[str, Any],
        *,
        model: str,
        aux: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[dict]:
        """No-op: CommandDriver does not support swap; returns empty dict."""
        return {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _driver_config(svc: Mapping[str, Any]) -> Mapping[str, Any]:
        from gateway.engine_contract_view import process_config

        process = process_config(svc)
        if isinstance(svc.get("process"), Mapping) and process.get("driver") == "command":
            return {**process, "_v2": True}
        lifecycle = svc.get("lifecycle") or {}
        if isinstance(lifecycle, Mapping):
            return lifecycle.get("driver_config") or {}
        return {}

    @staticmethod
    def _command(driver_cfg: Mapping[str, Any], key: str) -> list[str]:
        """Resolve a command from driver_config[key] into an argv list.

        Accepts either a string (parsed with shlex.split — POSIX rules) or an
        already-split argv list. Returns [] when the key is absent/empty.
        No shell is ever invoked (shell=False), so operator commands are not
        subject to shell-injection from interpolated values.
        """
        import shlex

        raw = driver_cfg.get(key)
        if not raw:
            return []
        if driver_cfg.get("_v2") and not (
            isinstance(raw, list) and raw and all(
                (isinstance(part, str) and part and "\x00" not in part) or runtime_secrets.secret_refs.is_secret_entry(part)
                for part in raw)
        ):
            return []
        if isinstance(raw, str):
            return shlex.split(raw)
        if isinstance(raw, (list, tuple)):
            try:
                return runtime_secrets.argv(raw)
            except runtime_secrets.SecretUnavailable:
                return []  # a secret that cannot be read means the command is not run, never run with a part missing
        return []

    @classmethod
    def _timeout(cls, driver_cfg: Mapping[str, Any]) -> float:
        try:
            return float(driver_cfg.get("timeout_s") or cls._DEFAULT_CMD_TIMEOUT_S)
        except (TypeError, ValueError):
            return cls._DEFAULT_CMD_TIMEOUT_S

    @staticmethod
    def _env(driver_cfg: Mapping[str, Any]) -> Optional[dict]:
        from gateway.engine_process import inherited_command_environment

        extra = driver_cfg.get("env")
        if not isinstance(extra, Mapping):
            return inherited_command_environment() if driver_cfg.get("_v2") else None
        merged = inherited_command_environment() if driver_cfg.get("_v2") else dict(os.environ)
        merged.update(runtime_secrets.env_mapping(extra))
        return merged

    async def _status_ok(self, driver_cfg: Mapping[str, Any]) -> Optional[bool]:
        """Return True/False if a status command is configured, else None.

        True  = status command exited 0 (running).
        False = status command exited non-zero (not running).
        None  = no status command configured (caller decides).
        """
        argv = self._command(driver_cfg, "status")
        if not argv:
            return None

        def _run_status() -> Optional[bool]:
            try:
                result = subprocess.run(
                    argv,
                    cwd=driver_cfg.get("cwd") or None,
                    env=self._env(driver_cfg),
                    timeout=self._timeout(driver_cfg),
                    check=False,
                    capture_output=True,
                )
                return result.returncode == 0
            except Exception:
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run_status)

    async def _run(self, driver_cfg: Mapping[str, Any], key: str) -> bool:
        """Run the command at driver_config[key] in a thread; True iff exit 0."""
        argv = self._command(driver_cfg, key)
        if not argv:
            return False

        def _exec() -> bool:
            try:
                result = subprocess.run(
                    argv,
                    cwd=driver_cfg.get("cwd") or None,
                    env=self._env(driver_cfg),
                    timeout=self._timeout(driver_cfg),
                    check=False,
                    capture_output=True,
                )
                return result.returncode == 0
            except Exception:
                return False

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _exec)


# ---------------------------------------------------------------------------
# Params-provider registry (option A from LIFECYCLE_DRIVER_PORT.md §3.2)
#
# Maps a string name to a callable (svc) -> dict of extra load-body params.
# The name lives in driver_config.load_body_params (pure data — the driver
# never knows what engine it is talking to).
# ---------------------------------------------------------------------------

PARAMS_PROVIDERS: dict[str, Callable[[Mapping[str, Any]], dict]] = {}


def register_params_provider(name: str, fn: Callable[[Mapping[str, Any]], dict]) -> None:
    """Register a named params-provider for HttpLoadUnloadDriver."""
    PARAMS_PROVIDERS[name] = fn


# ---------------------------------------------------------------------------
# SystemdDriver — dropin-env-restart swap strategy
# ---------------------------------------------------------------------------

class SystemdDriver:
    """Drive a systemd unit the operator already wrote.

    swap()       → write a drop-in env override → daemon-reload → restart → poll;
                   on failure, restore the previous drop-in and restart on it.
    stop()       → systemctl stop <unit>  (only fires when idle_timeout_s > 0).
    ensure_ready → systemctl start <unit> then poll health.

    driver_config keys
    ------------------
    scope            : "user" | "system"  (→ --user flag)
    unit             : operator-written unit name (e.g. "koboldcpp.service")
    dropin           : drop-in filename to write (e.g. "20-model.conf")
    swap_strategy    : must be "dropin-env-restart" (only strategy today)
    health_endpoint  : GET path to poll after restart (e.g. "/v1/models")
    health_timeout_s : poll budget in seconds (default: 60)

    Boundary (§0):
    - NEVER modifies the unit file itself.
    - ONLY writes the single declared drop-in path.
    - NEVER installs, builds, or provisions anything.
    """

    _SWAPPABLE = True
    _SYSTEMCTL_TIMEOUT_S: float = 30.0
    # daemon-reload, restart and start wait for systemd's job; 120 s is longer
    # than systemd's default 90 s start timeout, so only a hung call trips it.
    _SYSTEMCTL_JOB_TIMEOUT_S: float = 120.0
    _DEFAULT_HEALTH_TIMEOUT_S: float = 60.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        """Start the unit (if not running) then poll health."""
        dc = self._driver_config(svc)
        scope_flag = self._scope_flag(dc)
        unit = dc.get("unit", "")
        if not unit:
            return False
        cmd = ["systemctl"] + scope_flag + ["start", unit]
        ok = await self._run_systemctl(cmd)
        if not ok:
            return False
        health_ep = _health_endpoint(dc, "/health")
        svc_for_poll = self._svc_with_health_endpoint(svc, health_ep)
        return await _poll_health(svc_for_poll, timeout_s)

    def stop(self, svc: Mapping[str, Any]) -> None:
        """Stop the unit synchronously (best-effort; no-raise)."""
        dc = self._driver_config(svc)
        scope_flag = self._scope_flag(dc)
        unit = dc.get("unit", "")
        if not unit:
            return
        cmd = ["systemctl"] + scope_flag + ["stop", unit]
        try:
            subprocess.run(
                cmd,
                timeout=self._SYSTEMCTL_TIMEOUT_S,
                check=False,
                capture_output=True,
            )
        except Exception:
            pass

    async def status(self, svc: Mapping[str, Any]) -> str:
        dc = self._driver_config(svc)
        unit = dc.get("unit", "")
        if not unit:
            return ProcessState.UNMANAGED
        cmd = ["systemctl"] + self._scope_flag(dc) + ["is-active", unit]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._SYSTEMCTL_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ProcessState.UNMANAGED
            except asyncio.CancelledError:
                proc.kill()
                await proc.communicate()
                raise
        except asyncio.CancelledError:
            raise
        except Exception:
            return ProcessState.UNMANAGED
        state = stdout.decode(errors="ignore").strip().lower()
        if state == "active":
            return ProcessState.RUNNING
        if state == "activating":
            return ProcessState.STARTING
        if state == "deactivating":
            return ProcessState.DRAINING
        if state == "failed":
            return ProcessState.FAILED
        if state == "inactive":
            return ProcessState.STOPPED
        if state == "unknown":
            return ProcessState.UNMANAGED
        return ProcessState.UNMANAGED

    async def health(self, svc: Mapping[str, Any]) -> str:
        dc = self._driver_config(svc)
        health_ep = _health_endpoint(dc, "/health")
        svc_for_poll = self._svc_with_health_endpoint(svc, health_ep)
        ok = await _poll_health(svc_for_poll, 3.0)
        return HealthState.HEALTHY if ok else HealthState.UNHEALTHY

    async def preflight(self, svc: Mapping[str, Any]) -> Optional[str]:
        dc = self._driver_config(svc)
        unit = dc.get("unit", "")
        if not unit:
            return "systemd driver_config.unit is required but not set"
        return None

    def build_activation(
        self,
        svc: Mapping[str, Any],
        *,
        model: str,
        aux: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[dict]:
        """Build the activation dict for a dropin-env-restart swap from driver_config.

        Reads the env key names from ``driver_config.activation_env_keys``
        (mapping of ``{model: <ENV_VAR>, aux: <ENV_VAR>}``) so that LLAMA_BEE_*
        names are data, not code.

        When ``driver_config.precheck`` is ``"gguf_path"``, validates that the
        model path is an absolute, existing ``.gguf`` file.  Also validates the
        aux path when aux is provided.  Returns None (with a printed message) if
        validation fails — identical behaviour to the legacy ensure_target branch.

        A named profile is resolved only through
        ``driver_config.activation_profiles``. Unknown or malformed profiles
        fail closed. Profile env cannot overwrite the model/aux identity keys.

        Returns ``{"env": {<model_key>: model, <aux_key>: aux or "", ...},
        "profile": profile}`` for named profiles. The legacy no-profile shape
        is preserved for services that do not use profiles.
        """
        dc = self._driver_config(svc)

        # Read env key names from driver_config (pure data — no LLAMA_BEE_* in code).
        activation_env_keys = dc.get("activation_env_keys") or {}
        model_key = str(activation_env_keys.get("model") or "MODEL")
        aux_key = str(activation_env_keys.get("aux") or "AUX")

        # Optional GGUF-path precheck — gates swap when the file is missing.
        precheck = str(dc.get("precheck") or "").strip()
        if precheck == "gguf_path":
            if not (os.path.isabs(model) and model.endswith(".gguf") and os.path.exists(model)):
                print(
                    f"[proxy] ensure_target(kobold): GGUF not found at {model}",
                    flush=True,
                )
                return None
            if aux and not (os.path.isabs(aux) and os.path.exists(aux)):
                print(
                    f"[proxy] ensure_target(kobold): mmproj not found at {aux}",
                    flush=True,
                )
                return None

        env = {model_key: model, aux_key: aux or ""}
        if profile is None:
            return {"env": env}

        profile_name = str(profile).strip()
        profiles = dc.get("activation_profiles")
        profile_env = profiles.get(profile_name) if isinstance(profiles, Mapping) else None
        if not profile_name or not isinstance(profile_env, Mapping):
            print(
                f"[lifecycle] SystemdDriver.build_activation: unknown activation profile "
                f"{profile_name!r}",
                flush=True,
            )
            return None
        if any(not isinstance(key, str) or not key for key in profile_env):
            print(
                f"[lifecycle] SystemdDriver.build_activation: activation profile "
                f"{profile_name!r} contains an invalid environment key",
                flush=True,
            )
            return None
        if model_key in profile_env or aux_key in profile_env:
            print(
                f"[lifecycle] SystemdDriver.build_activation: activation profile "
                f"{profile_name!r} may not override model/aux identity",
                flush=True,
            )
            return None
        if any(not isinstance(value, (str, int, float, bool)) for value in profile_env.values()):
            print(
                f"[lifecycle] SystemdDriver.build_activation: activation profile "
                f"{profile_name!r} contains a non-scalar environment value",
                flush=True,
            )
            return None
        env.update({key: str(value) for key, value in profile_env.items()})
        return {"env": env, "profile": profile_name}

    async def swap(
        self,
        svc: Mapping[str, Any],
        activation: Mapping[str, Any],
        ctx: Optional[SwapContext] = None,
        warned_models: Optional[set] = None,
    ) -> bool:
        """Write drop-in → daemon-reload → restart → poll health.

        activation["env"] must be a dict of {KEY: value} pairs to write as
        Environment="KEY=value" lines in the [Service] drop-in block.

        When `ctx` is supplied, applies the already-loaded short-circuit and
        pin_loaded policy BEFORE writing anything:
          - already loaded  (model + aux both match) → return True immediately.
          - pin_loaded mismatch → warn-once (via warned_models set) + True.
          - mismatch, not pinned → proceed to drop-in write + restart (below).

        Drives the dropin-env-restart swap: write a higher-numbered systemd
        drop-in overriding the activation env keys, daemon-reload, restart the
        unit, then poll health.

        On failure (daemon-reload, restart, or health timeout) returns False
        after putting back the drop-in the unit ran before (or removing it if
        there was none) and, if the unit was already restarted, restarting it
        on that previous activation. The last working model stays loaded. If
        that rollback fails too, raises SwapRecoveryFailed.

        Drop-ins are replaced atomically (a failed write leaves the old bytes).
        Durability across a power loss is best effort: a failed directory fsync
        after the rename is logged, not treated as a failed swap. Once the
        drop-in has changed, the swap runs to its end even if the caller is
        cancelled; the cancellation is raised afterwards. It does not survive
        the gateway process exiting mid-swap. Each systemctl call is bounded by
        _SYSTEMCTL_JOB_TIMEOUT_S, so the swap (and the caller's swap lock) ends
        within about four such calls plus two health timeouts.
        """
        dc = self._driver_config(svc)
        scope_flag = self._scope_flag(dc)
        unit = dc.get("unit", "")
        dropin_name = dc.get("dropin", "20-model.conf")
        health_ep = _health_endpoint(dc, "/health")
        health_timeout = float(dc.get("health_timeout_s", self._DEFAULT_HEALTH_TIMEOUT_S))

        if not unit:
            return False

        dropin_path = self._dropin_path(dc)
        dropin_text = self._dropin_text(activation)

        # --- SwapContext policy (already-loaded short-circuit + pin) ----------
        if ctx is not None:
            target = ctx.target_model
            if self._already_loaded(dropin_path, dropin_text, ctx):
                return True
            if ctx.pin_loaded:
                # Warn once per model path — use the caller-supplied set so
                # the warn fires at most once per model across multiple calls.
                if warned_models is None:
                    warned_models = set()
                if target not in warned_models:
                    warned_models.add(target)
                    loaded = ctx.loaded_model
                    loaded_mmproj = ctx.loaded_aux
                    target_mmproj = ctx.target_aux or None
                    print(
                        f"[proxy] ensure_target pin_loaded_model=true: skipping switch "
                        f"(loaded={loaded}, requested={target}, "
                        f"loaded_mmproj={loaded_mmproj}, requested_mmproj={target_mmproj}); "
                        f"serving with current model",
                        flush=True,
                    )
                return True
        # --- end SwapContext policy -------------------------------------------

        # Keep the activation the unit runs now, so a failed swap can put it back.
        # None means no managed drop-in: the unit runs its base configuration.
        try:
            previous_dropin: Optional[str] = dropin_path.read_text()
        except FileNotFoundError:
            previous_dropin = None
        except OSError as e:
            # Without the old text a failure could not be undone: do not swap.
            print(f"[lifecycle] SystemdDriver.swap: cannot read current drop-in: {e}", flush=True)
            return False

        # Once the drop-in changes, the swap must finish or roll back: a
        # requester that disconnects must not leave the new drop-in on disk
        # with the old engine stopped. Cancellation is re-raised afterwards.
        return await _finish_even_if_cancelled(self._swap_transaction(
            svc, unit, dropin_path, dropin_text, previous_dropin,
            reload_cmd=["systemctl"] + scope_flag + ["daemon-reload"],
            restart_cmd=["systemctl"] + scope_flag + ["restart", unit],
            health_ep=health_ep, health_timeout=health_timeout,
        ))

    async def _swap_transaction(
        self,
        svc: Mapping[str, Any],
        unit: str,
        dropin_path: Path,
        dropin_text: str,
        previous_dropin: Optional[str],
        *,
        reload_cmd: list[str],
        restart_cmd: list[str],
        health_ep: str,
        health_timeout: float,
    ) -> bool:
        """Write the drop-in, daemon-reload, restart, poll health; roll back on failure."""
        try:
            dropin_path.parent.mkdir(parents=True, exist_ok=True)
            _replace_file_atomically(dropin_path, dropin_text)
        except Exception as e:
            print(f"[lifecycle] SystemdDriver.swap: failed to write drop-in (previous one kept): {e}", flush=True)
            return False

        # daemon-reload MUST precede restart.
        if not await self._run_systemctl(reload_cmd):
            await self._restore_after_failed_swap(
                svc, unit, dropin_path, previous_dropin, reload_cmd, None, health_ep, health_timeout,
            )
            return False
        if not await self._run_systemctl(restart_cmd):
            await self._restore_after_failed_swap(
                svc, unit, dropin_path, previous_dropin, reload_cmd, restart_cmd, health_ep, health_timeout,
            )
            return False

        elapsed = await self._wait_until_healthy(svc, health_ep, health_timeout)
        if elapsed is not None:
            print(f"[lifecycle] SystemdDriver.swap complete in {elapsed:.1f}s", flush=True)
            return True

        print(
            f"[lifecycle] SystemdDriver.swap: unit '{unit}' did not become ready within {health_timeout}s",
            flush=True,
        )
        await self._restore_after_failed_swap(
            svc, unit, dropin_path, previous_dropin, reload_cmd, restart_cmd, health_ep, health_timeout,
        )
        return False

    def swap_required(
        self,
        svc: Mapping[str, Any],
        activation: Mapping[str, Any],
        ctx: SwapContext,
    ) -> bool:
        """True when swap() would restart the unit (not loaded, not pinned)."""
        dc = self._driver_config(svc)
        if not dc.get("unit", ""):
            return False
        loaded = self._already_loaded(self._dropin_path(dc), self._dropin_text(activation), ctx)
        return not loaded and not ctx.pin_loaded

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _already_loaded(self, dropin_path: Path, dropin_text: str, ctx: SwapContext) -> bool:
        # Aux match: only compare aux if the activation env carries an aux
        # key — drivers without aux won't have it, so we only
        # factor aux in when the env dict actually contains an aux-shaped key.
        # We detect aux by checking whether loaded_aux / target_aux are
        # meaningful: if the activation.env is empty there's nothing to do
        # anyway, but for correctness we mirror the kobold logic:
        # mismatch = (model mismatch) OR (aux mismatch, normalised to None).
        profile_matches = (
            ctx.target_profile is None
            or self._managed_dropin_matches(dropin_path, dropin_text)
        )
        return (
            ctx.loaded_model == ctx.target_model
            and (ctx.loaded_aux or None) == (ctx.target_aux or None)
            and profile_matches
        )

    async def _wait_until_healthy(
        self, svc: Mapping[str, Any], health_ep: str, health_timeout: float,
    ) -> Optional[float]:
        """Poll the health endpoint; return seconds until 200, or None on timeout."""
        svc_for_poll = self._svc_with_health_endpoint(svc, health_ep)
        start = time.time()
        deadline = start + health_timeout
        interval = 1.0
        while True:
            try:
                import httpx  # local import — same as _poll_health
                async with httpx.AsyncClient(timeout=httpx.Timeout(3)) as client:
                    resp = await client.get(
                        f"http://{svc_for_poll.get('host', '127.0.0.1')}:{svc_for_poll.get('port')}{health_ep}"
                    )
                    if resp.status_code == 200:
                        return time.time() - start
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            await asyncio.sleep(interval)

    async def _restore_after_failed_swap(
        self,
        svc: Mapping[str, Any],
        unit: str,
        dropin_path: Path,
        previous_dropin: Optional[str],
        reload_cmd: list[str],
        restart_cmd: Optional[list[str]],
        health_ep: str,
        health_timeout: float,
    ) -> None:
        """Put back the activation the unit ran before the swap.

        Restores the previous drop-in text (or removes the drop-in if there was
        none), reloads systemd, and, when the failed swap already restarted the
        unit (``restart_cmd`` given), restarts it on the restored activation and
        waits for health. Raises SwapRecoveryFailed if any step fails.
        """

        def recovery_failed(detail: str) -> SwapRecoveryFailed:
            print(f"[lifecycle] SystemdDriver.swap: recovery failed for unit '{unit}': {detail}", flush=True)
            return SwapRecoveryFailed(unit, detail)

        try:
            if previous_dropin is None:
                dropin_path.unlink(missing_ok=True)
            else:
                _replace_file_atomically(dropin_path, previous_dropin)
        except OSError as exc:
            raise recovery_failed(f"cannot restore drop-in {dropin_path}: {exc}") from exc
        if not await self._run_systemctl(reload_cmd):
            raise recovery_failed("daemon-reload after restoring the drop-in failed")
        if restart_cmd is None:
            return
        if not await self._run_systemctl(restart_cmd):
            raise recovery_failed("restart on the previous activation failed")
        if await self._wait_until_healthy(svc, health_ep, health_timeout) is None:
            raise recovery_failed(f"the previous activation did not become ready within {health_timeout}s")
        print("[lifecycle] SystemdDriver.swap: rolled back to the previous activation", flush=True)

    @staticmethod
    def _driver_config(svc: Mapping[str, Any]) -> Mapping[str, Any]:
        lifecycle = svc.get("lifecycle") or {}
        if isinstance(lifecycle, Mapping):
            return lifecycle.get("driver_config") or {}
        return {}

    @staticmethod
    def _dropin_text(activation: Mapping[str, Any]) -> str:
        text = (
            "# Managed by azizam. Do not edit by hand; set role `model:`/`mmproj:` in proxy_config.yml.\n"
        )
        profile = activation.get("profile")
        if profile:
            text += f"# Activation profile: {profile}\n"
        text += "[Service]\n"
        for key, value in (activation.get("env") or {}).items():
            text += f'Environment="{key}={value}"\n'
        return text

    @staticmethod
    def _managed_dropin_matches(path: Path, expected: str) -> bool:
        try:
            return path.read_text() == expected
        except OSError:
            return False

    @staticmethod
    def _scope_flag(dc: Mapping[str, Any]) -> list[str]:
        scope = str(dc.get("scope", "user")).strip().lower()
        return ["--user"] if scope == "user" else []

    @staticmethod
    def _dropin_path(dc: Mapping[str, Any]) -> Path:
        scope = str(dc.get("scope", "user")).strip().lower()
        unit = dc.get("unit", "")
        dropin = dc.get("dropin", "20-model.conf")
        if scope == "user":
            base = Path.home() / ".config" / "systemd" / "user"
        else:
            base = Path("/etc/systemd/system")
        return base / f"{unit}.d" / dropin

    @staticmethod
    def _svc_with_health_endpoint(svc: Mapping[str, Any], health_ep: str) -> dict:
        """Return a copy of svc with health_endpoint set for _poll_health."""
        d = dict(svc)
        d["health_endpoint"] = health_ep
        return d

    async def _run_systemctl(self, cmd: list[str]) -> bool:
        """Run a systemctl command. True on exit code 0; False on failure or timeout.

        A call that runs longer than _SYSTEMCTL_JOB_TIMEOUT_S is killed and treated
        as failed. Killing the client does not cancel a job systemd already
        queued; the rollback that follows queues its own restart.
        """
        proc = None

        async def run() -> bytes:
            nonlocal proc
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            return stderr

        try:
            stderr = await asyncio.wait_for(run(), timeout=self._SYSTEMCTL_JOB_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(
                f"[lifecycle] SystemdDriver: `{' '.join(cmd)}` did not finish within "
                f"{self._SYSTEMCTL_JOB_TIMEOUT_S:.0f}s; treated as failed",
                flush=True,
            )
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return False
        except Exception as e:
            print(f"[lifecycle] SystemdDriver: subprocess error: {e}", flush=True)
            return False
        if proc.returncode != 0:
            print(
                f"[lifecycle] SystemdDriver: `{' '.join(cmd)}` failed: {stderr.decode()}",
                flush=True,
            )
            return False
        return True


# ---------------------------------------------------------------------------
# HttpLoadUnloadDriver — http-load-unload swap strategy
# ---------------------------------------------------------------------------

class HttpLoadUnloadDriver:
    """Drive a model-server with HTTP load/unload endpoints.

    swap()  → GET verify_endpoint → POST unload_url (if loaded) →
              POST load_url with body → confirm via success_marker or verify.
    stop()  → POST unload_url.

    driver_config keys
    ------------------
    load_url         : path to POST to load a model (e.g. "/v1/model/load")
    unload_url       : path to POST to unload (e.g. "/v1/model/unload")
    admin_header     : header name carrying admin credentials (e.g. "x-admin-key")
    load_body_params : name of a registered PARAMS_PROVIDERS entry for extra body params
    load_timeout_s   : timeout for the load POST (default: 300)
    success_marker   : substring in response body indicating success
    verify_endpoint  : GET path to confirm loaded model (e.g. "/v1/model")

    The admin credential value is read from svc["lifecycle"]["admin_key"] or
    the driver_config key "admin_key_env" (env var name).  It is NEVER logged.

    Boundary (§0):
    - NEVER spawns a process.
    - Only calls the operator-declared load/unload/verify endpoints.
    """

    _SWAPPABLE = True
    _DEFAULT_LOAD_TIMEOUT_S: float = 300.0
    _DEFAULT_UNLOAD_TIMEOUT_S: float = 60.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        """Health-check only; load is handled by swap() for single-slot drivers."""
        dc = self._driver_config(svc)
        verify_ep = dc.get("verify_endpoint", "/health")
        svc_for_poll = dict(svc)
        svc_for_poll["health_endpoint"] = verify_ep
        return await _poll_health(svc_for_poll, min(timeout_s, 5.0))

    def stop(self, svc: Mapping[str, Any]) -> None:
        """POST unload synchronously (best-effort)."""
        dc = self._driver_config(svc)
        unload_url = dc.get("unload_url", "")
        if not unload_url:
            return
        base_url = self._base_url(svc)
        admin_key = self._admin_key(svc, dc)
        admin_header = dc.get("admin_header", "x-admin-key")
        try:
            import httpx
            import asyncio as _aio

            async def _do_unload() -> None:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{base_url}{unload_url}",
                        headers={admin_header: admin_key},
                        timeout=self._DEFAULT_UNLOAD_TIMEOUT_S,
                    )

            _aio.get_event_loop().run_until_complete(_do_unload())
        except Exception:
            pass

    async def status(self, svc: Mapping[str, Any]) -> str:
        ok = await self.ensure_ready(svc, 2.0)
        return ProcessState.RUNNING if ok else ProcessState.STOPPED

    async def health(self, svc: Mapping[str, Any]) -> str:
        ok = await self.ensure_ready(svc, 3.0)
        return HealthState.HEALTHY if ok else HealthState.UNHEALTHY

    async def preflight(self, svc: Mapping[str, Any]) -> Optional[str]:
        dc = self._driver_config(svc)
        if not dc.get("load_url"):
            return "http-load-unload driver_config.load_url is required but not set"
        return None

    def build_activation(
        self,
        svc: Mapping[str, Any],
        *,
        model: str,
        aux: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Optional[dict]:
        """Build the activation dict for an http-load-unload swap from driver_config.

        Reads the model body key name from ``driver_config.activation_model_key``
        (defaults to ``"model_name"``).
        No precheck is performed (the server rejects invalid model names itself).

        Returns ``{<model_key>: model}``.
        """
        dc = self._driver_config(svc)
        model_key = str(dc.get("activation_model_key") or "model_name")
        return {model_key: model}

    async def swap(
        self,
        svc: Mapping[str, Any],
        activation: Mapping[str, Any],
        ctx: Optional[SwapContext] = None,
        warned_models: Optional[set] = None,
    ) -> bool:
        """Unload current model (if any) then load target.

        When `ctx` is supplied, uses ctx.loaded_model for the already-loaded
        short-circuit instead of issuing a verify GET (if ctx says it's already
        loaded, skip unload+load when current == target_model).
        HttpLoadUnloadDriver has no aux or pin concept; those fields of ctx are
        ignored.

        Drives the load/unload swap from driver_config and the activation
        {"model_name": ..., "params": {...}}.
        activation["params"] is merged into the load body alongside model_name;
        if absent the driver calls PARAMS_PROVIDERS[load_body_params](svc) live.
        """
        import httpx

        dc = self._driver_config(svc)
        model_key = str(dc.get("activation_model_key") or "model_name")
        target_model = activation.get(model_key, "")
        if not target_model:
            return False

        base_url = self._base_url(svc)
        admin_key = self._admin_key(svc, dc)
        admin_header = dc.get("admin_header", "x-admin-key")
        load_url = dc.get("load_url", "/v1/model/load")
        unload_url = dc.get("unload_url", "/v1/model/unload")
        verify_ep = dc.get("verify_endpoint", "/v1/model")
        load_timeout = float(dc.get("load_timeout_s", self._DEFAULT_LOAD_TIMEOUT_S))
        success_marker = dc.get("success_marker", '"status":"finished"')

        # Get the api key for verify calls — read from svc (mirrors proxy.API_KEY pattern)
        api_key = self._api_key(svc, dc)

        # Step 1: check what's currently loaded.
        # If ctx is supplied, use it directly (no extra GET needed — caller
        # already probed the backend).  Otherwise fall back to the verify GET
        # (backward-compat for callers that don't supply ctx).
        if ctx is not None:
            current = ctx.loaded_model
            if current == target_model:
                return True
        else:
            current = await self._get_loaded_model(base_url, verify_ep, api_key)
            if current == target_model:
                return True

        start = time.time()
        print(f"[lifecycle] HttpLoadUnloadDriver.swap: {current} -> {target_model}", flush=True)

        # Step 2: unload current model if one is loaded.
        if current:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{base_url}{unload_url}",
                        headers={admin_header: admin_key},
                        timeout=self._DEFAULT_UNLOAD_TIMEOUT_S,
                    )
                    if resp.status_code not in (200, 204):
                        print(
                            f"[lifecycle] HttpLoadUnloadDriver.swap: unload returned "
                            f"{resp.status_code}: {resp.text[:200]}",
                            flush=True,
                        )
            except Exception as e:
                print(f"[lifecycle] HttpLoadUnloadDriver.swap: unload error: {e}", flush=True)
                return False

        # Step 3: build load body.
        # activation["params"] takes priority; fall back to params-provider.
        if "params" in activation:
            model_params = dict(activation["params"])
        else:
            load_body_params_key = dc.get("load_body_params", "")
            if load_body_params_key and load_body_params_key in PARAMS_PROVIDERS:
                model_params = PARAMS_PROVIDERS[load_body_params_key](svc)
            else:
                model_params = {}
        # The requested model wins over a stray key in the extra params.
        load_payload = {**model_params, model_key: target_model}

        # Step 4: POST load endpoint, stream the response.
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}{load_url}",
                    headers={admin_header: admin_key, "Content-Type": "application/json"},
                    json=load_payload,
                    timeout=load_timeout,
                )
                response_text = ""
                async for chunk in resp.aiter_bytes():
                    response_text += chunk.decode("utf-8", errors="replace")

                # Step 5a: success via marker.
                if resp.status_code == 200 and success_marker in response_text:
                    elapsed = time.time() - start
                    print(
                        f"[lifecycle] HttpLoadUnloadDriver.swap: success in {elapsed:.1f}s",
                        flush=True,
                    )
                    return True

                # Step 5b: error marker.
                if '"status":"error"' in response_text or '"error":' in response_text:
                    print(
                        f"[lifecycle] HttpLoadUnloadDriver.swap: load reported error: "
                        f"{response_text[-300:]}",
                        flush=True,
                    )
                    return False

                # Step 5c: verify fallback.
                loaded_now = await self._get_loaded_model(base_url, verify_ep, api_key)
                if loaded_now == target_model:
                    elapsed = time.time() - start
                    print(
                        f"[lifecycle] HttpLoadUnloadDriver.swap: success (verified) in {elapsed:.1f}s",
                        flush=True,
                    )
                    return True

                print(
                    f"[lifecycle] HttpLoadUnloadDriver.swap: outcome unclear, "
                    f"loaded={loaded_now}, response={response_text[-200:]}",
                    flush=True,
                )
                return False
        except Exception as e:
            print(f"[lifecycle] HttpLoadUnloadDriver.swap: load error: {e}", flush=True)
            return False

    def swap_required(
        self,
        svc: Mapping[str, Any],
        activation: Mapping[str, Any],
        ctx: SwapContext,
    ) -> bool:
        """True when swap() would unload/load. Like swap(), ignores pin_loaded."""
        dc = self._driver_config(svc)
        model_key = str(dc.get("activation_model_key") or "model_name")
        target_model = activation.get(model_key, "")
        return bool(target_model) and ctx.loaded_model != target_model

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _driver_config(svc: Mapping[str, Any]) -> Mapping[str, Any]:
        lifecycle = svc.get("lifecycle") or {}
        if isinstance(lifecycle, Mapping):
            return lifecycle.get("driver_config") or {}
        return {}

    @staticmethod
    def _base_url(svc: Mapping[str, Any]) -> str:
        host = svc.get("host", "127.0.0.1")
        port = svc.get("port", 80)
        return f"http://{host}:{port}"

    @staticmethod
    def _admin_key(svc: Mapping[str, Any], dc: Mapping[str, Any]) -> str:
        """Resolve the admin credential — from svc lifecycle or an env var name.

        NEVER logs the value.
        """
        lifecycle = svc.get("lifecycle") or {}
        if isinstance(lifecycle, Mapping):
            key = runtime_secrets.scalar(lifecycle, "admin_key")
            if key:
                return key
        env_var = dc.get("admin_key_env", "")
        if env_var:
            return get_env(env_var, "")
        return ""

    @staticmethod
    def _api_key(svc: Mapping[str, Any], dc: Mapping[str, Any]) -> str:
        """Resolve the read-only API key for verify GET calls."""
        lifecycle = svc.get("lifecycle") or {}
        if isinstance(lifecycle, Mapping):
            key = lifecycle.get("api_key", "")
            if key:
                return str(key)
        env_var = dc.get("api_key_env", "")
        if env_var:
            return get_env(env_var, "")
        return ""

    async def _get_loaded_model(self, base_url: str, verify_ep: str, api_key: str) -> Optional[str]:
        """GET verify_endpoint and return the loaded model id, or None."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5)) as client:
                headers = {}
                if api_key:
                    headers["x-api-key"] = api_key
                resp = await client.get(f"{base_url}{verify_ep}", headers=headers)
                if resp.status_code == 200:
                    model_id = resp.json().get("id")
                    return model_id or None
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Driver registry — driver_for_service()
# ---------------------------------------------------------------------------

_DRIVER_MAP: dict[str, Any] = {
    "none":             ExternalDriver,
    "external":         ExternalDriver,
    "docker":           DockerDriver,
    "command":          CommandDriver,
    "systemd":          SystemdDriver,
    "http-load-unload": HttpLoadUnloadDriver,
}

# Module-level singletons (stateless drivers; safe to share across calls)
_external_driver           = ExternalDriver()
_docker_driver             = DockerDriver()
_command_driver            = CommandDriver()
_systemd_driver            = SystemdDriver()
_http_load_unload_driver   = HttpLoadUnloadDriver()
from gateway.engine_process import ProcessDriver
_process_driver = ProcessDriver()


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

    from gateway.engine_contract_view import process_config

    driver_name = str(process_config(bcfg).get("driver") or "none").strip().lower()

    if driver_name == "docker":
        return _docker_driver
    if driver_name == "command":
        return _command_driver
    if driver_name == "process":
        return _process_driver
    if driver_name == "systemd":
        return _systemd_driver
    if driver_name == "http-load-unload":
        return _http_load_unload_driver
    # Default (including "external" and any unrecognised value) → external.
    # Unrecognised values get external so we fail safe rather than crash.
    return _external_driver
