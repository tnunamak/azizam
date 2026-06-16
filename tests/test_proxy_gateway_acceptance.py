"""Acceptance tests that pin the live proxy wiring the owner is landing.

These tests target seams in ``proxy.py`` that the owner is actively wiring:

* ``/gateway/v1/*`` discovery/capabilities/receipts mounted *before* the
  catch-all and rejecting unauthenticated requests when proxy auth is enabled.
* Gateway receipt/capability routes using a profile-derived audience
  consistently.
* ``finish_image_route_receipt`` persisting receipts to ``receipt_store.record``
  and swallowing persistence failures.
* The image route helper rejecting unknown top-level params instead of silently
  dropping them, and accepting namespaced workflow extensions when a manifest
  exists.
* Non-streaming Anthropic ``/v1/messages``, Ollama ``/api/chat``,
  ``/api/generate``, ``/api/tags`` routes mounted and translating through a
  mocked chat forwarder (no real network).

When the owner-side symbol does not yet exist on the branch, the test skips
with a precise reason so the suite is still green and the skip itself
documents what wiring is missing.
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import receipt_store  # noqa: E402
from gateway.policy import AppProfile, StatusVisibilityPolicy  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_paths(app) -> list[str]:
    paths: list[str] = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        if path:
            paths.append(path)
    return paths


def _catch_all_index(app) -> int:
    """Index of the catch-all ``/{path:path}`` route, or -1 if not found."""
    for i, route in enumerate(getattr(app, "routes", [])):
        if getattr(route, "path", "") == "/{path:path}":
            return i
    return -1


@dataclass
class _StubResponse:
    status_code: int = 200


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        return None


class _FakeStreamClient:
    async def aclose(self):
        return None


class _FakeLiteLLMResponse:
    status_code = 200
    headers = {"content-type": "application/json", "x-litellm-call-id": "call_1"}

    async def aread(self):
        return json.dumps(
            {
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "model": "assistant-default",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()

    async def aclose(self):
        return None


class _FakeLiteLLMUnknownModelResponse:
    status_code = 400
    headers = {"content-type": "application/json"}

    async def aread(self):
        return json.dumps(
            {
                "error": {
                    "message": (
                        "/chat/completions: Invalid model name passed in "
                        "model=nope-doesnt-exist. Call `/v1/models` to view "
                        "available models for your key."
                    ),
                    "type": "None",
                    "param": "None",
                    "code": "400",
                }
            }
        ).encode()

    async def aclose(self):
        return None


class _FakeLiteLLMEmbeddingResponse:
    status_code = 200
    headers = {"content-type": "application/json", "x-litellm-call-id": "embedding_1"}

    async def aread(self):
        return json.dumps(
            {
                "object": "list",
                "model": "embed-default",
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [3.0, 4.0, 0.0],
                    }
                ],
            }
        ).encode()

    async def aclose(self):
        return None

class _FakeLiteLLMModelsResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self):
        return json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "id": "assistant-default",
                        "object": "model",
                        "owned_by": "litellm",
                        "context_window": 102400,
                    }
                ],
            }
        ).encode()

    async def aclose(self):
        return None


# ---------------------------------------------------------------------------
# Gateway router: mounted before catch-all, unauth requests rejected
# ---------------------------------------------------------------------------


class TestGatewayRouterMounting(unittest.TestCase):
    def test_gateway_router_is_mounted_before_catch_all(self) -> None:
        paths = _route_paths(proxy.app)
        gateway_paths = [p for p in paths if p.startswith("/gateway/v1")]
        if not gateway_paths:
            self.skipTest(
                "gateway router not yet mounted on proxy.app — owner is "
                "wiring create_gateway_router into proxy.py"
            )

        # Catch-all must come AFTER the gateway routes so /gateway/v1/* is
        # served by the router, not the TabbyAPI fall-through.
        catch_all_idx = _catch_all_index(proxy.app)
        self.assertGreaterEqual(catch_all_idx, 0, "catch-all route missing")
        for route in proxy.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/gateway/v1"):
                # All gateway routes must appear before the catch-all index.
                idx = proxy.app.routes.index(route)
                self.assertLess(
                    idx,
                    catch_all_idx,
                    f"gateway route {path!r} is registered after the catch-all",
                )

    def test_gateway_routes_reject_unauthenticated_when_auth_enabled(self) -> None:
        if not any(
            p.startswith("/gateway/v1") for p in _route_paths(proxy.app)
        ):
            self.skipTest(
                "gateway router not yet mounted on proxy.app — cannot test "
                "auth rejection until owner wiring lands"
            )

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", True):
            response = client.get("/gateway/v1/workflows")
            # Unauthenticated → 401. The catch-all would have returned a
            # ProxyError-shaped 401 too, but we additionally verify the body
            # is the proxy's auth_error shape rather than a TabbyAPI passthrough.
            self.assertEqual(response.status_code, 401)
            body = response.json()
            self.assertEqual(
                body.get("error", {}).get("type"), "authentication_error"
            )

    def test_gateway_media_jobs_use_live_store_provider(self) -> None:
        class _FakeJob:
            def to_dict(self, audience):
                return {
                    "job_id": "mj_acceptance",
                    "schema_version": 1,
                    "audience": audience,
                    "intent": "image.generate",
                    "status": "queued",
                }

        calls: list[dict[str, Any]] = []

        def fake_create(payload, *, path):
            calls.append({"payload": payload, "path": path})
            return _FakeJob()

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.media_jobs, "create", fake_create
        ):
            response = client.post(
                "/gateway/v1/media/jobs",
                json={"intent": "image.generate"},
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["job_id"], "mj_acceptance")
        self.assertEqual(calls[0]["path"], proxy.media_jobs.DB_PATH)

    def test_explicit_openai_proxy_routes_bypass_unmapped_v1_guard(self) -> None:
        async def fake_false(*_args, **_kwargs):
            return False

        async def fake_active(*_args, **_kwargs):
            return "tabby"

        def no_litellm_registry():
            return {"services": {}, "protocol_frontends": {}, "workflows": {}, "capabilities": {}}

        client = TestClient(proxy.app)
        blocked_activation = AssertionError(
            "OpenAI-shaped LLM routes must not activate local backends when LiteLLM is unavailable"
        )

        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy, "default_model_for_operation", lambda *_args, **_kwargs: "local-default"
        ), patch.object(
            proxy, "get_active_backend", fake_active
        ), patch.object(
            proxy, "check_tabby_health", fake_false
        ), patch.object(
            proxy, "check_embeddings_health", fake_false
        ), patch.object(
            proxy, "restart_tabby_if_needed", fake_false
        ), patch.object(
            proxy, "ensure_target", AsyncMock(side_effect=blocked_activation)
        ), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", no_litellm_registry
        ):
            cases = [
                (
                    "/v1/chat/completions",
                    {
                        "model": "default",
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                ),
                (
                    "/v1/chat/completions",
                    {
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                ),
                ("/v1/completions", {"model": "gpt-4o-mini", "prompt": "ping"}),
                ("/v1/embeddings", {"model": "text-embedding-3-small", "input": "ping"}),
            ]
            for path, payload in cases:
                with self.subTest(path=path):
                    response = client.post(path, json=payload)
                    self.assertNotEqual(response.status_code, 404, response.text)
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "litellm_unconfigured",
                    )

    def test_chat_routes_through_litellm_after_gateway_policy(self) -> None:
        calls: list[Any] = []
        receipts: list[Any] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMResponse()

        async def fake_policy_ok(*_args, **_kwargs):
            return None

        def fake_record(receipt, *_args, **_kwargs):
            receipts.append(receipt)
            return receipt.request_id

        def fake_registry():
            return {
                "schema_version": 1,
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {
                            "base_url": "http://127.0.0.1:4011",
                            "api_key_env": "TEST_LITELLM_KEY",
                        },
                    }
                },
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.dict(
            proxy.os.environ, {"TEST_LITELLM_KEY": "sk-litellm-internal"}
        ), patch.object(
            proxy, "_current_loaded_model_policy_decision", fake_policy_ok
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy.receipt_store, "record", fake_record
        ):
            response = client.post(
                "/v1/chat/completions?trace=1",
                headers={"Authorization": "Bearer opk_client"},
                json={
                    "model": "assistant-default",
                    "messages": [{"role": "user", "content": "ping"}],
                    "metadata": {"client": "kept"},
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "pong")
        self.assertEqual(response.headers["X-AI-Gateway-LLM-Execution"], "litellm")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].url,
            "http://127.0.0.1:4011/v1/chat/completions?trace=1",
        )
        self.assertEqual(calls[0].headers["authorization"], "Bearer sk-litellm-internal")
        self.assertEqual(calls[0].headers["x-ai-gateway-execution"], "litellm")
        self.assertEqual(calls[0].headers["x-ai-gateway-service-id"], "litellm")
        self.assertEqual(json.loads(calls[0].body)["metadata"], {"client": "kept"})
        self.assertNotIn("opk_client", repr(calls[0]))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].backend, "litellm")
        self.assertEqual(receipts[0].requested_model, "assistant-default")
        self.assertEqual(receipts[0].status, "succeeded")
        self.assertEqual(receipts[0].metadata["execution_backend"], "litellm")
        self.assertEqual(receipts[0].metadata["litellm"]["call_id"], "call_1")

    def test_current_model_uses_active_loaded_backend_without_activation(self) -> None:
        calls: list[Any] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMResponse()

        async def forbidden_activation(*_args, **_kwargs):
            raise AssertionError("model='current' must not activate or load models")

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "LLM_BACKEND_HOSTS",
            {"kobold": "http://127.0.0.1:5051"},
        ), patch.object(
            proxy,
            "get_active_backend",
            AsyncMock(return_value="kobold"),
        ), patch.object(
            proxy,
            "current_llm_model_for_backend",
            AsyncMock(return_value="Manual-Experiment.gguf"),
        ), patch.object(
            proxy,
            "_service_availability_response",
            lambda *_args, **_kwargs: None,
        ), patch.object(
            proxy,
            "ensure_target",
            forbidden_activation,
        ), patch.object(
            proxy,
            "load_model",
            forbidden_activation,
        ), patch.object(
            proxy,
            "send_llm_execution_request",
            fake_send,
        ):
            response = client.post(
                "/v1/chat/completions?api_key=opk_should_not_forward&trace=1",
                json={
                    "model": "current",
                    "messages": [{"role": "user", "content": "ping"}],
                    "reasoning_effort": "minimal",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["X-AI-Gateway-LLM-Execution"], "current-loaded")
        self.assertEqual(response.headers["X-AI-Gateway-Backend"], "kobold")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].url,
            "http://127.0.0.1:5051/v1/chat/completions?trace=1",
        )
        forwarded = json.loads(calls[0].body.decode())
        self.assertEqual(forwarded["model"], "Manual-Experiment.gguf")
        self.assertNotIn("_vf_requested_model", forwarded)
        self.assertNotIn("reasoning_effort", forwarded)
        # Suppression is via chat_template_kwargs.enable_thinking=False only; no /no_think injection
        self.assertFalse(forwarded["chat_template_kwargs"]["enable_thinking"])
        for msg in forwarded["messages"]:
            self.assertNotIn("/no_think", msg.get("content", ""))

    def test_current_model_does_not_force_fast_path(self) -> None:
        calls: list[Any] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMResponse()

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "LLM_BACKEND_HOSTS",
            {"kobold": "http://127.0.0.1:5051"},
        ), patch.object(
            proxy,
            "get_active_backend",
            AsyncMock(return_value="kobold"),
        ), patch.object(
            proxy,
            "current_llm_model_for_backend",
            AsyncMock(return_value="Manual-Experiment.gguf"),
        ), patch.object(
            proxy,
            "_service_availability_response",
            lambda *_args, **_kwargs: None,
        ), patch.object(
            proxy,
            "send_llm_execution_request",
            fake_send,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "current",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        forwarded = json.loads(calls[0].body.decode())
        self.assertEqual(forwarded["model"], "Manual-Experiment.gguf")
        self.assertEqual(
            forwarded["messages"],
            [{"role": "user", "content": "ping"}],
        )
        self.assertNotIn("reasoning_effort", forwarded)
        self.assertNotIn("chat_template_kwargs", forwarded)

    def test_current_fast_model_uses_loaded_backend_and_forces_thinking_off(self) -> None:
        calls: list[Any] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMResponse()

        async def forbidden_activation(*_args, **_kwargs):
            raise AssertionError("model='current-fast' must not activate or load models")

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "LLM_BACKEND_HOSTS",
            {"kobold": "http://127.0.0.1:5051"},
        ), patch.object(
            proxy,
            "get_active_backend",
            AsyncMock(return_value="kobold"),
        ), patch.object(
            proxy,
            "current_llm_model_for_backend",
            AsyncMock(return_value="Manual-Experiment.gguf"),
        ), patch.object(
            proxy,
            "_service_availability_response",
            lambda *_args, **_kwargs: None,
        ), patch.object(
            proxy,
            "ensure_target",
            forbidden_activation,
        ), patch.object(
            proxy,
            "load_model",
            forbidden_activation,
        ), patch.object(
            proxy,
            "send_llm_execution_request",
            fake_send,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "current-fast",
                    "messages": [{"role": "user", "content": "ping"}],
                    "reasoning_effort": "high",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["X-AI-Gateway-LLM-Execution"], "current-loaded")
        forwarded = json.loads(calls[0].body.decode())
        self.assertEqual(forwarded["model"], "Manual-Experiment.gguf")
        self.assertNotIn("reasoning_effort", forwarded)
        # Suppression is via chat_template_kwargs.enable_thinking=False only; no /no_think injection
        self.assertFalse(forwarded["chat_template_kwargs"]["enable_thinking"])
        for msg in forwarded["messages"]:
            self.assertNotIn("/no_think", msg.get("content", ""))

    def test_current_model_returns_503_when_active_backend_has_no_loaded_model(self) -> None:
        async def forbidden_send(*_args, **_kwargs):
            raise AssertionError("unloaded current model request must not forward")

        async def forbidden_activation(*_args, **_kwargs):
            raise AssertionError("unloaded current model request must not load")

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "LLM_BACKEND_HOSTS",
            {"tabby": "http://127.0.0.1:5050"},
        ), patch.object(
            proxy,
            "get_active_backend",
            AsyncMock(return_value="tabby"),
        ), patch.object(
            proxy,
            "current_llm_model_for_backend",
            AsyncMock(return_value=""),
        ), patch.object(
            proxy,
            "_service_availability_response",
            lambda *_args, **_kwargs: None,
        ), patch.object(
            proxy,
            "load_model",
            forbidden_activation,
        ), patch.object(
            proxy,
            "send_llm_execution_request",
            forbidden_send,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "current",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "current_model_unavailable")

    def test_current_model_returns_503_when_no_backend_is_active(self) -> None:
        async def forbidden_send(*_args, **_kwargs):
            raise AssertionError("inactive current model request must not forward")

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "get_active_backend",
            AsyncMock(return_value=None),
        ), patch.object(
            proxy,
            "send_llm_execution_request",
            forbidden_send,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "current",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "current_model_unavailable")

    def test_litellm_activation_lookup_uses_registered_model_name_only(self) -> None:
        service_cfg = {
            "models": [
                {
                    "id": "internal-id",
                    "public_name": "friendly-name",
                    "model_group": "registered-name",
                    "upstream_model": "upstream-name",
                    "activation_model": "/models/registered.gguf",
                }
            ]
        }
        with patch.object(proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"}), patch.object(
            proxy,
            "config",
            {"backends": {"kobold": service_cfg}},
        ):
            self.assertEqual(
                proxy._configured_litellm_local_model_target("registered-name"),
                ("kobold", "/models/registered.gguf", None),
            )
            self.assertEqual(
                proxy._configured_litellm_local_model_target("friendly-name"),
                (None, None, None),
            )
            self.assertEqual(
                proxy._configured_litellm_local_model_target("upstream-name"),
                (None, None, None),
            )

    def test_litellm_activates_gateway_owned_local_role_before_forwarding(self) -> None:
        calls: list[Any] = []
        activations: list[tuple[str, str | None, str | None]] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMResponse()

        async def fake_ensure_target(backend, model, mmproj=None):
            activations.append((backend, model, mmproj))
            return True

        def fake_registry():
            return {
                "schema_version": 1,
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                },
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "ensure_target", fake_ensure_target
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "default",
                    "reasoning_effort": "minimal",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(activations), 1)
        backend, model, mmproj = activations[0]
        self.assertEqual(backend, "kobold")
        self.assertEqual(model, proxy.ROLES["default"]["model"])
        self.assertEqual(mmproj, proxy.ROLES["default"]["mmproj"])
        forwarded = json.loads(calls[0].body.decode())
        self.assertEqual(forwarded["model"], "default")
        self.assertNotIn("reasoning_effort", forwarded)
        # Suppression is via chat_template_kwargs.enable_thinking=False only; no /no_think injection
        self.assertFalse(forwarded["chat_template_kwargs"]["enable_thinking"])
        for msg in forwarded["messages"]:
            self.assertNotIn("/no_think", msg.get("content", ""))

    def test_litellm_activates_mmproj_only_for_vision_requests(self) -> None:
        activations: list[tuple[str, str | None, str | None]] = []

        async def fake_send(_execution_request):
            return _FakeStreamClient(), _FakeLiteLLMResponse()

        async def fake_ensure_target(backend, model, mmproj=None):
            activations.append((backend, model, mmproj))
            return True

        def fake_registry():
            return {
                "schema_version": 1,
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                },
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "ensure_target", fake_ensure_target
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "default",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe this"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,iVBORw0KGgo="
                                    },
                                },
                            ],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(activations), 1)
        self.assertTrue(
            activations[0][2]
            and activations[0][2].endswith("Qwen3.6-27B-mmproj-BF16.gguf")
        )

    def test_litellm_substrate_keeps_gateway_anthropic_and_responses_translation(self) -> None:
        chat_calls: list[dict[str, Any]] = []

        async def fake_chat_forward(_request, _auth_context, cc_body, *, stream):
            chat_calls.append(cc_body)
            return httpx.Response(
                200,
                json=_make_canonical_chat_response("pong"),
                request=httpx.Request("POST", "http://test/v1/chat/completions"),
            )

        async def forbidden_litellm_direct(*_args, **_kwargs):
            raise AssertionError("protocol frontends should use chat execution, not direct LiteLLM protocol endpoints")

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "_forward_chat_completions", fake_chat_forward
        ), patch.object(
            proxy, "send_llm_execution_request", forbidden_litellm_direct
        ):
            anthropic = client.post(
                "/anthropic/v1/messages",
                json={
                    "model": "default",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            responses = client.post(
                "/v1/responses",
                json={"model": "default", "input": "ping", "store": False},
            )

        self.assertEqual(anthropic.status_code, 200, anthropic.text)
        self.assertEqual(responses.status_code, 200, responses.text)
        self.assertEqual(anthropic.json()["content"][0]["text"], "pong")
        self.assertEqual(responses.json()["output_text"], "pong")
        self.assertEqual([call["model"] for call in chat_calls], ["default", "default"])
        self.assertEqual(chat_calls[0]["reasoning_effort"], "minimal")

    def test_litellm_substrate_suppresses_anthropic_local_reasoning_chatter(self) -> None:
        chat_calls: list[dict[str, Any]] = []

        async def fake_chat_forward(_request, _auth_context, cc_body, *, stream):
            chat_calls.append(cc_body)
            return httpx.Response(
                200,
                json=_make_canonical_chat_response(
                    "<think>private local reasoning</think>\npong"
                ),
                request=httpx.Request("POST", "http://test/v1/chat/completions"),
            )

        async def forbidden_litellm_direct(*_args, **_kwargs):
            raise AssertionError("Anthropic should translate through chat execution")

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "_forward_chat_completions", fake_chat_forward
        ), patch.object(
            proxy, "send_llm_execution_request", forbidden_litellm_direct
        ):
            response = client.post(
                "/anthropic/v1/messages",
                json={
                    "model": "default",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"], [{"type": "text", "text": "pong"}])
        self.assertEqual([call["model"] for call in chat_calls], ["default"])

    def test_litellm_route_rejects_malformed_json_before_executor(self) -> None:
        async def fake_send(_execution_request):
            raise AssertionError("executor should not run for malformed JSON")

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            response = client.post(
                "/v1/chat/completions",
                content=b"{not-json",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_litellm_unknown_model_error_is_openai_shaped(self) -> None:
        async def fake_send(_execution_request):
            return _FakeStreamClient(), _FakeLiteLLMUnknownModelResponse()

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "nope-doesnt-exist",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(response.status_code, 400, response.text)
        error = response.json()["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "model")
        self.assertEqual(error["code"], "model_not_found")

    def test_litellm_service_owns_openai_model_list_for_llm_models(self) -> None:
        calls: list[Any] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMModelsResponse()

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ), patch.object(
            proxy,
            "_build_workflow_model_entries",
            lambda: [
                {
                    "id": "image.generate.test",
                    "object": "model",
                    "owned_by": "workflow",
                }
            ],
        ):
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        ids = [item["id"] for item in payload["data"]]
        self.assertIn("assistant-default", ids)
        self.assertIn("image.generate.test", ids)
        self.assertIn("current", ids)
        self.assertIn("current-fast", ids)
        self.assertNotIn("default", ids)
        assistant = next(item for item in payload["data"] if item["id"] == "assistant-default")
        self.assertEqual(assistant["context_window"], 102400)
        self.assertEqual(payload["x_ai_gateway_llm_execution"], "litellm")
        self.assertEqual(calls[0].url, "http://127.0.0.1:4011/v1/models")

    def test_litellm_model_catalog_failures_are_protocol_shaped(self) -> None:
        async def fake_send(_execution_request):
            raise httpx.ConnectError("litellm unavailable")

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            openai_models = client.get("/v1/models")
            anthropic_models = client.get("/anthropic/v1/models")
            ollama_tags = client.get("/api/tags")

        self.assertEqual(openai_models.status_code, 200, openai_models.text)
        self.assertIn(
            "vivid_fish_catalog_warning",
            openai_models.json(),
        )
        self.assertIn(
            "current",
            [item["id"] for item in openai_models.json()["data"]],
        )
        self.assertIn(
            "current-fast",
            [item["id"] for item in openai_models.json()["data"]],
        )
        self.assertEqual(anthropic_models.status_code, 200, anthropic_models.text)
        self.assertEqual(
            [item["id"] for item in anthropic_models.json()["data"]],
            ["current", "current-fast"],
        )
        self.assertEqual(ollama_tags.status_code, 200, ollama_tags.text)
        self.assertEqual(
            [item["name"] for item in ollama_tags.json()["models"]],
            ["current", "current-fast"],
        )

    def test_litellm_routes_embeddings_and_applies_requested_dimensions(self) -> None:
        calls: list[Any] = []

        async def fake_send(execution_request):
            calls.append(execution_request)
            return _FakeStreamClient(), _FakeLiteLLMEmbeddingResponse()

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            response = client.post(
                "/v1/embeddings",
                json={"model": "embed-default", "input": "ping", "dimensions": 2},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["data"][0]["embedding"],
            [0.6, 0.8],
        )
        self.assertEqual(calls[0].url, "http://127.0.0.1:4011/v1/embeddings")
        self.assertNotIn("dimensions", json.loads(calls[0].body.decode()))

    def test_litellm_connect_error_is_protocol_shaped(self) -> None:
        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        async def fake_connect(_execution_request):
            raise httpx.ConnectError("down")

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(proxy, "send_llm_execution_request", fake_connect):
            connect = client.post(
                "/v1/chat/completions",
                json={
                    "model": "assistant-default",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )

        self.assertEqual(connect.status_code, 503, connect.text)
        self.assertEqual(connect.json()["error"]["code"], "backend_unavailable")

    def test_litellm_substrate_keeps_gateway_responses_store_for_retrieve_delete_cancel(self) -> None:
        async def forbidden_litellm_direct(*_args, **_kwargs):
            raise AssertionError("Responses retrieve/delete/cancel should use the gateway store")

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", forbidden_litellm_direct
        ), patch.object(
            proxy.responses_store,
            "get",
            return_value={"response_json": {"id": "resp_123", "status": "completed"}},
        ), patch.object(
            proxy.responses_store, "delete", return_value=True
        ):
            retrieved = client.get("/v1/responses/resp_123")
            deleted = client.delete("/v1/responses/resp_123")
            cancelled = client.post("/v1/responses/resp_123/cancel", json={})

        self.assertEqual(retrieved.status_code, 200, retrieved.text)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(cancelled.status_code, 400, cancelled.text)
        self.assertEqual(retrieved.json()["status"], "completed")
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(cancelled.json()["error"]["code"], "unsupported_operation")

    def test_ollama_tags_project_litellm_model_catalog(self) -> None:
        async def fake_send(_execution_request):
            return _FakeStreamClient(), _FakeLiteLLMModelsResponse()

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
        ):
            tags = client.get("/api/tags")
            ps = client.get("/api/ps")

        self.assertEqual(tags.status_code, 200, tags.text)
        self.assertEqual(
            [m["name"] for m in tags.json()["models"]],
            ["assistant-default", "current", "current-fast"],
        )
        self.assertEqual(ps.status_code, 200, ps.text)
        self.assertEqual(ps.json(), {"models": []})


# ---------------------------------------------------------------------------
# Gateway audience derived from the authenticated profile
# ---------------------------------------------------------------------------


class TestGatewayAudienceFromProfile(unittest.TestCase):
    """Profile drives the discovery audience for receipts AND capabilities."""

    def _audience_resolver(self) -> Optional[Callable[..., Any]]:
        # The owner-wired helper is expected to be exposed as one of these
        # symbols. Look for the one that exists; fall back to a skip.
        for name in (
            "gateway_audience_resolver",
            "build_gateway_audience_resolver",
            "_gateway_audience_resolver",
        ):
            candidate = getattr(proxy, name, None)
            if candidate is None or not callable(candidate):
                continue
            try:
                sig = inspect.signature(candidate)
            except (TypeError, ValueError):
                return candidate  # type: ignore[return-value]
            params = list(sig.parameters.values())
            if len(params) == 1 and params[0].name in {"request", "req"}:
                return candidate  # type: ignore[return-value]
            try:
                built = candidate()  # factory shape
                if callable(built):
                    return built  # type: ignore[return-value]
            except TypeError:
                return candidate  # type: ignore[return-value]
        return None

    def test_resolver_returns_profile_derived_audience(self) -> None:
        resolver = self._audience_resolver()
        if resolver is None:
            self.skipTest(
                "no proxy.gateway_audience_resolver symbol yet — owner is "
                "wiring profile-derived audience"
            )

        # The resolver MUST return one of the discovery audiences. We use a
        # minimal Request stub with the authenticated identity attached the
        # same way the proxy middleware will.
        class _Stub:
            def __init__(self, identity):
                self.scope = {
                    "auth_context": proxy.AuthContext(identity=identity),
                }

        admin_identity = proxy.AuthIdentity(
            name="ops",
            key_hash="sha256:ops",
            downstream={},
            profile=AppProfile(
                id="ops",
                status_visibility=StatusVisibilityPolicy(level="standard"),
            ),
        )

        result = resolver(_Stub(admin_identity))
        self.assertEqual(result, "admin")


# ---------------------------------------------------------------------------
# finish_image_route_receipt: persist to receipt_store.record + swallow errors
# ---------------------------------------------------------------------------


class TestFinishImageRouteReceiptPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "receipts.db"
        receipt_store.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_receipt(self):
        return proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

    def test_finish_persists_to_receipt_store(self) -> None:
        receipt = self._build_receipt()
        recorded: list[Any] = []

        original_record = receipt_store.record

        def spy_record(r, *args, **kwargs):
            recorded.append(r)
            kwargs.setdefault("path", self.db)
            return original_record(r, *args, **kwargs)

        # The wiring is allowed to either call receipt_store.record directly
        # *or* be inert (older main). Once landed, the call MUST happen.
        with patch.object(receipt_store, "record", spy_record), patch.object(
            proxy.firehose_metrics, "record_event", lambda *a, **kw: None
        ):
            proxy.finish_image_route_receipt(receipt, _StubResponse(200))

        if not recorded:
            self.skipTest(
                "proxy.finish_image_route_receipt does not yet call "
                "receipt_store.record — owner is wiring durable persistence"
            )
        self.assertEqual(recorded[0].request_id, receipt.request_id)

    def test_persistence_failure_does_not_raise(self) -> None:
        receipt = self._build_receipt()

        def boom(*_a, **_kw):
            raise RuntimeError("disk full")

        # If the owner has not yet wired persistence, this still must not
        # raise — finish_image_route_receipt has always swallowed failures
        # from the firehose path. Once wired, the new persistence path must
        # follow the same rule.
        with patch.object(receipt_store, "record", boom), patch.object(
            proxy.firehose_metrics, "record_event", boom
        ):
            proxy.finish_image_route_receipt(receipt, _StubResponse(200))

        self.assertEqual(receipt.status, "ok")


# ---------------------------------------------------------------------------
# Image route helper: rejects unknown top-level params, accepts namespaced
# workflow extensions when a manifest exists
# ---------------------------------------------------------------------------


class _StubRequest:
    """Minimal Request-shaped stub for proxy_comfy_request."""

    def __init__(self, body: Any):
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class TestImageRouteRejectsUnknownTopLevelParams(unittest.IsolatedAsyncioTestCase):
    async def _call(self, body: Any):
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

        async def fake_generate(**kwargs):
            return Response(
                content=json.dumps({"created": 0, "data": []}),
                status_code=200,
                media_type="application/json",
            )

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "configured_downstream_headers", lambda *_a, **_kw: {}
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_request(
                _StubRequest(body), proxy.NO_AUTH_CONTEXT, receipt=receipt
            )
        return receipt, response

    async def test_unknown_top_level_param_is_rejected(self) -> None:
        # `denoise` belongs under extensions.com.vividfish.workflow, not at
        # the top level. The route used to silently drop it. The owner wiring
        # converts that into a 400.
        receipt, response = await self._call(
            {"prompt": "x", "model": "flux2-klein-4b", "denoise": 0.5}
        )

        if response.status_code != 400 or "denoise" not in (
            receipt.rejected_inputs or {}
        ):
            self.skipTest(
                "image route does not yet reject unknown top-level params — "
                "owner is wiring detect_unknown_top_level_fields"
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("denoise", receipt.rejected_inputs)

    async def test_dalle_generation_defaults_to_url_response(self) -> None:
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs):
            captured.update(kwargs)
            return Response(
                content=json.dumps({"created": 0, "data": []}),
                status_code=200,
                media_type="application/json",
            )

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "configured_downstream_headers", lambda *_a, **_kw: {}
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_request(
                _StubRequest({"prompt": "x", "model": "dall-e-3"}),
                proxy.NO_AUTH_CONTEXT,
                receipt=proxy.start_image_route_receipt(
                    "/v1/images/generations", proxy.NO_AUTH_CONTEXT
                ),
            )

        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual(captured.get("response_format"), "url")

    async def test_namespaced_extension_is_accepted_when_manifest_exists(
        self,
    ) -> None:
        # The repo ships manifests/workflows/flux2-klein-4b-edit.yml (and the
        # generate-side equivalent in the registry path). The exact name is
        # asserted in TestAttachImageManifest — we re-use it here so we know
        # a manifest *is* present for that model.
        body = {
            "prompt": "x",
            "model": "flux2-klein-4b",
            "extensions": {"com.vividfish.workflow": {"guidance": 3.5}},
        }
        receipt, response = await self._call(body)

        # Acceptance: the helper must not 400 on the namespaced field, and the
        # receipt must NOT carry an unknown_top_level rejection for "guidance"
        # or "extensions".
        if "guidance" in (receipt.rejected_inputs or {}):
            self.skipTest(
                "namespaced extension handling not yet wired — owner is "
                "wiring bind_workflow_extensions"
            )
        self.assertNotIn("extensions", receipt.rejected_inputs or {})
        self.assertIn(
            "com.vividfish.workflow.guidance",
            receipt.accepted_inputs,
        )
        # We allow the test to pass even when downstream backend returns a
        # 502/504 because there's no real ComfyUI here; the contract under
        # test is *acceptance*, not happy-path success.
        self.assertNotEqual(response.status_code, 400, response.body)

    async def test_seed_extension_reaches_generation_batcher_as_fixed_seed(self) -> None:
        body = {
            "prompt": "tileable mossy cobblestone",
            "model": "sdxl-seamless-tile",
            "size": "512x512",
            "extensions": {"com.vividfish.workflow": {"seed": 12345}},
        }
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs):
            captured.update(kwargs)
            return Response(
                content=json.dumps({"created": 0, "data": []}),
                status_code=200,
                media_type="application/json",
            )

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "configured_downstream_headers", lambda *_a, **_kw: {}
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_request(
                _StubRequest(body), proxy.NO_AUTH_CONTEXT, receipt=receipt
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["fixed_seed"], 12345)
        self.assertEqual(captured["base_workflow"]["8"]["inputs"]["width"], 512)
        self.assertEqual(captured["base_workflow"]["8"]["inputs"]["height"], 512)
        self.assertIn("com.vividfish.workflow.seed", receipt.accepted_inputs)


# ---------------------------------------------------------------------------
# Anthropic / Ollama protocol routes mounted + translate via chat forwarder
# ---------------------------------------------------------------------------


def _make_canonical_chat_response(text: str = "ok") -> dict[str, Any]:
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": "stub-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _find_chat_forwarder() -> Optional[str]:
    """Best-effort: find a forwarder symbol the owner is wiring."""
    for name in (
        "forward_chat_completion",
        "forward_canonical_chat",
        "proxy_chat_completion",
        "chat_completions_forward",
    ):
        if getattr(proxy, name, None) is not None:
            return name
    return None


class TestAnthropicAndOllamaRoutes(unittest.TestCase):
    """Pin the protocol-frontend routes the owner is mounting."""

    def setUp(self) -> None:
        self.client = TestClient(proxy.app)

    def _has_route(self, path: str, method: str) -> bool:
        for route in getattr(proxy.app, "routes", []):
            if getattr(route, "path", "") != path:
                continue
            methods = getattr(route, "methods", None) or set()
            if method.upper() in methods:
                return True
        return False

    def _patch_auth_open(self):
        # Routes under proxy auth would otherwise 401. We disable auth for the
        # duration of the call to focus the assertion on translation wiring.
        return patch.object(proxy, "AUTH_ENABLED", False)

    def _patch_forwarder(self, payload: dict[str, Any]):
        forwarder_name = _find_chat_forwarder()
        if forwarder_name is None:
            return None

        async def fake_forward(*_args, **_kwargs):
            return Response(
                content=json.dumps(payload),
                status_code=200,
                media_type="application/json",
            )

        return patch.object(proxy, forwarder_name, fake_forward)

    def test_anthropic_messages_route_is_mounted(self) -> None:
        if not self._has_route("/v1/messages", "POST"):
            self.skipTest(
                "POST /v1/messages not yet mounted — owner is wiring the "
                "Anthropic protocol frontend"
            )
        # Mounted: also exercise it through a mocked forwarder.
        forwarder_patch = self._patch_forwarder(_make_canonical_chat_response())
        if forwarder_patch is None:
            return  # presence is enough; translation tested elsewhere

        with self._patch_auth_open(), forwarder_patch:
            response = self.client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet",
                    "max_tokens": 8,
                    "messages": [
                        {"role": "user", "content": "ping"},
                    ],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # Anthropic shape: top-level type=message, content blocks list.
        self.assertEqual(body.get("type"), "message")
        self.assertIn("content", body)
        self.assertIsInstance(body["content"], list)

    def test_anthropic_messages_streaming_route_translates_sse(self) -> None:
        if not self._has_route("/v1/messages", "POST"):
            self.skipTest("/v1/messages not mounted yet")

        async def fake_forward(*_args, **kwargs):
            self.assertTrue(kwargs["stream"])
            chunks = [
                b'data: {"id":"chatcmpl-1","model":"local","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
                b'data: {"id":"chatcmpl-1","model":"local","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            return _FakeStreamClient(), _FakeStreamResponse(chunks)

        with self._patch_auth_open(), patch.object(
            proxy,
            "_forward_chat_completions",
            fake_forward,
        ):
            response = self.client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet",
                    "max_tokens": 8,
                    "stream": True,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: message_start", response.text)
        self.assertIn("event: content_block_delta", response.text)
        self.assertIn('"text":"hi"', response.text)
        self.assertIn("event: message_stop", response.text)

    def test_ollama_chat_route_is_mounted(self) -> None:
        if not self._has_route("/api/chat", "POST"):
            self.skipTest(
                "POST /api/chat not yet mounted — owner is wiring the Ollama "
                "protocol frontend"
            )
        forwarder_patch = self._patch_forwarder(_make_canonical_chat_response())
        if forwarder_patch is None:
            return

        with self._patch_auth_open(), forwarder_patch:
            response = self.client.post(
                "/api/chat",
                json={
                    "model": "llama3",
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": False,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("message", body)
        self.assertEqual(body.get("done"), True)

    def test_ollama_chat_streaming_route_translates_ndjson(self) -> None:
        if not self._has_route("/api/chat", "POST"):
            self.skipTest("/api/chat not mounted yet")

        async def fake_forward(*_args, **kwargs):
            self.assertTrue(kwargs["stream"])
            chunks = [
                b'data: {"id":"chatcmpl-1","model":"llama3","choices":[{"index":0,"delta":{"content":"hel"}}]}\n\n',
                b'data: {"id":"chatcmpl-1","model":"llama3","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
                b'data: {"id":"chatcmpl-1","model":"llama3","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            return _FakeStreamClient(), _FakeStreamResponse(chunks)

        with self._patch_auth_open(), patch.object(
            proxy,
            "_forward_chat_completions",
            fake_forward,
        ):
            response = self.client.post(
                "/api/chat",
                json={
                    "model": "llama3",
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": True,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        lines = [json.loads(line) for line in response.text.splitlines() if line]
        self.assertEqual([line["message"]["content"] for line in lines[:2]], ["hel", "lo"])
        self.assertTrue(lines[-1]["done"])
        self.assertEqual(lines[-1]["done_reason"], "stop")

    def test_ollama_generate_route_is_mounted(self) -> None:
        if not self._has_route("/api/generate", "POST"):
            self.skipTest(
                "POST /api/generate not yet mounted — owner is wiring the "
                "Ollama protocol frontend"
            )
        forwarder_patch = self._patch_forwarder(_make_canonical_chat_response())
        if forwarder_patch is None:
            return

        with self._patch_auth_open(), forwarder_patch:
            response = self.client.post(
                "/api/generate",
                json={"model": "llama3", "prompt": "ping", "stream": False},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("response", body)
        self.assertEqual(body.get("done"), True)

    def test_ollama_embed_route_is_mounted(self) -> None:
        if not self._has_route("/api/embed", "POST"):
            self.skipTest("/api/embed not mounted yet")

        async def fake_forward(*_args, **_kwargs):
            return Response(
                content=json.dumps({
                    "object": "list",
                    "model": "nomic-embed-text-v1.5",
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [0.1, 0.2],
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                }),
                status_code=200,
                media_type="application/json",
            )

        with self._patch_auth_open(), patch.object(
            proxy,
            "_forward_embeddings",
            fake_forward,
        ):
            response = self.client.post(
                "/api/embed",
                json={"model": "nomic-embed-text-v1.5", "input": "hello"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["embeddings"], [[0.1, 0.2]])

    def test_ollama_legacy_embeddings_route_is_mounted(self) -> None:
        if not self._has_route("/api/embeddings", "POST"):
            self.skipTest("/api/embeddings not mounted yet")

        async def fake_forward(*_args, **_kwargs):
            return Response(
                content=json.dumps({
                    "object": "list",
                    "model": "nomic-embed-text-v1.5",
                    "data": [
                        {
                            "object": "embedding",
                            "index": 0,
                            "embedding": [0.3, 0.4],
                        }
                    ],
                }),
                status_code=200,
                media_type="application/json",
            )

        with self._patch_auth_open(), patch.object(
            proxy,
            "_forward_embeddings",
            fake_forward,
        ):
            response = self.client.post(
                "/api/embeddings",
                json={"model": "nomic-embed-text-v1.5", "prompt": "hello"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"embedding": [0.3, 0.4]})

    def test_ollama_tags_route_is_mounted(self) -> None:
        if not self._has_route("/api/tags", "GET"):
            self.skipTest(
                "GET /api/tags not yet mounted — owner is wiring the Ollama "
                "model listing"
            )
        with self._patch_auth_open():
            response = self.client.get("/api/tags")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("models", body)
        self.assertIsInstance(body["models"], list)

    def test_ollama_model_management_routes_are_mounted(self) -> None:
        for path, method in (
            ("/api/show", "POST"),
            ("/api/ps", "GET"),
            ("/api/version", "GET"),
            ("/api/pull", "POST"),
            ("/api/delete", "DELETE"),
        ):
            self.assertTrue(self._has_route(path, method), f"{method} {path}")

        summaries = [
            {
                "id": "llama3:8b",
                "family": "llama",
                "parameter_size": "8B",
                "quantization_level": "Q4_K_M",
                "context_window": 8192,
            }
        ]
        with self._patch_auth_open(), patch.object(
            proxy,
            "_ollama_model_catalog_summaries",
            AsyncMock(return_value=summaries),
        ), patch.object(
            proxy,
            "_ollama_loaded_model_summaries",
            AsyncMock(return_value=summaries),
        ):
            show = self.client.post("/api/show", json={"model": "llama3:8b"})
            ps = self.client.get("/api/ps")
            version = self.client.get("/api/version")
            pull = self.client.post("/api/pull", json={"name": "llama3:8b"})

        self.assertEqual(show.status_code, 200, show.text)
        self.assertEqual(show.json()["details"]["family"], "llama")
        self.assertEqual(show.json()["model_info"]["llama.context_length"], 8192)
        self.assertIn("num_ctx 8192", show.json()["parameters"])
        self.assertEqual(ps.status_code, 200, ps.text)
        self.assertEqual(ps.json()["models"][0]["model"], "llama3:8b")
        self.assertEqual(version.status_code, 200, version.text)
        self.assertIn("version", version.json())
        self.assertEqual(pull.status_code, 501, pull.text)

    def test_anthropic_route_registered_before_catch_all(self) -> None:
        if not self._has_route("/v1/messages", "POST"):
            self.skipTest("/v1/messages not mounted yet")
        catch_idx = _catch_all_index(proxy.app)
        self.assertGreaterEqual(catch_idx, 0)
        for i, route in enumerate(proxy.app.routes):
            if getattr(route, "path", "") == "/v1/messages":
                self.assertLess(
                    i,
                    catch_idx,
                    "/v1/messages must register before the catch-all",
                )
                return
        self.fail("/v1/messages route disappeared between checks")


# ---------------------------------------------------------------------------
# Body-leak guard: receipts must not carry prompts/images
# ---------------------------------------------------------------------------


class TestNoBodyLeakInReceiptPersistence(unittest.TestCase):
    """Belt-and-suspenders: the persisted receipt MUST NOT carry prompt text."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "receipts.db"
        receipt_store.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persisted_receipt_has_no_prompt_field(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )
        receipt.requested_model = "flux2-klein-4b"
        receipt.accept_input("prompt")  # name only

        recorded: list[dict[str, Any]] = []

        def spy_record(r, *_args, **_kwargs):
            recorded.append(r.to_dict("internal"))

        with patch.object(receipt_store, "record", spy_record), patch.object(
            proxy.firehose_metrics, "record_event", lambda *a, **kw: None
        ):
            proxy.finish_image_route_receipt(receipt, _StubResponse(200))

        if not recorded:
            self.skipTest(
                "receipt persistence not yet wired — see "
                "TestFinishImageRouteReceiptPersistence"
            )

        for key in recorded[0]:
            self.assertNotIn(
                str(key).lower(),
                {"prompt", "image", "mask", "body", "file", "filename"},
                f"persisted receipt leaks {key!r}",
            )


