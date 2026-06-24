from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import proxy


class _FakeExecutionClient:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeExecutionResponse:
    status_code = 200
    headers = {
        "content-type": "application/json",
        "x-upstream": "local",
    }

    def __init__(self, payload: dict):
        self._payload = payload
        self.closed = False

    async def aread(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    async def aclose(self) -> None:
        self.closed = True


def test_internal_litellm_local_route_requires_internal_key() -> None:
    client = TestClient(proxy.app)

    with patch.dict(os.environ, {}, clear=True):
        response = client.post(
            "/internal/litellm/local/kobold/v1/chat/completions",
            json={"model": "Qwen.gguf", "messages": []},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_internal_litellm_local_route_activates_and_forwards_directly() -> None:
    client = TestClient(proxy.app)
    captured = {}

    async def fake_send(execution_request):
        captured["request"] = execution_request
        return _FakeExecutionClient(), _FakeExecutionResponse(
            {
                "id": "chatcmpl_internal_lifecycle",
                "object": "chat.completion",
                "model": "Qwen.gguf",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    with patch.dict(
        os.environ,
        {"AI_GATEWAY_INTERNAL_LITELLM_KEY": "internal-secret"},
        clear=False,
    ), patch.dict(
        proxy.LLM_BACKEND_HOSTS,
        {"kobold": "http://127.0.0.1:5051"},
        clear=True,
    ), patch.object(
        proxy,
        "_service_availability_response",
        return_value=None,
    ), patch.object(
        proxy,
        "ensure_target",
        AsyncMock(return_value=True),
    ) as ensure_target, patch.object(
        proxy,
        "send_llm_execution_request",
        fake_send,
    ):
        response = client.post(
            "/internal/litellm/local/kobold/v1/chat/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={
                "model": "openai/Qwen.gguf",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 200, response.text
    ensure_target.assert_awaited_once_with("kobold", "Qwen.gguf", None)
    execution_request = captured["request"]
    assert execution_request.url == "http://127.0.0.1:5051/v1/chat/completions"
    assert execution_request.headers == {"content-type": "application/json"}
    forwarded_body = json.loads(execution_request.body.decode("utf-8"))
    assert forwarded_body["model"] == "Qwen.gguf"
    assert response.headers["x-upstream"] == "local"
    assert response.json()["choices"][0]["message"]["content"] == "ok"


def test_internal_litellm_local_route_reports_activation_failure() -> None:
    client = TestClient(proxy.app)

    with patch.dict(
        os.environ,
        {"AI_GATEWAY_INTERNAL_LITELLM_KEY": "internal-secret"},
        clear=False,
    ), patch.dict(
        proxy.LLM_BACKEND_HOSTS,
        {"kobold": "http://127.0.0.1:5051"},
        clear=True,
    ), patch.object(
        proxy,
        "_service_availability_response",
        return_value=None,
    ), patch.object(
        proxy,
        "ensure_target",
        AsyncMock(return_value=False),
    ):
        response = client.post(
            "/internal/litellm/local/kobold/v1/chat/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={"model": "Qwen.gguf", "messages": []},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"


def test_internal_litellm_local_route_resolves_activation_path_from_model_data() -> None:
    client = TestClient(proxy.app)
    captured = {}
    activation_path = "/models/Qwen.gguf"
    activation_mmproj = "/models/Qwen-mmproj.gguf"

    async def fake_send(execution_request):
        captured["request"] = execution_request
        return _FakeExecutionClient(), _FakeExecutionResponse({"ok": True})

    with patch.dict(
        os.environ,
        {"AI_GATEWAY_INTERNAL_LITELLM_KEY": "internal-secret"},
        clear=False,
    ), patch.dict(
        proxy.LLM_BACKEND_HOSTS,
        {"kobold": "http://127.0.0.1:5051"},
        clear=True,
    ), patch.object(
        proxy,
        "config",
        {
            "backends": {
                "kobold": {
                    "models": [
                        {
                            "model_group": "default",
                            "upstream_model": "Qwen.gguf",
                            "activation_model": activation_path,
                            "activation_mmproj": activation_mmproj,
                        }
                    ]
                }
            }
        },
    ), patch.object(
        proxy,
        "_service_availability_response",
        return_value=None,
    ), patch.object(
        proxy,
        "ensure_target",
        AsyncMock(return_value=True),
    ) as ensure_target, patch.object(
        proxy,
        "send_llm_execution_request",
        fake_send,
    ):
        response = client.post(
            "/internal/litellm/local/kobold/v1/chat/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={"model": "openai/Qwen.gguf", "messages": []},
        )

    assert response.status_code == 200, response.text
    ensure_target.assert_awaited_once_with("kobold", activation_path, activation_mmproj)
    forwarded_body = json.loads(captured["request"].body.decode("utf-8"))
    assert forwarded_body["model"] == "Qwen.gguf"
