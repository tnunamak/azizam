from __future__ import annotations

import asyncio
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

import proxy


def _migrated_model_policy_config() -> dict:
    config = copy.deepcopy(proxy.config)
    for service in (config.get("backends") or {}).values():
        if isinstance(service, dict) and isinstance(service.get("models"), list):
            service["model_policy_schema_version"] = proxy.service_registry.MODEL_POLICY_SCHEMA_VERSION
    return config


class _FakeExecutionClient:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


class _TrackedCloseClient:
    def __init__(self) -> None:
        self.close_attempted = False

    async def aclose(self) -> None:
        self.close_attempted = True


class _RaisingCloseExecutionResponse:
    """A successful nonstream response whose own aclose() fails.

    Proves the internal-litellm-local route routes cleanup through the
    shared close_llm_response helper: client teardown must still be
    attempted even though the response's own close raised.
    """

    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self) -> bytes:
        return json.dumps(
            {
                "id": "chatcmpl_internal_close_probe",
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
        ).encode("utf-8")

    async def aclose(self) -> None:
        raise RuntimeError("response close failed")


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


class _BodyThenDisconnect:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self._body_sent = False
        self.abort = asyncio.Event()
        self.receive_calls = 0
        self.body_deliveries = 0

    async def receive(self) -> dict:
        self.receive_calls += 1
        if not self._body_sent:
            self._body_sent = True
            self.body_deliveries += 1
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self.abort.wait()
        return {"type": "http.disconnect"}


class _NonBlockingDisconnectRequest(Request):
    async def is_disconnected(self) -> bool:
        return False


class _FakeInferenceTask:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.released = asyncio.Event()


class _BlockingInferenceResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, task: _FakeInferenceTask) -> None:
        self._task = task
        self._never = asyncio.Event()
        self.closed = False

    async def aread(self) -> bytes:
        self._task.started.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self._task.read_cancelled.set()
            raise
        return b"unreachable"

    async def aclose(self) -> None:
        self.closed = True
        self._task.released.set()


def _internal_current_request(receiver: _BodyThenDisconnect) -> Request:
    return _NonBlockingDisconnectRequest(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/internal/litellm/current/v1/chat/completions",
            "raw_path": b"/internal/litellm/current/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive=receiver.receive,
    )


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

    async def fake_send(execution_request, *, cancellation_token=None):
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
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
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
    ensure_target.assert_awaited_once_with("kobold", "Qwen.gguf", None, profile=None)
    execution_request = captured["request"]
    assert execution_request.url == "http://127.0.0.1:5051/v1/chat/completions"
    assert execution_request.headers == {"content-type": "application/json"}
    forwarded_body = json.loads(execution_request.body.decode("utf-8"))
    assert forwarded_body["model"] == "Qwen.gguf"
    assert response.headers["x-upstream"] == "local"
    assert response.json()["choices"][0]["message"]["content"] == "ok"


