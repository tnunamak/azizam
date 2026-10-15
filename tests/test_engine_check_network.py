"""Add engine: where the check may connect, and how much it may read (security review r1).

Every listener here is a fake on 127.0.0.1 that this test starts; no test
contacts a cloud settings address or another computer. Names are answered by
fake resolvers, and the computer's own lookup is replaced where a test needs
to show which lookup the connection used.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import tracemalloc

import pytest

import proxy
from gateway import engine_check
from gateway.engine_check import blocked_reason, make_http_fetch
from gateway.engine_definitions import load_engine_catalog
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture

PROVENANCE = {"source": "hand-written", "reference": "add-engine-security-review-r1.md"}


def _listener(fakes, exchanges=()):
    """A fake engine on a 127.0.0.1 port. With no exchanges, any request reaching it fails the test."""
    return fakes.loopback(scenario_from_data({"protocol": "engine-probe", "name": "listener", "provenance": PROVENANCE,
                                              "exchanges": list(exchanges)}))


def _port(fake) -> int:
    return int(fake.base_url.rsplit(":", 1)[1])


def _models_ok(**response):
    return {"id": "models", "match": {"method": "GET", "path": "/v1/models"}, "repeat": True, "required": False,
            "response": {"status": 200, "json": {"data": []}, **response}}


def _system_lookup(monkeypatch, answers: dict[str, str]) -> list[str]:
    """Replace the computer's own name lookup for ``answers``; return the names it was asked."""
    original = socket.getaddrinfo
    asked: list[str] = []

    def lookup(host, port, *args, **kwargs):
        if isinstance(host, bytes):  # the async socket layer passes the name encoded
            host = host.decode("idna")
        if isinstance(host, str) and host in answers:
            asked.append(host)
            if answers[host] == "fail":
                raise socket.gaierror(socket.EAI_NONAME, "no such name")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], int(port or 80)))]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    return asked


def _dial_spy(monkeypatch, *, unreachable: str) -> list[tuple[str, int]]:
    """Record every address a connection dials. ``unreachable`` (a documentation address) fails at once."""
    import httpcore

    original = httpcore.AnyIOBackend.connect_tcp
    dialled: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, *args, **kwargs):
        dialled.append((host, port))
        if host == unreachable:
            raise httpcore.ConnectError(f"{host} is a documentation address")
        return await original(self, host, port, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    return dialled


def _fetch(fetch, url, cap=64 * 1024, timeout=1.0):
    return asyncio.run(fetch("GET", url, headers={}, cap_bytes=cap, timeout_s=timeout))


# ---------------------------------------------------------------------------
# Finding 1: a name is looked up once, and only a validated address is dialled
# ---------------------------------------------------------------------------


def test_a_name_that_rebinds_to_the_gateway_after_validation_is_never_connected(fake_upstreams, monkeypatch):
    gateway = _listener(fake_upstreams)
    port = _port(gateway)
    answers = iter([["198.51.100.7"], ["127.0.0.1"]])

    async def resolve(host, port=None):
        return next(answers)

    # The second lookup (the one a connection would make) answers this computer.
    _system_lookup(monkeypatch, {"rebind.test": "127.0.0.1"})
    dialled = _dial_spy(monkeypatch, unreachable="198.51.100.7")
    answer = _fetch(make_http_fetch(resolve=resolve, gateway_ports={port}), f"http://rebind.test:{port}/v1/models")
    assert gateway.requests == [], "the request reached the gateway port through a second lookup"
    assert dialled == [("198.51.100.7", port)], "only the validated address may be dialled"
    assert answer.transport != "ok"


def test_a_name_whose_check_lookup_fails_is_never_connected(fake_upstreams, monkeypatch):
    gateway = _listener(fake_upstreams)
    port = _port(gateway)

    async def resolve(host, port=None):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    _system_lookup(monkeypatch, {"flaky.test": "127.0.0.1"})
    answer = _fetch(make_http_fetch(resolve=resolve, gateway_ports={port}), f"http://flaky.test:{port}/v1/models")
    assert gateway.requests == []
    assert answer.transport == "dns"


def test_the_connection_goes_to_the_validated_address_and_keeps_the_name_in_host(fake_upstreams, monkeypatch):
    engine = _listener(fake_upstreams, [_models_ok()])
    port = _port(engine)

    async def resolve(host, port=None):
        return ["127.0.0.1"] if host == "named.test" else []

    # The computer's own lookup fails: only the check's lookup can find the engine.
    asked = _system_lookup(monkeypatch, {"named.test": "fail"})
    answer = _fetch(make_http_fetch(resolve=resolve, gateway_ports=set()), f"http://named.test:{port}/v1/models")
    assert answer.status == 200, answer.detail
    assert asked == [], "the connection made its own lookup"
    assert engine.requests[0].headers["host"] == f"named.test:{port}"


def test_every_address_a_name_answers_is_checked(fake_upstreams, monkeypatch):
    gateway = _listener(fake_upstreams)
    port = _port(gateway)

    async def resolve(host, port=None):
        return ["198.51.100.7", "127.0.0.1"]

    answer = _fetch(make_http_fetch(resolve=resolve, gateway_ports={port}), f"http://mixed.test:{port}/v1/models")
    assert answer.transport == "blocked"
    assert gateway.requests == []


# ---------------------------------------------------------------------------
# Finding 2: IPv4 written as IPv6 is checked as the IPv4 address it is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://[::ffff:169.254.169.254]/latest",
    "http://[::ffff:a9fe:a9fe]/latest",
    "http://[::ffff:100.100.100.200]/",
    "http://[::169.254.169.254]/latest",
    "http://[::ffff:0:a9fe:a9fe]/latest",
    "http://[64:ff9b::a9fe:a9fe]/latest",
])
def test_ipv4_written_as_ipv6_is_checked_as_ipv4(url):
    reason = asyncio.run(blocked_reason(url))
    assert reason and "link-local or cloud settings" in reason


