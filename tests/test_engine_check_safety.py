"""Add engine: the check's safety rules (spec section 5).

No redirects, a timeout, a size cap (higher for the API description and the
node catalog), GET or empty-body POST only, a fresh made-up path per check,
nothing to link-local or cloud settings addresses, and nothing to the
gateway's own ports on this computer.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from gateway import engine_check
from gateway.engine_check import KeyInput, blocked_reason, make_http_fetch, run_check
from gateway.engine_definitions import load_engine_catalog
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture

TOKEN = "t0k3n"


async def _no_dns(host: str) -> list[str]:
    raise OSError("fake hosts do not resolve")


def _check(address: str, *, fetch=None, definition: str = "openai-compatible", **values):
    catalog = load_engine_catalog()
    return asyncio.run(run_check(catalog, catalog.definition(definition), {"address": address, **values}, key=KeyInput(),
                                 fetch=fetch or make_http_fetch(resolve=_no_dns), baseline_token=TOKEN))


@pytest.mark.parametrize("address", [
    "http://127.0.0.1:5000/v1",
    "http://localhost:15000",
    "http://[::1]:5000",
    "http://0.0.0.0:5000",
])
def test_the_gateways_own_ports_on_this_computer_are_never_called(address):
    async def resolve(host: str) -> list[str]:
        return ["127.0.0.1"]
    result = _check(address, fetch=make_http_fetch(resolve=resolve))
    assert result["outcome"] == "error"
    finding = result["findings"][0]
    assert finding["code"] == "blocked" and finding["field"] == "address"
    assert "the gateway itself" in finding["why"]
    # Blocked before any connection: an attempted request would read refused or answered.
    assert "details" in finding and finding["details"]["raw"].endswith("so Azizam does not call it.")


@pytest.mark.parametrize("address", ["http://169.254.169.254/latest", "http://[fe80::1]:8080", "http://[fd00:ec2::254]"])
def test_link_local_and_cloud_settings_addresses_are_never_called(address):
    result = _check(address)
    assert result["findings"][0]["code"] == "blocked"
    assert "link-local or cloud settings" in result["findings"][0]["why"]


def test_a_name_that_resolves_to_a_metadata_address_is_blocked():
    async def resolve(host: str) -> list[str]:
        return ["169.254.169.254"]
    reason = asyncio.run(blocked_reason("http://innocent.example:8080/v1", resolve=resolve))
    assert reason and "link-local" in reason


def test_this_computer_is_known_by_binding_not_by_its_name():
    import ipaddress
    assert engine_check.is_this_computer(ipaddress.ip_address("127.0.0.1"))
    assert engine_check.is_this_computer(ipaddress.ip_address("0.0.0.0"))
    assert not engine_check.is_this_computer(ipaddress.ip_address("192.0.2.77"))


def test_the_port_this_gateway_listens_on_is_blocked_too():
    async def resolve(host: str) -> list[str]:
        return ["127.0.0.1"]
    reason = asyncio.run(blocked_reason("http://localhost:18080/v1", resolve=resolve, gateway_ports={5000, 15000, 18080}))
    assert reason and "the gateway itself" in reason


def test_port_5000_on_another_computer_is_not_blocked():
    async def resolve(host: str) -> list[str]:
        return ["192.0.2.44"]
    assert asyncio.run(blocked_reason("http://lan-box.example:5000", resolve=resolve)) is None


def test_a_redirect_is_reported_and_not_followed(fake_upstreams):
    fake = fake_upstreams.asgi(load_scenario("engine-probe/one-answer", token=TOKEN, status=302, body={},
                                             headers={"location": "http://elsewhere.fk.test/v1/models"}, wait=0),
                               host="redirecting.fk.test")
    result = _check(f"{fake.base_url}/v1")
    finding = result["findings"][0]
    assert finding["code"] == "redirect"
    assert "http://elsewhere.fk.test/v1/models" in finding["what"]
    assert all("elsewhere" not in r.path for r in fake.requests)


def test_a_model_list_over_1_mb_is_cut_and_reported(fake_upstreams):
    big = {"object": "list", "data": [{"id": f"model-{i}", "description": "x" * 400} for i in range(2800)]}
    assert len(json.dumps(big)) > 1024 * 1024
    fake = fake_upstreams.asgi(load_scenario("engine-probe/one-answer", token=TOKEN, status=200, body=big, headers={}, wait=0),
                               host="big.fk.test")
    result = _check(f"{fake.base_url}/v1")
    assert result["findings"][0]["code"] == "too_large"


def test_a_long_model_list_from_a_server_that_fronts_many_models_is_read(fake_upstreams):
    long = {"object": "list", "data": [{"id": f"vendor/model-{i}", "description": "x" * 300} for i in range(400)]}
    assert 64 * 1024 < len(json.dumps(long)) < 1024 * 1024
    fake = fake_upstreams.asgi(load_scenario("engine-probe/one-answer", token=TOKEN, status=200, body=long, headers={}, wait=0),
                               host="long.fk.test")
    result = _check(f"{fake.base_url}/v1")
    assert len(result["models"]) == 400
    assert all(f["code"] != "too_large" for f in result["findings"])


def test_a_server_that_does_not_answer_in_time_is_unreachable(fake_upstreams, monkeypatch):
    # The check's own limit (5 s) is not a setting; shorten it so the test waits 1 s.
    monkeypatch.setattr(engine_check, "DEFAULT_TIMEOUT_S", 1.0)
    fake = fake_upstreams.loopback(load_scenario("engine-probe/one-answer", token=TOKEN, status=200, body={"data": []}, headers={}, wait="forever"))
    result = _check(f"{fake.base_url}/v1", fetch=make_http_fetch(resolve=_no_dns))
    finding = result["findings"][0]
    assert finding["code"] == "down"
    assert "within 1 second." in finding["why"]
    assert "--max-time 5" in finding["details"]["request"]


def test_a_closed_port_is_unreachable_with_what_why_and_fix():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    result = _check(f"http://127.0.0.1:{port}/v1")
    finding = result["findings"][0]
    assert finding["code"] == "down" and finding["keep_off"] is True
    assert finding["what"] == "Azizam could not reach the server."
    assert "localhost" in finding["fix"]
    assert finding["details"]["raw"].startswith(f"GET http://127.0.0.1:{port}/v1/models")


def test_a_key_a_request_cannot_carry_is_not_sent():
    answer = asyncio.run(make_http_fetch(resolve=_no_dns)("GET", "http://192.0.2.9:8080/v1/models",
                                                          headers={"Authorization": "Bearer clé"}, cap_bytes=1024, timeout_s=1))
    assert answer.transport == "blocked" and "cannot carry" in answer.detail


def test_each_check_uses_a_fresh_made_up_path():
    seen: list[str] = []

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        seen.append(url)
        return engine_check.Answer(method=method, url=url, transport="refused", detail="refused")

    catalog = load_engine_catalog()
    for _ in range(2):
        asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://192.0.2.9:8080/v1"},
                              key=KeyInput(), fetch=fetch))
    tokens = {url.rsplit("zz-not-a-route-", 1)[1] for url in seen if "zz-not-a-route-" in url}
    assert len(tokens) == 2


def test_the_check_sends_only_gets_and_empty_posts_with_short_caps():
    calls: list[tuple[str, str, int, bytes | None]] = []

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        calls.append((method, url, cap_bytes, content))
        status = 200 if url.endswith("/models") else 404
        body = b'{"data":[{"id":"m"}]}' if status == 200 else b"{}"
        return engine_check.Answer(method=method, url=url, status=status, body=body)

    catalog = load_engine_catalog()
    asyncio.run(run_check(catalog, catalog.definition("openai-compatible"), {"address": "http://192.0.2.9:8080/v1"},
                          key=KeyInput(), fetch=fetch, baseline_token=TOKEN))
    assert calls
    for method, url, cap, content in calls:
        assert method in {"GET", "POST"}
        assert content in (None, b"{}"), f"{method} {url} sent a body"
        if url.endswith(("/openapi.json", "/v1/models")):
            assert cap == 1024 * 1024, "the API description and a long model list get a larger cap"
        else:
            assert cap == 64 * 1024


def test_a_resolver_or_connection_that_raises_gives_a_finding_not_a_crash():
    async def resolve(host, port=None):
        raise RuntimeError("name resolution is not allowed here")
    result = _check("http://name.invalid:8080/v1", fetch=make_http_fetch(resolve=resolve))
    assert result["outcome"] == "error"
    assert result["findings"][0]["field"] == "address"


def test_the_resolver_is_asked_for_the_port_it_will_connect_to():
    asked: list = []

    async def resolve(host, port=None):
        asked.append((host, port))
        return ["192.0.2.30"]
    asyncio.run(blocked_reason("http://lan-box.example:8188", resolve=resolve))
    assert asked == [("lan-box.example", 8188)]