def test_internal_litellm_local_route_still_closes_client_when_response_close_fails() -> None:
    client = TestClient(proxy.app, raise_server_exceptions=False)
    tracked_client = _TrackedCloseClient()

    async def fake_send(_execution_request, *, cancellation_token=None):
        return tracked_client, _RaisingCloseExecutionResponse()

    with patch.dict(
        os.environ,
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
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
    ), patch.object(
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

    assert response.status_code == 500, response.text
    assert tracked_client.close_attempted is True


def test_internal_litellm_local_route_reports_activation_failure() -> None:
    client = TestClient(proxy.app)

    with patch.dict(
        os.environ,
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
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

    async def fake_send(execution_request, *, cancellation_token=None):
        captured["request"] = execution_request
        return _FakeExecutionClient(), _FakeExecutionResponse({"ok": True})

    with patch.dict(
        os.environ,
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
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
    ensure_target.assert_awaited_once_with(
        "kobold",
        activation_path,
        activation_mmproj,
        profile=None,
    )
    forwarded_body = json.loads(captured["request"].body.decode("utf-8"))
    assert forwarded_body["model"] == "Qwen.gguf"


def test_internal_litellm_route_preserves_q8_profile_alias_and_forwards_backend_model() -> None:
    client = TestClient(proxy.app)
    captured = {}
    activation_path = "/models/Qwen.gguf"

    async def fake_send(execution_request, *, cancellation_token=None):
        captured["request"] = execution_request
        return _FakeExecutionClient(), _FakeExecutionResponse({"ok": True})

    with patch.dict(
        os.environ,
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
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
                            "id": "coder-q8",
                            "model_group": "coder-q8",
                            "upstream_model": "coder-q8",
                            "backend_model": "Qwen.gguf",
                            "activation_model": activation_path,
                            "activation_profile": "kv-q8",
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
            json={"model": "openai/coder-q8", "messages": []},
        )

    assert response.status_code == 200, response.text
    ensure_target.assert_awaited_once_with(
        "kobold",
        activation_path,
        None,
        profile="kv-q8",
    )
    forwarded_body = json.loads(captured["request"].body.decode("utf-8"))
    assert forwarded_body["model"] == "Qwen.gguf"


def test_internal_litellm_route_preserves_rp_context_profile_and_huihui_model() -> None:
    client = TestClient(proxy.app)
    captured = {}
    activation_path = "/models/Huihui-Qwen.gguf"

    async def fake_send(execution_request, *, cancellation_token=None):
        captured["request"] = execution_request
        return _FakeExecutionClient(), _FakeExecutionResponse({"ok": True})

    with patch.dict(
        os.environ,
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
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
                            "id": "rp",
                            "model_group": "rp",
                            "upstream_model": "rp",
                            "backend_model": "Huihui-Qwen.gguf",
                            "activation_model": activation_path,
                            "activation_profile": "kv-q5-q4-rp",
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
            json={"model": "openai/rp", "messages": []},
        )

    assert response.status_code == 200, response.text
    ensure_target.assert_awaited_once_with(
        "kobold",
        activation_path,
        None,
        profile="kv-q5-q4-rp",
    )
    forwarded_body = json.loads(captured["request"].body.decode("utf-8"))
    assert forwarded_body["model"] == "Huihui-Qwen.gguf"


def test_internal_litellm_current_route_does_not_enforce_gateway_profile_policy() -> None:
    captured = {}

    async def fake_forward(
        request, *, request_path, auth_context, body_json, enforce_gateway_policy=True, request_id=""
    ):
        captured.update(
            {
                "request_path": request_path,
                "auth_context": auth_context,
                "body_json": body_json,
                "enforce_gateway_policy": enforce_gateway_policy,
                "request_id": request_id,
            }
        )
        return Response(
            content='{"ok": true}',
            status_code=200,
            media_type="application/json",
        )

    client = TestClient(proxy.app)
    with patch.dict(
        os.environ,
        {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"},
        clear=False,
    ), patch.object(
        proxy,
        "_forward_current_loaded_openai_request",
        fake_forward,
    ):
        response = client.post(
            "/internal/litellm/current/v1/chat/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={"model": "openai/current-fast", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200, response.text
    assert captured["request_path"] == "/v1/chat/completions"
    assert captured["auth_context"] == proxy.NO_AUTH_CONTEXT
    assert captured["body_json"]["model"] == "current-fast"
    assert captured["enforce_gateway_policy"] is False


class InternalCurrentLoadedCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._model_policy_config = patch.object(proxy, "config", _migrated_model_policy_config())
        self._model_policy_config.start()
        self.addCleanup(self._model_policy_config.stop)

    def _patches(self, send):
        return (
            patch.object(proxy, "_is_internal_litellm_lifecycle_request", return_value=True),
            patch.object(
                proxy,
                "_current_loaded_llm_target",
                AsyncMock(
                    return_value=(
                        proxy.CurrentLoadedLlmTarget(
                            backend="local",
                            host="http://upstream.test",
                            model="loaded.gguf",
                        ),
                        None,
                    )
                ),
            ),
            patch.object(proxy, "prepare_downstream_headers", return_value={}),
            patch.object(proxy, "send_llm_execution_request", send),
        )

    async def test_abort_releases_current_loaded_upstream_task_after_body_read(self) -> None:
        receiver = _BodyThenDisconnect(
            {"model": "current", "messages": [{"role": "user", "content": "hi"}]}
        )
        task = _FakeInferenceTask()
        upstream = _BlockingInferenceResponse(task)
        client = _FakeExecutionClient()

        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return client, upstream

        patches = self._patches(fake_send)
        with patches[0], patches[1], patches[2], patches[3]:
            route_task = asyncio.create_task(
                proxy.internal_litellm_current_chat_completions(
                    _internal_current_request(receiver)
                )
            )
            await asyncio.wait_for(task.started.wait(), timeout=0.25)
            receiver.abort.set()
            response = await asyncio.wait_for(route_task, timeout=0.25)

        self.assertEqual(response.status_code, 499)
        self.assertEqual(receiver.body_deliveries, 1)
        self.assertEqual(receiver.receive_calls, 2)
        self.assertTrue(task.read_cancelled.is_set())
        self.assertTrue(task.released.is_set())
        self.assertTrue(upstream.closed)
        self.assertTrue(client.closed)

    async def test_current_loaded_non_stream_response_completes_without_a_second_body_read(self) -> None:
        receiver = _BodyThenDisconnect(
            {"model": "current", "messages": [{"role": "user", "content": "hi"}]}
        )
        upstream = _FakeExecutionResponse({"id": "chatcmpl_123", "choices": []})
        client = _FakeExecutionClient()

        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return client, upstream

        patches = self._patches(fake_send)
        with patches[0], patches[1], patches[2], patches[3]:
            response = await asyncio.wait_for(
                proxy.internal_litellm_current_chat_completions(
                    _internal_current_request(receiver)
                ),
                timeout=0.25,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"id": "chatcmpl_123", "choices": []})
        self.assertEqual(receiver.body_deliveries, 1)
        self.assertLessEqual(receiver.receive_calls, 2)
        self.assertTrue(upstream.closed)
        self.assertTrue(client.closed)
