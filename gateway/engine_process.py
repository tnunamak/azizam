"""Gateway-owned process groups for owner-authored launch recipes.

Linux uses a new session and tracks the leader's PID plus kernel start tick.
On Windows this driver fails closed until a Job Object implementation can keep
descendants and adopted processes under one ownership boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from gateway.engine_observations import ObservationReader


def inherited_command_environment() -> dict[str, str]:
    """Pass process basics, never the gateway's provider keys or admin tokens."""
    names = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    return {name: os.environ[name] for name in names if name in os.environ}


def _start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


_SUPERVISOR = Path(__file__).resolve().with_name("engine_supervisor.py")


def _argv_digest(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv).encode()).hexdigest()


def _supervised_argv(pid: int) -> list[str] | None:
    """The owner argv a live supervisor was started with, or None if it is not ours."""
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]
        cmdline = [part.decode("utf-8", "surrogateescape") for part in parts]
        return cmdline[2:] if len(cmdline) > 2 and Path(cmdline[1]).resolve() == _SUPERVISOR else None
    except (OSError, ValueError):
        return None


def _may_signal_group(pid: Any) -> bool:
    """Never signal group 0, init's group, or the gateway's own group."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and pid not in {os.getpid(), os.getpgrp()}


def _idle_stop_s(svc: Mapping[str, Any]) -> float | None:
    """Seconds of idleness before the gateway stops the engine; 0 for never; None if invalid."""
    from gateway.engine_contract_view import residency_config

    value = residency_config(svc).get("idle_stop_s")
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
        return None
    return float(value)


def _configuration(service: Mapping[str, Any]) -> Mapping[str, Any]:
    from gateway.engine_contract_view import process_config

    config = process_config(service)
    return config if config.get("driver") == "process" else {}


def _identifier(service: Mapping[str, Any]) -> str:
    value = service.get("id") or service.get("service_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("process service requires a safe immutable id")
    return value


class ProcessDriver:
    """Own, recover, and stop a single declared process group per engine id."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        commands_allowed: Callable[[], bool] | None = None,
        observations: ObservationReader | None = None,
    ) -> None:
        self.state_dir = state_dir or Path(__file__).resolve().parent.parent / "data" / "processes"
        self._commands_allowed = commands_allowed or _operator_allows_commands
        self._observations = observations or ObservationReader()
        self._locks: dict[str, asyncio.Lock] = {}
        self._children: dict[str, subprocess.Popen] = {}
        self._services: dict[str, Mapping[str, Any]] = {}
        self._starts: dict[str, deque[float]] = {}
        self._last_use: dict[str, float] = {}
        self._active: dict[str, int] = {}
        self._idle_tasks: dict[str, asyncio.Task] = {}
        self._unhealthy: set[str] = set()

    async def ensure_ready(self, svc: Mapping[str, Any], timeout_s: float) -> bool:
        if not self._commands_allowed() or os.name != "posix" or not Path("/proc").exists():
            return False
        try:
            ident = _identifier(svc)
            cfg, argv, port = self._validated(svc)
        except ValueError:
            return False
        lock = self._locks.setdefault(ident, asyncio.Lock())
        async with lock:
            if self._live_identity(ident, port, argv):
                self._services[ident] = svc
                ready = await self._ready(svc, float(cfg.get("ready_timeout_s", timeout_s)))
                if ready:
                    self._unhealthy.discard(ident)
                    self._schedule_idle(ident, svc)
                    return True
                # A failed probe on a live engine is a health fact, not a start
                # failure. Requests in flight keep their process; only a drained
                # engine is replaced, and the restart budget still applies.
                self._unhealthy.add(ident)
                if self._active.get(ident, 0) > 0:
                    return False
                await self._stop_async(svc, reset_budget=False)
            elif self._identity_matches(self._read_state(ident)):
                await self._stop_async(svc, reset_budget=False)
            address = (svc.get("connection") or {}).get("address") or f"http://{svc.get('host', '127.0.0.1')}:{port}"
            if self._port_in_use(urlsplit(address).hostname or "127.0.0.1", port):
                return False
            if not self._restart_available(ident, cfg):
                return False
            await self._backoff(ident, cfg)
            if not self._trusted_state_dir(create=True):
                return False
            try:
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("engine_supervisor.py")), *argv],
                    cwd=cfg.get("cwd") or None, env=self._environment(cfg),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, shell=False,
                )
            except (OSError, ValueError):
                return False
            self._children[ident] = child
            self._services[ident] = svc
            start = _start_time(child.pid)
            if start is None:
                await self._stop_async(svc, reset_budget=True)
                return False
            self._write_state(ident, child.pid, start, port, argv)
            threading.Thread(target=self._copy_logs, args=(ident, child, cfg), daemon=True).start()
            # A waiter that gives up does not own the start. The process keeps
            # running, and the next request probes it like any live engine.
            ready = await self._ready(svc, float(cfg.get("ready_timeout_s", timeout_s)), ident)
            if not ready:
                await self._stop_async(svc, reset_budget=False)
                return False
            self._schedule_idle(ident, svc)
            return True

    def stop(self, svc: Mapping[str, Any]) -> None:
        self._stop(svc, reset_budget=True)

    def _stop(self, svc: Mapping[str, Any], *, reset_budget: bool) -> None:
        parts = self._prepare_stop(svc)
        if parts is None:
            return
        ident, pid, timeout_s, child = parts
        try:
            self._terminate_group(pid, timeout_s, child)
        finally:
            self._finish_stop(ident, reset_budget=reset_budget)

    async def _stop_async(self, svc: Mapping[str, Any], *, reset_budget: bool) -> None:
        parts = self._prepare_stop(svc)
        if parts is None:
            return
        ident, pid, timeout_s, child = parts
        from gateway.lifecycle_drivers import _finish_even_if_cancelled

        try:
            await _finish_even_if_cancelled(asyncio.to_thread(self._terminate_group, pid, timeout_s, child))
        finally:
            self._finish_stop(ident, reset_budget=reset_budget)

    def _prepare_stop(self, svc: Mapping[str, Any]) -> tuple[str, int, float, subprocess.Popen | None] | None:
        try:
            ident = _identifier(svc)
        except ValueError:
            return None
        task = self._idle_tasks.pop(ident, None)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task and task is not current:
            task.cancel()
        state = self._read_state(ident)
        if not state or not self._identity_matches(state):
            self._remove_state(ident)
            self._services.pop(ident, None)
            return None
        pid = state["pid"]
        cfg = _configuration(svc)
        timeout_s = max(0.01, min(float(cfg.get("stop_timeout_s", 5)), 30))
        return ident, pid, timeout_s, self._children.get(ident)

    def _terminate_group(self, pid: int, timeout_s: float, child: subprocess.Popen | None) -> None:
        if not _may_signal_group(pid):
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        deadline = time.monotonic() + timeout_s
        while self._group_alive(pid) and time.monotonic() < deadline:
            if child:
                child.poll()
            time.sleep(0.01)
        if self._group_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if child:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    def _finish_stop(self, ident: str, *, reset_budget: bool) -> None:
        self._children.pop(ident, None)
        self._remove_state(ident)
        self._services.pop(ident, None)
        if reset_budget:
            self._starts.pop(ident, None)

    def shutdown(self) -> None:
        """Stop only processes this gateway instance started or adopted."""
        for service in list(self._services.values()):
            self.stop(service)

    def recover(self, services: Mapping[str, Mapping[str, Any]]) -> None:
        """At boot, adopt matching process groups; terminate stale recipes.

        Recovery never launches a process. A PID whose kernel start tick no
        longer matches is forgotten without sending a signal.
        """
        if not self._trusted_state_dir():
            return
        for path in self.state_dir.glob("*.json"):
            ident = path.stem
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", ident):
                continue
            state = self._read_state(ident)
            if not self._identity_matches(state):
                self._remove_state(ident)
                continue
            svc = {**(services.get(ident) or {}), "id": ident}
            cfg = _configuration(svc)
            try:
                validated = self._validated(svc)
            except ValueError:
                validated = None
            if self._commands_allowed() and cfg and validated and self._live_identity(
                ident, validated[2], validated[1]
            ):
                self._services[ident] = svc
                continue
            self._stop(svc, reset_budget=False)

    async def status(self, svc: Mapping[str, Any]) -> str:
        try:
            ident = _identifier(svc)
        except ValueError:
            return "unknown"
        return "running" if self._identity_matches(self._read_state(ident)) else "stopped"

    async def health(self, svc: Mapping[str, Any]) -> str:
        if await self.status(svc) != "running":
            return "unknown"
        result = await self._observations.read(self._observation_service(svc), "process_ready")
        return "healthy" if result.known and result.value is True else "unknown"

    async def preflight(self, svc: Mapping[str, Any]) -> str | None:
        if not self._commands_allowed():
            return "Allow Azizam to run commands on this computer is off"
        try:
            self._validated(svc)
        except ValueError as exc:
            return str(exc)
        return None

    def build_activation(self, svc: Mapping[str, Any], *, model: str, aux=None, profile=None) -> dict:
        return {}

    def request_started(self, svc: Mapping[str, Any]) -> None:
        ident = _identifier(svc)
        self._active[ident] = self._active.get(ident, 0) + 1
        self._last_use[ident] = time.monotonic()

    def request_finished(self, svc: Mapping[str, Any]) -> None:
        ident = _identifier(svc)
        self._active[ident] = max(0, self._active.get(ident, 0) - 1)
        self._last_use[ident] = time.monotonic()
        self._schedule_idle(ident, svc)

    def _validated(self, svc: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[str], int]:
        from gateway.engine_contract_view import observations_config

        cfg = _configuration(svc)
        argv = cfg.get("start")
        port_spec = cfg.get("port")
        port = port_spec.get("fixed") if isinstance(port_spec, Mapping) else None
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\x00" in x for x in argv):
            raise ValueError("process.start must be a nonempty argv array")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("process.port.fixed must be a declared TCP port")
        address = (svc.get("connection") or {}).get("address")
        if address is None and svc.get("port") == port:
            address = f"http://{svc.get('host', '127.0.0.1')}:{port}"
        parsed = urlsplit(address) if isinstance(address, str) else None
        if parsed is None or parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or parsed.port != port:
            raise ValueError("process port must match connection address")
        if not isinstance(observations_config(svc).get("process_ready"), Mapping):
            raise ValueError("process_ready observation is required for owned process readiness")
        if _idle_stop_s(svc) is None:
            raise ValueError("residency.idle_stop_s must be a non-negative number")
        return cfg, argv, port

    async def _ready(self, svc: Mapping[str, Any], timeout_s: float, ident: str | None = None) -> bool:
        deadline = time.monotonic() + max(0, timeout_s)
        observed_svc = self._observation_service(svc)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                result = await asyncio.wait_for(
                    self._observations.read(observed_svc, "process_ready"), timeout=remaining
                )
            except asyncio.TimeoutError:
                return False
            if result.known and result.value is True:
                return ident is None or self._identity_matches(self._read_state(ident))
            if ident is not None and not self._identity_matches(self._read_state(ident)):
                return False
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))

    @staticmethod
    def _port_in_use(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.05):
                return True
        except OSError:
            return False

    @staticmethod
    def _group_alive(pid: int) -> bool:
        if not _may_signal_group(pid):
            return False
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _observation_service(svc: Mapping[str, Any]) -> Mapping[str, Any]:
        from gateway.engine_contract_view import observations_config

        connection = svc.get("connection") or (svc.get("config") or {}).get("connection")
        if not connection:
            connection = {"address": f"http://{svc.get('host', '127.0.0.1')}:{svc.get('port')}"}
        return {**svc, "connection": connection, "observations": observations_config(svc)}

    def _state_path(self, ident: str) -> Path:
        return self.state_dir / f"{ident}.json"

    def _trusted_state_dir(self, *, create: bool = False) -> bool:
        """The state directory is ours alone: not a link, our uid, mode 0700."""
        path = self.state_dir
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if not create:
                return False
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
            info = os.lstat(path)
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return False
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
        return True

    def _read_state(self, ident: str) -> dict | None:
        path = self._state_path(ident)
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            state = json.loads(path.read_text())
            return state if isinstance(state, dict) else None
        except (OSError, ValueError):
            return None

    def _write_state(self, ident: str, pid: int, start: str, port: int, argv: list[str]) -> None:
        path = self._state_path(ident)
        payload = {"pid": pid, "start_time": start, "port": port, "argv_sha256": _argv_digest(argv)}
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def _remove_state(self, ident: str) -> None:
        try:
            self._state_path(ident).unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _identity_matches(state: dict | None) -> bool:
        """A state file is a claim. Believe it only when the kernel agrees."""
        if not isinstance(state, dict):
            return False
        pid, start = state.get("pid"), state.get("start_time")
        if not _may_signal_group(pid) or not isinstance(start, str) or not start:
            return False
        if _start_time(pid) != start:
            return False
        try:
            if os.getpgid(pid) != pid:
                return False
        except OSError:
            return False
        argv = _supervised_argv(pid)
        return argv is not None and state.get("argv_sha256") == _argv_digest(argv)

    def _live_identity(self, ident: str, port: int, argv: list[str]) -> bool:
        state = self._read_state(ident)
        if not self._identity_matches(state):
            self._remove_state(ident)
            return False
        if state.get("port") != port or state.get("argv_sha256") != _argv_digest(argv):
            return False
        return True

    def _restart_available(self, ident: str, cfg: Mapping[str, Any]) -> bool:
        policy = cfg.get("restart") or {}
        maximum = max(1, int(policy.get("max_attempts", 3)))
        window = max(1, float(policy.get("window_s", 60)))
        starts = self._starts.setdefault(ident, deque())
        now = time.monotonic()
        while starts and starts[0] <= now - window:
            starts.popleft()
        if len(starts) >= maximum:
            return False
        starts.append(now)
        return True

    async def _backoff(self, ident: str, cfg: Mapping[str, Any]) -> None:
        starts = self._starts[ident]
        if len(starts) > 1:
            policy = cfg.get("restart") or {}
            base = max(0, float(policy.get("backoff_s", 1)))
            await asyncio.sleep(min(base * 2 ** (len(starts) - 2), 30))

    @staticmethod
    def _environment(cfg: Mapping[str, Any]) -> dict[str, str]:
        env = inherited_command_environment()
        extra = cfg.get("env") or {}
        if not isinstance(extra, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in extra.items()):
            raise ValueError("process.env must be a string map")
        env.update(extra)
        return env

    def _copy_logs(self, ident: str, child: subprocess.Popen, cfg: Mapping[str, Any]) -> None:
        limit = max(1024, min(int(cfg.get("log_max_bytes", 65536)), 1048576))
        log_path = self.state_dir / f"{ident}.log"
        secrets = [value.encode() for value in (cfg.get("env") or {}).values() if isinstance(value, str) and len(value) >= 4]
        if child.stdout is None:
            return
        while True:
            line = child.stdout.readline(4096)
            if not line:
                return
            if len(line) == 4096 and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = child.stdout.readline(4096)
                line = b"[oversized log line omitted]\n"
            for secret in secrets:
                line = line.replace(secret, b"[redacted]")
            line = re.sub(rb"\b(?:opk_|oak_)[A-Za-z0-9_-]+", b"[redacted]", line)
            line = line[:limit]
            try:
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "ab") as output:
                    if output.tell() + len(line) > limit:
                        output.seek(0)
                        output.truncate()
                    output.write(line)
            except OSError:
                return

    def _schedule_idle(self, ident: str, svc: Mapping[str, Any]) -> None:
        from gateway.engine_contract_view import residency_config

        # Called from request release hooks, possibly outside the event loop.
        # A bad value or a missing loop means no idle stop, never an exception.
        delay = _idle_stop_s(svc)
        if residency_config(svc).get("idle_owner") != "gateway" or not delay:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        old = self._idle_tasks.pop(ident, None)
        if old and old is not asyncio.current_task():
            old.cancel()
        self._last_use.setdefault(ident, time.monotonic())

        async def idle_stop() -> None:
            await asyncio.sleep(delay)
            lock = self._locks.setdefault(ident, asyncio.Lock())
            async with lock:
                if self._active.get(ident, 0) == 0 and time.monotonic() - self._last_use[ident] >= delay:
                    await self._stop_async(svc, reset_budget=True)

        self._idle_tasks[ident] = loop.create_task(idle_stop())


def _operator_allows_commands() -> bool:
    from gateway import admin_store

    try:
        setting = admin_store.get_operator_setting("allow_host_commands")
        return bool(setting and setting.get("value", {}).get("enabled") is True)
    except Exception:  # setting store unavailable: host execution stays off
        return False
