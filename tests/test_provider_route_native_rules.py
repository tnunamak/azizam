"""/gateway/v1/providers/{provider}/{path} keeps the native lane's rules for the same engine record.

The older provider route and /native/{engine}/ share the engine record, so the route must not be a way
around the lane: no engine cookies or origin headers to the caller, no undeclared query, a destination
check on every call, and nothing but a read for a caller who is not the owner unless the record's
``native.allow`` list declares the call ``generate`` (contract section 6).
"""
import gzip
import json

import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

import proxy
from tests.support.access import plan_for
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.test_native_lane_hardening import WIDE, _denied, _models_scenario, _unload_scenario
from tests.test_native_passthrough import _engine, _scenario, native_gateway  # noqa: F401 - fixture
from tests.test_runtime_route_policy import AUTH_HEADERS, authenticated_profile

pytestmark = pytest.mark.usefixtures("native_gateway")

PROVIDER = "/gateway/v1/providers/garage"


def _serve(engine_url: str, **record):
    """Patch the engine record in and rebuild the provider route's targets from it."""
    return (
        patch.dict(proxy.config["backends"], {"garage": _engine(engine_url, native_passthrough=True, **record)}),
        patch.dict(proxy._PROVIDER_NATIVE_TARGETS, proxy._build_provider_native_targets()),
    )


def _call(method: str, path: str, plan_name: str, engine_url: str, *, owner: bool = False, **record):
    backends, targets = _serve(engine_url, **record)
    with authenticated_profile(plan_for(plan_name, caps=(), **WIDE)), backends:
        with patch.object(proxy, "_native_caller_is_owner", return_value=owner), patch.dict(
            proxy._PROVIDER_NATIVE_TARGETS, proxy._build_provider_native_targets(),
        ):
            return TestClient(proxy.app).request(method, path, headers=AUTH_HEADERS)


def test_the_provider_route_does_not_relay_an_engine_cookie_or_origin_header(fake_upstreams):
    engine = fake_upstreams.loopback(_models_scenario(headers={
        "set-cookie": "sid=engine; Path=/", "access-control-allow-origin": "*", "strict-transport-security": "max-age=1",
        "x-engine-note": "kept",
    }))
    response = _call("GET", f"{PROVIDER}/v1/models", "prov-cookie", engine.base_url)
    assert response.status_code == 200, response.text
    names = {name.lower() for name in response.headers}
    assert not names & {"set-cookie", "access-control-allow-origin", "strict-transport-security"}
    assert response.headers["x-engine-note"] == "kept"


def test_a_read_through_the_provider_route_passes_an_ordinary_query(fake_upstreams):
    engine = fake_upstreams.loopback(_models_scenario())
    response = _call("GET", f"{PROVIDER}/v1/models?limit=5", "prov-query-ok", engine.base_url)
    assert response.status_code == 200, response.text
    [sent] = engine.calls("models")
    assert dict(sent.query) == {"limit": "5"}


@pytest.mark.parametrize("query", ["filename=/etc/passwd", "file=%2Fetc%2Fpasswd", "path=../../keys", "f=file:///etc/passwd"])
def test_a_read_cannot_carry_a_file_path_in_a_query_value(fake_upstreams, query):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    response = _call("GET", f"{PROVIDER}/v1/models?{query}", "prov-query-path", engine.base_url)
    _denied(response, "native_query_value_refused")
    assert engine.requests == []


def test_a_non_owner_cannot_make_a_control_call_through_the_provider_route(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    response = _call("DELETE", f"{PROVIDER}/v1/models/demo-chat", "prov-control", engine.base_url)
    _denied(response, "native_owner_only")
    assert engine.requests == []


def test_the_owner_can_make_the_same_call(fake_upstreams):
    engine = fake_upstreams.loopback(_unload_scenario())
    response = _call("DELETE", f"{PROVIDER}/v1/models/demo-chat", "prov-owner", engine.base_url, owner=True)
    assert response.status_code == 200, response.text
    assert len(engine.calls("unload")) == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_a_write_the_record_does_not_declare_is_owner_only(fake_upstreams, method):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    response = _call(method, f"{PROVIDER}/v1/embeddings", "prov-write", engine.base_url)
    _denied(response, "native_owner_only")
    assert engine.requests == []


def test_a_write_the_record_declares_generate_is_open_to_a_non_owner(fake_upstreams):
    engine = fake_upstreams.loopback(_scenario({
        "id": "chat", "match": {"method": "POST", "path": "/v1/chat/completions"},
        "response": {"status": 200, "json": {"ok": True}},
    }))
    allow = {"allow": [{"method": "POST", "path": "/v1/chat/completions", "class": "generate"}]}
    response = _call("POST", f"{PROVIDER}/v1/chat/completions", "prov-generate", engine.base_url, native=allow)
    assert response.status_code == 200, response.text
    assert len(engine.calls("chat")) == 1


def test_a_read_the_record_declares_control_is_owner_only(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "control"}]}
    response = _call("GET", f"{PROVIDER}/v1/models", "prov-read-control", engine.base_url, native=allow)
    _denied(response, "native_owner_only")
    assert engine.requests == []


def test_a_declared_generate_call_keeps_its_query_allow_list(fake_upstreams):
    engine = fake_upstreams.loopback(load_scenario("openai-compatible/no-calls"))
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate", "query": ["limit"]}]}
    response = _call("GET", f"{PROVIDER}/v1/models?debug=1", "prov-allow-query", engine.base_url, native=allow)
    _denied(response, "native_query_not_allowed")
    assert engine.requests == []


@pytest.mark.parametrize("url", ["http://169.254.169.254", "http://[fd00:ec2::254]:80"])
def test_the_provider_route_checks_the_destination_when_the_call_is_made(url):
    response = _call("GET", f"{PROVIDER}/v1/models", "prov-ssrf", url)
    _denied(response, "native_destination_blocked", status=502)


def _gzip_models_scenario():
    payload = gzip.compress(json.dumps({"object": "list", "data": []}).encode())
    return _scenario({
        "id": "models", "match": {"method": "GET", "path": "/v1/models"},
        "response": {"status": 200, "headers": {"content-encoding": "gzip", "content-type": "application/json"},
                     "body": [{"hex": payload.hex()}]},
    })


@pytest.mark.parametrize("path", [f"{PROVIDER}/v1/models", "/native/garage/v1/models"])
def test_decoded_bytes_do_not_go_out_under_the_engines_content_encoding(fake_upstreams, path):
    engine = fake_upstreams.loopback(_gzip_models_scenario())
    allow = {"allow": [{"method": "GET", "path": "/v1/models", "class": "generate"}]}
    response = _call("GET", path, "prov-gzip", engine.base_url, native=allow)
    assert response.status_code == 200, response.text
    assert "content-encoding" not in {name.lower() for name in response.headers}
    assert response.json() == {"object": "list", "data": []}