def test_a_mapped_address_of_this_computer_on_a_gateway_port_is_never_connected(fake_upstreams):
    gateway = _listener(fake_upstreams)
    port = _port(gateway)
    answer = _fetch(make_http_fetch(gateway_ports={port}), f"http://[::ffff:127.0.0.1]:{port}/v1/models")
    assert answer.transport == "blocked" and "the gateway itself" in answer.detail
    assert gateway.requests == []


# ---------------------------------------------------------------------------
# Limits: the gateway's own ports come from its configuration
# ---------------------------------------------------------------------------


def test_the_self_ports_are_read_when_the_check_runs(fake_upstreams):
    gateway = _listener(fake_upstreams)
    port = _port(gateway)
    listening: set[int] = set()
    fetch = make_http_fetch(gateway_ports=lambda: listening)
    listening.add(port)  # configured after the fetcher was built, as a reload would
    answer = _fetch(fetch, f"http://127.0.0.1:{port}/v1/models")
    assert answer.transport == "blocked"
    assert gateway.requests == []


def test_the_gateway_blocks_its_configured_port_and_its_router_port(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_PORT", 18111)

    class Runtime:
        base_url = "http://127.0.0.1:18222"

    monkeypatch.setattr(proxy, "_litellm_runtime_config", lambda: Runtime())
    ports = proxy._engine_check_self_ports()
    assert {18111, 18222} <= ports


# ---------------------------------------------------------------------------
# Limits: the cap counts decoded bytes, and the check asks for no compression
# ---------------------------------------------------------------------------


def test_a_compressed_answer_cannot_grow_past_the_cap(fake_upstreams):
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)
    engine = _listener(fake_upstreams, [{
        "id": "models", "match": {"method": "GET", "path": "/v1/models"}, "repeat": True, "required": False,
        "response": {"status": 200, "headers": {"content-type": "application/json", "content-encoding": "gzip"},
                     "body": [{"hex": bomb.hex()}]},
    }])
    cap = 1024 * 1024
    tracemalloc.start()
    try:
        answer = _fetch(make_http_fetch(gateway_ports=set()), f"{engine.base_url}/v1/models", cap=cap, timeout=10)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert answer.transport == "too_large"
    assert len(answer.body) <= cap
    assert peak < 16 * 1024 * 1024, f"decoding held {peak // (1024 * 1024)} MiB for a {cap // 1024} KB cap"
    assert engine.requests[0].headers["accept-encoding"] == "identity"


def test_a_header_row_cannot_ask_for_compression():
    definition = load_engine_catalog().definition("openai-compatible")
    with pytest.raises(engine_check.DraftError) as refused:
        from gateway.engine_admin import clean_values

        clean_values(definition, {"address": "http://plain.fk.test/v1",
                                  "headers": [{"name": "Accept-Encoding", "value": "gzip"}]})
    assert refused.value.field == "headers"
