"""A failed connection is judged by its cause, the same way in both health probes.

Only a refused connection means nothing listens on the port. The owner status grid and the model
catalog used to read every failure to connect as refused. A name that does not resolve or a broken
TLS handshake therefore hid an on-demand engine as idle, and a resident engine's last error said
"connect refused" when that was not what happened. These tests make a real refused connection, a
name that does not resolve and a failed TLS handshake, against a resident and an on-demand engine,
and pin the verdict of each probe.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl

import httpcore
import httpx
import pytest

import proxy
import status_endpoint
from gateway.models_catalog import connect_failure_kind
from tests.support.fake_upstream.loopback import LoopbackServer

# ".invalid" is reserved and never resolves (RFC 2606), so the lookup fails without touching a real host.
DNS_NAME = "no-such-host.invalid"

# Each probe's verdict for each failure: (catalog status, catalog available, grid status, grid last_error).
VERDICTS = {
    ("refused", "resident"): ("unavailable", False, "down", "connect refused"),
    ("refused", "on_demand"): ("stopped", None, "stopped", None),
    ("dns", "resident"): ("unavailable", False, "down", "DNS lookup failed"),
    ("dns", "on_demand"): ("unavailable", False, "down", "DNS lookup failed"),
    ("tls", "resident"): ("unavailable", False, "down", "TLS handshake failed"),
    ("tls", "on_demand"): ("unavailable", False, "down", "TLS handshake failed"),
}


async def _never_answers(scope, receive, send):
    raise AssertionError("the connection must fail before any HTTP exchange")


@contextlib.contextmanager
def _unreachable(kind: str):
    """A health URL whose connection fails in one way: refused, a name that does not resolve, or TLS."""
    if kind == "refused":
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # The probe socket is closed now, so nothing listens on this port.
        yield f"http://127.0.0.1:{port}/health"
    elif kind == "dns":
        yield f"http://{DNS_NAME}:8080/health"
    else:
        server = LoopbackServer(_never_answers).start()
        try:
            # The server speaks plain HTTP, so the TLS handshake fails before any request is sent.
            yield f"https://127.0.0.1:{server.port}/health"
        finally:
            server.stop()


def _registry(residency: str) -> dict:
    config = {"residency": "on_demand"} if residency == "on_demand" else {}
    return {"services": {"engine": {"id": "engine", "enabled": True, "kind": "provider", "name": "Engine",
                                    "config": config}}}


def _catalog_evidence(monkeypatch, url: str, residency: str) -> dict:
    monkeypatch.setattr(proxy, "_runtime_health_endpoint", lambda service: url)
    monkeypatch.setattr(proxy, "_HEALTH_EVIDENCE_CACHE", {})
    evidence = asyncio.run(proxy._models_catalog_health_evidence(_registry(residency), service_ids=["engine"]))
    return evidence["engine"]


def _grid_row(url: str, residency: str) -> dict:
    engine = {"name": "Engine", "health_endpoint": url, "role": "standby"}
    if residency == "on_demand":
        engine["residency"] = "on_demand"
    (row,) = asyncio.run(status_endpoint._backend_grid({"backends": {"engine": engine}}))
    return row


@pytest.mark.parametrize("residency", ["resident", "on_demand"])
@pytest.mark.parametrize("kind", ["refused", "dns", "tls"])
def test_each_probe_judges_a_failed_connection_by_its_cause(kind, residency, monkeypatch):
    catalog_status, catalog_available, grid_status, grid_error = VERDICTS[(kind, residency)]

    with _unreachable(kind) as url:
        catalog = _catalog_evidence(monkeypatch, url, residency)
        grid = _grid_row(url, residency)

    assert (catalog["status"], catalog["available"]) == (catalog_status, catalog_available)
    assert grid["status"] == grid_status
    assert grid.get("last_error") == grid_error


@pytest.mark.parametrize(("cause", "kind"), [
    (ConnectionRefusedError(111, "Connection refused"), "refused"),
    (socket.gaierror(-2, "Name or service not known"), "dns"),
    (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "tls"),
    (OSError(113, "No route to host"), "other"),
])
def test_connect_failure_kind_reads_the_socket_error_under_httpx(cause, kind):
    # httpx wraps httpcore, which wraps the socket or TLS error: the chain is three deep in practice.
    inner = httpcore.ConnectError("connect failed")
    inner.__cause__ = cause
    outer = httpx.ConnectError("connect failed")
    outer.__cause__ = inner

    assert connect_failure_kind(outer) == kind
