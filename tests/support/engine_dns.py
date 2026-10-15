"""A name lookup for Add engine tests: every ``*.fk.test`` name answers one documentation address.

The check looks a name up once and connects only to an address it validated
(gateway/engine_check.py), and a name that does not resolve is refused. The
in-process fakes are routed by name before any connection, so the address is
never dialled; 192.0.2.0/24 (TEST-NET-1) is neither this computer nor
link-local, so the address rules let it through.
"""

from __future__ import annotations

import socket

import pytest

FAKE_ENGINE_ADDRESS = "192.0.2.10"


async def resolve_fake_engines(host: str, port: int | None = None) -> list[str]:
    if host.endswith(".fk.test"):
        return [FAKE_ENGINE_ADDRESS]
    raise socket.gaierror(socket.EAI_NONAME, f"{host} does not resolve in tests")


def use_fake_engine_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gateway's own engine fetcher resolve ``*.fk.test`` names."""
    from gateway import engine_check

    monkeypatch.setattr(engine_check, "_default_resolve", resolve_fake_engines)
