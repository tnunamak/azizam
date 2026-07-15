from __future__ import annotations

import os
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import proxy


class _FakeClient:
    async def aclose(self) -> None:
        return None


class _FakeResponse:
    def __init__(self, payload: bytes, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload

    async def aread(self) -> bytes:
        return self._payload

    async def aclose(self) -> None:
        return None


def _litellm_registry() -> dict:
    return {
        "services": {
            "litellm": {
                "id": "litellm",
                "kind": "llm-substrate",
                "enabled": True,
                "capabilities": ["llm.execution"],
                "config": {
                    "base_url": "http://127.0.0.1:4011",
                    "api_key_env": "LITELLM_MASTER_TEST",
                },
            }
        }
    }


def test_litellm_virtual_key_reaches_litellm_with_delegated_header() -> None:
    captured = []

    async def fake_send(request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
        captured.append(request)
        return _FakeClient(), _FakeResponse(
            b'{"id":"chatcmpl_test","object":"chat.completion",'
            b'"model":"delegated-model","choices":[{"index":0,'
            b'"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'
        )

    client = TestClient(proxy.app)
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_TEST": "sk-master"}, clear=False),
        patch.object(proxy, "AUTH_ENABLED", True),
        patch.object(proxy, "runtime_identity_for_app_credential_hash", lambda *a, **k: None),
        patch.object(proxy.ADMIN_STORE, "get_service_registry", _litellm_registry),
        patch.object(proxy, "send_llm_execution_request", fake_send),
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-virtual-test"},
            json={
                "model": "delegated-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200, response.text
    assert captured
    headers = captured[0].headers
    assert headers["X-Litellm-Key"] == "Bearer sk-virtual-test"
    assert "authorization" not in headers


def test_litellm_virtual_key_does_not_unlock_gateway_media_routes() -> None:
    client = TestClient(proxy.app)
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_TEST": "sk-master"}, clear=False),
        patch.object(proxy, "AUTH_ENABLED", True),
        patch.object(proxy, "runtime_identity_for_app_credential_hash", lambda *a, **k: None),
    ):
        response = client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer sk-virtual-test"},
            json={"model": "image-test", "prompt": "cat"},
        )

    assert response.status_code == 401


def test_litellm_virtual_key_rejected_on_durable_media_job_routes() -> None:
    """A LiteLLM virtual key is for metered LLM execution only — it must never
    authorize the gateway-owned durable media/job surface.

    Delegation (``allow_litellm_delegation=True``) is wired only into the
    LLM-execution routes; the ``/gateway/v1/media/jobs*`` routes authenticate
    through ``_gateway_auth_context`` (``authenticate_request``, no delegation),
    so an ``sk-``-prefixed virtual key that is not a real gateway credential
    must be rejected with 401. LAN bypass is forced off so the boundary is
    asserted purely on the credential, independent of local config.
    """
    media_job_routes = [
        ("POST", "/gateway/v1/media/jobs"),
        ("GET", "/gateway/v1/media/jobs"),
        ("POST", "/gateway/v1/media/jobs/image-generations"),
        ("GET", "/gateway/v1/media/jobs/mj_probe"),
        ("GET", "/gateway/v1/media/jobs/mj_probe/events"),
        ("POST", "/gateway/v1/media/jobs/mj_probe/events"),
        ("POST", "/gateway/v1/media/jobs/mj_probe/cancel"),
        ("GET", "/gateway/v1/media/jobs/mj_probe/content"),
    ]

    client = TestClient(proxy.app)
    with (
        patch.object(proxy, "AUTH_ENABLED", True),
        patch.object(proxy, "_client_is_lan", lambda _request: False),
        patch.object(proxy, "runtime_identity_for_app_credential_hash", lambda *a, **k: None),
    ):
        for method, path in media_job_routes:
            response = client.request(
                method,
                path,
                headers={"Authorization": "Bearer sk-virtual-test"},
                json={} if method == "POST" else None,
            )
            assert response.status_code == 401, (
                f"{method} {path} accepted a LiteLLM virtual key "
                f"(status {response.status_code}); delegated keys must not "
                "unlock gateway-owned media/job routes"
            )


def test_litellm_virtual_key_model_list_is_litellm_catalog_only() -> None:
    captured = []

    async def fake_send(request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
        captured.append(request)
        return _FakeClient(), _FakeResponse(
            b'{"object":"list","data":[{"id":"allowed-model","object":"model"}]}'
        )

    client = TestClient(proxy.app)
    with (
        patch.object(proxy, "AUTH_ENABLED", True),
        patch.object(proxy, "runtime_identity_for_app_credential_hash", lambda *a, **k: None),
        patch.object(proxy.ADMIN_STORE, "get_service_registry", _litellm_registry),
        patch.object(proxy, "send_llm_execution_request", fake_send),
        patch.object(proxy, "_build_workflow_model_entries", lambda: [{"id": "media-workflow"}]),
    ):
        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-virtual-test"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [entry["id"] for entry in payload["data"]] == ["allowed-model"]
    assert captured[0].headers["X-Litellm-Key"] == "Bearer sk-virtual-test"


def test_anthropic_messages_accepts_litellm_virtual_key_without_gateway_profile_policy() -> None:
    captured = {}

    async def fake_forward(request, auth_context, cc_body, *, stream):
        captured.update({"auth_context": auth_context, "body": cc_body, "stream": stream})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_anthropic",
                "object": "chat.completion",
                "model": "delegated-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = TestClient(proxy.app)
    with (
        patch.object(proxy, "AUTH_ENABLED", True),
        patch.object(proxy, "runtime_identity_for_app_credential_hash", lambda *a, **k: None),
        patch.object(proxy, "_protocol_policy_decision", side_effect=AssertionError("gateway policy should be skipped")),
        patch.object(proxy, "_forward_chat_completions", fake_forward),
    ):
        response = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer sk-virtual-test"},
            json={
                "model": "delegated-model",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200, response.text
    assert captured["auth_context"] == proxy.NO_AUTH_CONTEXT
    assert captured["body"]["model"] == "delegated-model"


def test_ollama_tags_uses_litellm_filtered_catalog_for_virtual_key() -> None:
    captured = {}

    async def fake_models_payload(request, auth_context, *, runtime_config, delegated_litellm_api_key=""):
        captured.update(
            {
                "auth_context": auth_context,
                "delegated_litellm_api_key": delegated_litellm_api_key,
            }
        )
        return {
            "object": "list",
            "data": [{"id": "allowed-model", "object": "model"}],
        }

    client = TestClient(proxy.app)
    with (
        patch.dict(os.environ, {"LITELLM_MASTER_TEST": "sk-master"}, clear=False),
        patch.object(proxy, "AUTH_ENABLED", True),
        patch.object(proxy, "runtime_identity_for_app_credential_hash", lambda *a, **k: None),
        patch.object(proxy.ADMIN_STORE, "get_service_registry", _litellm_registry),
        patch.object(proxy, "_protocol_policy_decision", side_effect=AssertionError("gateway policy should be skipped")),
        patch.object(proxy, "_litellm_models_payload", fake_models_payload),
    ):
        response = client.get(
            "/api/tags",
            headers={"Authorization": "Bearer sk-virtual-test"},
        )

    assert response.status_code == 200, response.text
    assert captured["auth_context"] == proxy.NO_AUTH_CONTEXT
    assert captured["delegated_litellm_api_key"] == "sk-virtual-test"
    assert [model["name"] for model in response.json()["models"]] == ["allowed-model"]
