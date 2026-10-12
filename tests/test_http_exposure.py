"""Exercise the public HTTP boundary without contacting inference engines."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

import proxy


@pytest.fixture
def public_client(monkeypatch):
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "OLLAMA_COMPAT_DISCOVERY_ENABLED", False)
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
    client = TestClient(proxy.app, base_url="https://gateway.example", client=("203.0.113.25", 41000))
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/models"), ("GET", "/openai/v1/models"),
    ("POST", "/v1/chat/completions"), ("POST", "/v1/responses"),
    ("POST", "/v1/embeddings"), ("POST", "/v1/images/generations"),
    ("POST", "/v1/images/edits"), ("POST", "/v1/videos"),
    ("POST", "/v1/audio/speech"), ("POST", "/v1/audio/transcriptions"),
    ("GET", "/anthropic/v1/models"), ("POST", "/anthropic/v1/messages"),
    ("POST", "/anthropic/v1/messages/count_tokens"),
    ("GET", "/api/tags"), ("POST", "/api/generate"),
    ("GET", "/ollama/api/tags"), ("POST", "/ollama/api/chat"),
    ("GET", "/sdapi/v1/sd-models"), ("POST", "/sdapi/v1/txt2img"),
    ("GET", "/gateway/v1/capabilities"), ("GET", "/gateway/v1/receipts"),
    ("POST", "/gateway/v1/media/jobs"),
    ("GET", "/gateway/v1/providers/absent/v1/models"),
    ("GET", "/typesafe/v1/models"), ("POST", "/typesafe/v1/systemone"),
    ("POST", "/v1/decisions"), ("POST", "/mcp"),
    ("GET", "/showcase/api/status"),
    ("POST", "/me/api/apps"), ("GET", "/me/api/media"),
    ("GET", "/me/api/usage/summary"), ("GET", "/admin/api/people"),
    ("GET", "/admin/api/operator/service-registry"),
])
def test_public_protocols_require_auth_before_execution(public_client, method, path):
    response = public_client.request(method, path, **({"json": {}} if method == "POST" else {}))
    assert response.status_code == 401, (method, path, response.status_code)


def test_public_realtime_cannot_open_session(public_client):
    with pytest.raises(WebSocketDenialResponse) as error:
        with public_client.websocket_connect("/v1/realtime"):
            pytest.fail("Unauthenticated realtime connection accepted")
    assert error.value.status_code == 401


@pytest.mark.parametrize("path", ["/v1/images", "/v1/assets"])
def test_unsigned_asset_fetch_cannot_deliver_content(public_client, path):
    asset_id = "img_" + "a" * 64
    response = public_client.get(f"{path}/{asset_id}/content", params={"expires": "4102444800", "sig": "invalid"})
    assert response.status_code == 403


def test_public_activity_has_no_member_records(public_client):
    response = public_client.get("/me/api/activity")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert not response.json().get("receipts")


@pytest.mark.parametrize("path,status", [("/status/raw", 401), ("/settings/", 302)])
def test_owner_pages_refuse_anonymous_browsers(public_client, path, status):
    response = public_client.get(path, follow_redirects=False)
    assert response.status_code == status
    if status == 302:
        assert response.headers["location"].startswith("/login?")
