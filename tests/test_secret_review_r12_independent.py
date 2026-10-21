"""Round 12 of the secret handling: what the independent review of phase1-r11 (cfa7a8d9) found (D45, D45b).

Two error paths still handed an engine's own value to a caller (synthetic credentials, mounted routes):

* the ``Retry-After`` of a provider's 429, stored in ``ProviderRateLimited`` and copied into the gateway's error response;
* the status, body and ``Content-Type`` of a provider's reply to ``/v1/decisions``, relayed even when it was an error.

``ENCODED`` stands for a credential the provider echoes in a shape no scrubber of known spellings recognizes.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import client_errors, providers
from tests.test_decisions_provider_integration import (
    AUTH_HEADERS, BODY, _authenticated, _install_registry, _mock_openai_http, _mock_typesafe_http, _profile,
    _provider_registry,
)

MARKER = "r12-review-synthetic-credential-392701"
ENCODED = base64.b64encode(MARKER.encode()).decode()


@pytest.mark.parametrize("path", ["/v1/systemone", "/typesafe/v1/systemone"])
def test_a_provider_retry_after_is_not_copied_into_the_error(tmp_path, path):
    calls = []

    async def engine(request):
        assert request.headers["authorization"] == "Bearer " + MARKER
        calls.append(str(request.url))
        return httpx.Response(429, json={"error": "refused"}, headers={"Retry-After": ENCODED})

    with _authenticated(_profile()) as (_, mp):
        _install_registry(mp, tmp_path)
        mp.setenv("TYPESAFE_TEST_KEY", MARKER)
        with _mock_typesafe_http(engine):
            response = TestClient(proxy.app).post(path, json=BODY, headers=AUTH_HEADERS)
    assert calls
    assert response.status_code == 429
    assert "retry-after" not in response.headers
    assert ENCODED not in str(dict(response.headers)) + response.text


@pytest.mark.parametrize("value,expected", [("30", "30"), (ENCODED, None), ("Wed, 21 Oct 2015 07:28:00 GMT", "0"), (None, None)])
def test_a_provider_error_keeps_retry_after_only_as_a_number(value, expected):
    error = providers.ProviderRateLimited("remote provider rate limited the request", retry_after=value)
    assert error.retry_after == expected


@pytest.mark.parametrize("sink", ["content-type", "error-body"])
def test_a_provider_error_reply_to_decisions_is_the_gateways_words(tmp_path, sink):
    calls = []

    async def engine(request):
        assert request.headers["authorization"] == "Bearer " + MARKER
        calls.append(str(request.url))
        headers = {"content-type": "application/json; echo=" + ENCODED} if sink == "content-type" else {}
        body = {"error": {"message": ENCODED if sink == "error-body" else "refused", "code": "not_enabled"}}
        return httpx.Response(403, json=body, headers=headers)

    registry = _provider_registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}])
    profile = _profile(max_usd_per_request=None, budget_usd_per_day=None, budget_usd_per_month=None)
    with _authenticated(profile) as (_, mp):
        _install_registry(mp, tmp_path, registry)
        mp.setenv("OPENAI_TEST_KEY", MARKER)
        with _mock_openai_http(engine):
            response = TestClient(proxy.app).post("/v1/decisions", json={"model": "gpt-5", "input": "test"}, headers=AUTH_HEADERS)
    assert calls
    assert response.status_code == 403
    assert ENCODED not in str(dict(response.headers)) + response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["message"] == client_errors.status_reason(403)
    assert response.headers["x-azizam-provider"] == "openai"


def test_a_successful_decisions_reply_keeps_only_the_media_type_and_charset(tmp_path):
    async def engine(request):
        return httpx.Response(200, json={"decision": "ok"}, headers={"content-type": "application/json; charset=utf-8; echo=" + ENCODED})

    registry = _provider_registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}])
    profile = _profile(max_usd_per_request=None, budget_usd_per_day=None, budget_usd_per_month=None)
    with _authenticated(profile) as (_, mp):
        _install_registry(mp, tmp_path, registry)
        mp.setenv("OPENAI_TEST_KEY", MARKER)
        with _mock_openai_http(engine):
            response = TestClient(proxy.app).post("/v1/decisions", json={"model": "gpt-5", "input": "test"}, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert ENCODED not in str(dict(response.headers))
    assert response.headers["content-type"] == "application/json; charset=utf-8"


# --- the matrix: every error status and every place a provider can put the credential ----------------------------------------

def _echo(status: int, sink: str) -> httpx.Response:
    headers = {"x-trace": ENCODED, "x-azizam-note": ENCODED, "www-authenticate": 'Bearer realm="' + ENCODED + '"'}
    body: dict = {"error": {"message": ENCODED, "type": ENCODED, "param": ENCODED, "code": ENCODED}, "detail": ENCODED}
    if sink == "retry-after":
        headers["retry-after"] = ENCODED
    if sink == "content-type":
        headers["content-type"] = "application/json; echo=" + ENCODED
    return httpx.Response(status, json=body, headers=headers)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 422, 429, 500, 502, 503])
@pytest.mark.parametrize("sink", ["body", "retry-after", "content-type"])
@pytest.mark.parametrize("path", ["/v1/systemone", "/typesafe/v1/systemone"])
def test_no_systemone_error_carries_a_provider_value(tmp_path, path, sink, status):
    async def engine(request):
        return _echo(status, sink)

    with _authenticated(_profile()) as (_, mp):
        _install_registry(mp, tmp_path)
        mp.setenv("TYPESAFE_TEST_KEY", MARKER)
        with _mock_typesafe_http(engine):
            response = TestClient(proxy.app).post(path, json=BODY, headers=AUTH_HEADERS)
    assert response.status_code >= 400
    assert ENCODED not in str(dict(response.headers)) + response.text


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 422, 429, 500, 502, 503])
@pytest.mark.parametrize("sink", ["body", "retry-after", "content-type"])
def test_no_decisions_error_carries_a_provider_value(tmp_path, sink, status):
    async def engine(request):
        return _echo(status, sink)

    registry = _provider_registry(provider_id="openai", protocols=("openai-compatible", "decisions"), models=[{"id": "gpt-5"}])
    profile = _profile(max_usd_per_request=None, budget_usd_per_day=None, budget_usd_per_month=None)
    with _authenticated(profile) as (_, mp):
        _install_registry(mp, tmp_path, registry)
        mp.setenv("OPENAI_TEST_KEY", MARKER)
        with _mock_openai_http(engine):
            response = TestClient(proxy.app).post("/v1/decisions", json={"model": "gpt-5", "input": "test"}, headers=AUTH_HEADERS)
    assert response.status_code >= 400
    assert ENCODED not in str(dict(response.headers)) + response.text
