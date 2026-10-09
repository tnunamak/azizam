"""A fake engine behind a systemd unit, for SystemdDriver swap tests.

It stands in for one llama.cpp process run by a unit with a managed drop-in:
``systemctl restart`` reloads the engine from the drop-in on disk and kills
every stream it is serving at that moment. It grew out of the lane-4
throwaway ``~/.tmp/gpu-lane-internal/swap_inflight_fake.py``.

``install_fake_engine`` points HOME at a temp dir (the drop-in path resolves
under ``Path.home()``) and replaces ``asyncio.create_subprocess_exec`` and
``httpx.AsyncClient`` (the driver's health poll). Nothing runs systemctl or
opens a socket.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from gateway import lifecycle_drivers as ld

MODEL = "/models/fake.gguf"
UNIT = "fake-llm.service"
DROPIN = "95-activation.conf"


def fake_svc(*, health_timeout_s: float = 1.0) -> dict:
    """A swap-capable service record with two activation profiles."""
    return {
        "host": "127.0.0.1",
        "port": 59999,
        "pin_loaded_model": False,
        "lifecycle": {
            "driver": "systemd",
            "driver_config": {
                "scope": "user",
                "unit": UNIT,
                "dropin": DROPIN,
                "health_endpoint": "/v1/models",
                "health_timeout_s": health_timeout_s,
                "activation_env_keys": {"model": "FAKE_MODEL", "aux": "FAKE_AUX"},
                "activation_profiles": {
                    "p-a": {"FAKE_CTX": 1000},
                    "p-b": {"FAKE_CTX": 2000},
                },
            },
        },
    }


def dropin_text(profile: str) -> str:
    driver = ld.SystemdDriver()
    activation = driver.build_activation(fake_svc(), model=MODEL, profile=profile)
    assert activation is not None
    return driver._dropin_text(activation)


class _Proc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b"" if self.returncode == 0 else b"fake failure"


class FakeEngine:
    """One engine process behind a systemd unit, driven through systemctl."""

    def __init__(self, dropin: Path) -> None:
        self.dropin = dropin
        self.events: list[tuple] = []
        self.streams: set["UpstreamStream"] = set()
        self.killed: list[str] = []
        self.loaded = self._profile_on_disk()
        self.restart_codes: list[int] = []  # popped per restart; empty = success
        self.reload_codes: list[int] = []  # popped per daemon-reload; empty = success
        self.unhealthy_profiles: set[str] = set()
        self.restart_gate: asyncio.Event | None = None
        self.reload_gate: asyncio.Event | None = None

    def _profile_on_disk(self) -> str:
        try:
            text = self.dropin.read_text()
        except FileNotFoundError:
            return "base-unit"
        for line in text.splitlines():
            if line.startswith("# Activation profile: "):
                return line.removeprefix("# Activation profile: ").strip()
        return "unknown"

    async def exec(self, *cmd: str, **_kwargs: Any) -> _Proc:
        verb = cmd[2] if cmd[1] == "--user" else cmd[1]
        if verb == "daemon-reload":
            if self.reload_gate is not None:
                await self.reload_gate.wait()
            code = self.reload_codes.pop(0) if self.reload_codes else 0
            self.events.append(("daemon-reload", code))
            return _Proc(code)
        if verb == "restart":
            if self.restart_gate is not None:
                await self.restart_gate.wait()
            for stream in list(self.streams):
                stream.kill()
            code = self.restart_codes.pop(0) if self.restart_codes else 0
            self.loaded = self._profile_on_disk() if code == 0 else "down"
            self.events.append(("restart", self.loaded, code))
            return _Proc(code)
        self.events.append((verb,))
        return _Proc(0)

    def healthy(self) -> bool:
        return self.loaded not in {"down", *self.unhealthy_profiles}

    def restarts(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "restart"]

    def serve(self, name: str) -> "UpstreamStream":
        stream = UpstreamStream(self, name)
        self.streams.add(stream)
        return stream


class UpstreamStream:
    """One generation in flight on the fake engine."""

    def __init__(self, engine: FakeEngine, name: str) -> None:
        self.engine = engine
        self.name = name
        self.finish = asyncio.Event()
        self.was_killed = False

    def kill(self) -> None:
        self.was_killed = True
        self.engine.killed.append(self.name)
        self.engine.streams.discard(self)
        self.finish.set()

    def done(self) -> None:
        self.engine.streams.discard(self)
        self.finish.set()


class _HealthClient:
    engine: FakeEngine

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_HealthClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def get(self, url: str, **_kwargs: Any) -> Any:
        if not self.engine.healthy():
            raise httpx.ConnectError("engine down")

        class _Ok:
            status_code = 200

        return _Ok()


def install_fake_engine(monkeypatch: Any, home: Path, *, profile: str = "p-a") -> FakeEngine:
    """Run the unit on ``profile`` (its drop-in on disk) and route systemctl to the fake."""
    monkeypatch.setenv("HOME", str(home))
    dropin = home / ".config" / "systemd" / "user" / f"{UNIT}.d" / DROPIN
    dropin.parent.mkdir(parents=True)
    dropin.write_text(dropin_text(profile))
    engine = FakeEngine(dropin)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", engine.exec)
    monkeypatch.setattr(httpx, "AsyncClient", type("HealthClient", (_HealthClient,), {"engine": engine}))
    return engine
