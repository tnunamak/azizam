"""A pass-through engine that is stopped, with a start command that would leave a mark.

The record is injected straight into ``proxy.config["backends"]``, past the registry check that refuses a
pass-through member with a gateway-run process driver. It is the shape a record would have if that check
were bypassed or wrong: ``process.driver: command`` with a ``start`` argv that creates a marker file, a
transition-enabled pass-through residency domain, and a port nothing listens on.

``assert_untouched`` is the proof a request never started the engine: the marker file is absent and
``driver_for_service`` was never asked for a driver.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import proxy
from gateway import lifecycle_drivers

DOMAIN = "stopped-domain"
SERVICE_ID = "stopped-engine"


def closed_port() -> int:
    """A loopback port nothing listens on (bound, read, released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StoppedEngine:
    def __init__(self, monkeypatch: Any, tmp_path: Path, *, service_id: str = SERVICE_ID) -> None:
        self.service_id = service_id
        self.sends: list[str] = []
        self.marker = tmp_path / "started.marker"
        self.port = closed_port()
        self.driver_lookups: list[str] = []
        real = lifecycle_drivers.driver_for_service

        def spy(name: str, config_backends: Any = None):
            self.driver_lookups.append(name)
            return real(name, config_backends)

        monkeypatch.setattr(lifecycle_drivers, "driver_for_service", spy)
        # The start command is allowed to run, so a marker that never appears is not an artefact of the switch.
        monkeypatch.setattr(lifecycle_drivers._command_driver, "_commands_allowed", lambda: True)

    def watch_sends(self, monkeypatch: Any) -> list[str]:
        """Record each httpx request that passed the send-time lease check, i.e. reached the transport.

        A refusal before the send leaves the list empty; a request that reached a closed port is listed once.
        """
        from gateway import engine_egress

        real = engine_egress.check_request
        self.sends = []

        def spy(request: Any) -> None:
            real(request)
            self.sends.append(str(request.url))

        monkeypatch.setattr(engine_egress, "check_request", spy)
        return self.sends

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, *, pass_through: bool = True, **extra: Any) -> dict[str, Any]:
        transition = {"enabled": True, "activation": "pass_through", "admission_wait_s": 0.5} if pass_through else {}
        return {
            "enabled": True, "host": "127.0.0.1", "port": self.port,
            "process": {"driver": "command", "start": ["touch", str(self.marker)]},
            "residency": {"domain": DOMAIN, **({"transition": transition} if transition else {})},
            **extra,
        }

    def install(self, monkeypatch: Any, record: dict[str, Any]) -> None:
        monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), self.service_id: record})

    def assert_untouched(self) -> None:
        assert not self.marker.exists(), "the start command ran: the request started the engine"
        assert self.driver_lookups == [], f"a lifecycle driver was looked up for {self.driver_lookups}"
