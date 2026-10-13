"""Backend requests must not carry gateway authority or ambiguous scope paths."""

import asyncio
from unittest.mock import patch

import pytest
from starlette.requests import Request

import proxy
from tests.support.access import access, plan_for  # noqa: E402
from gateway.outbound_request import backend_query_string, canonical_native_path
from gateway.backends.llm_execution import litellm_url
from gateway.realtime_proxy import upstream_url
from gateway.policy import PolicyRequest, evaluate


def test_backend_headers_allow_only_representation_and_configured_auth(monkeypatch):
    monkeypatch.setattr(proxy, "service_auth_config", lambda *_: {
        "headers": {"Authorization": "Bearer synthetic-backend-key"},
    })
    headers = proxy.prepare_downstream_headers({
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "test-client",
        "Cookie": "azizam_admin_session=synthetic-session",
        "X-CSRF-Token": "synthetic-csrf",
        proxy._INTERNAL_PROTOCOL_FORWARD_HEADER: "synthetic-internal",
        "X-Azizam-Request-Id": "synthetic-parent",
        "Authorization": "Bearer synthetic-caller",
        "X-Api-Key": "synthetic-caller",
        "X-Admin-Key": "synthetic-caller",
        "Api-Key": "synthetic-caller",
        "Host": "gateway.example",
        "Content-Length": "1",
        "Connection": "keep-alive",
        "X-Arbitrary": "untrusted",
    }, proxy.NO_AUTH_CONTEXT, "test-backend", backend_config={})
    assert headers == {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "accept-encoding": "identity",
        "user-agent": "test-client",
        "Authorization": "Bearer synthetic-backend-key",
    }


def test_backend_query_strips_all_gateway_credential_spellings():
    assert backend_query_string(
        "limit=1&api_key=a&api%5fkey=b&API_KEY=c&admin_key=d&access_token=e&api-key=f&empty="
    ) == "limit=1&empty="


def test_substrate_and_realtime_urls_strip_gateway_credentials():
    assert litellm_url(base_url="http://substrate.example", path="/v1/chat/completions", query="api_key=synthetic&limit=1") == "http://substrate.example/v1/chat/completions?limit=1"
    assert upstream_url("realtime.example", 80, {"api_key": "synthetic", "API_KEY": "synthetic", "model": "speech"}) == "ws://realtime.example:80/v1/realtime?model=speech"


@pytest.mark.parametrize("path", [
    "v1/../admin/keys", "v1/./models", "v1/%2e%2e/admin/keys",
    "v1/%252e%252e/admin/keys", "v1%2f..%2fadmin/keys",
    "v1\\..\\admin\\keys", "v1//models", "/v1//models",
])
@pytest.mark.parametrize("scope", ["GET /v1/*", "*", "GET *"])
def test_native_scope_rejects_ambiguous_paths(path, scope):
    plan = plan_for("native-security", caps=(), engines=["test-backend"], operations=[scope])
    decision = evaluate(access(plan), PolicyRequest(
        protocol="provider-native", intent="provider.native", backend="test-backend",
        native_passthrough=True, native_backend="test-backend", native_operation=f"GET /{path.lstrip('/')}",
    ))
    assert decision.denied


@pytest.mark.parametrize("native_path,raw_path", [
    ("v1/../admin/keys", b"/gateway/v1/providers/test/v1/../admin/keys"),
    ("v1/../admin/keys", b"/gateway/v1/providers/test/v1/%2e%2e/admin/keys"),
    ("v1/models", b"/gateway/v1/providers/test/v1%2fmodels"),
    ("v1/models", b"/gateway/v1/providers/test/v1/%2emodels"),
    ("v1//models", b"/gateway/v1/providers/test/v1//models"),
    ("v1\\models", b"/gateway/v1/providers/test/v1%5cmodels"),
])
def test_native_route_rejects_ambiguity_before_policy_body_or_backend(native_path, raw_path):
    async def unread():
        raise AssertionError("invalid native path must not read body")

    request = Request({
        "type": "http", "method": "GET", "scheme": "http",
        "path": raw_path.decode(), "raw_path": raw_path,
        "query_string": b"", "headers": [],
    }, receive=unread)
    with patch.object(proxy, "_PROVIDER_NATIVE_TARGETS", {
        "test": {"backend": "test-backend", "service": "test-backend", "host": "http://backend.example"},
    }), patch.object(proxy, "_provider_native_invalid_paid_remote_response", return_value=None), patch.object(
        proxy, "evaluate_and_apply", side_effect=AssertionError("must reject before authorization"),
    ):
        response = asyncio.run(proxy.provider_native_request(request, "test", native_path))
    assert response.status_code == 400
    assert b"invalid_native_path" in response.body


def test_native_valid_path_is_authorized_and_transmitted_identically():
    assert canonical_native_path("v1/models") == "/v1/models"
    assert canonical_native_path("/v1/models") == "/v1/models"