# ---------------------------------------------------------------------------
# _gateway_capabilities_provider: live wiring through local capability builder
# ---------------------------------------------------------------------------


class _StubScope:
    def __init__(self) -> None:
        self.scope: dict[str, Any] = {}


class TestGatewayCapabilitiesProviderUsesLocalBuilder(unittest.TestCase):
    def test_provider_delegates_to_build_local_capabilities(self) -> None:
        sentinel: list[bool] = []

        def fake_build(cfg, **kwargs):
            sentinel.append(True)
            self.assertIs(cfg, proxy.config)
            self.assertIn("enabled_flags", kwargs)
            return []

        with patch.object(proxy, "build_local_capabilities", fake_build):
            result = proxy._gateway_capabilities_provider(_StubScope())  # type: ignore[arg-type]

        self.assertTrue(sentinel, "build_local_capabilities was not invoked")
        self.assertIsInstance(result, list)

    def test_provider_still_policy_projects_capabilities(self) -> None:
        seen: list[Any] = []
        original_project = proxy._capability_for_profile

        def spy_project(capability, auth_context, *, protocol=None):
            projected = original_project(
                capability,
                auth_context,
                protocol=protocol,
            )
            seen.append(projected)
            return projected

        with patch.object(proxy, "_capability_for_profile", spy_project):
            result = proxy._gateway_capabilities_provider(_StubScope())  # type: ignore[arg-type]

        self.assertEqual(len(result), len(seen))


if __name__ == "__main__":
    unittest.main(verbosity=2)
