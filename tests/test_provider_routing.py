from __future__ import annotations

import asyncio
import copy
import json
import unittest
from starlette.requests import Request
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import proxy
from gateway import litellm_substrate


def _registry() -> dict:
    return {
        "services": {
            "openrouter": {
                "id": "openrouter",
                "kind": "provider",
                "capabilities": ["openai"],
                "config": {
                    "provider_id": "openrouter",
                    "provider_type": "paid-remote",
                    "cost_class": "paid-remote",
                    "protocol": "openai-compatible",
                    "protocols": ["openai"],
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "credential_origin": "https://openrouter.ai",
                    "models": [
                        {
                            "id": "openai/gpt-4.1-mini",
                            "upstream_model": "openai/gpt-4.1-mini",
                        }
                    ],
                    "price": {"input_usd_per_million_tokens": "1.00"},
                },
            }
        }
    }




class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._body = body if body is not None else b"{}"

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        pass


def _request(payload: dict) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {
                "type": "http.request",
                "body": json.dumps(payload).encode(),
                "more_body": False,
            }
        await asyncio.Future()

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
        },
        receive,
    )


class ProviderRoutingTests(unittest.TestCase):
    def test_provider_qualified_model_bypasses_local_openai_globs(self) -> None:
        aliases = {"model_aliases": {"chat": {
            "stable": {"fast": "default-fast"},
            "openai": [{"match": "o*-mini*", "to": "fast"}],
        }}}
        with patch.object(proxy, "config", aliases):
            self.assertIsNone(proxy.resolve_stable_alias("chat", "openrouter/openai/gpt-4.1-mini"))

    def test_alias_paid_fallback_is_explicit_and_marked(self) -> None:
        body = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]}
        request = _request(body)
        alias_config = {
            "backends": {
                "local": {"host": "127.0.0.1", "port": 18123, "enabled": True},
                "openrouter": {"enabled": True, "provider_type": "paid-remote"},
            },
            "model_aliases": {"chat": {
                "stable": {"assistant": ["free", "paid"]},
                "engines": {
                    "free": {"service": "local", "model": "local-test"},
                    "paid": {"service": "openrouter", "model": "openai/gpt-4.1-mini"},
                },
            }},
        }
        handler = AsyncMock(side_effect=[proxy.Response(status_code=503), proxy.Response(status_code=200)])
        with patch.object(proxy, "config", alias_config), patch.object(proxy, "_handle_openai_litellm_request_once", handler):
            response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-fallback"], "free->paid (http_503)")
        self.assertEqual(handler.await_count, 2)
        self.assertEqual(handler.call_args.kwargs["model_override"], "openrouter/openai/gpt-4.1-mini")
        self.assertTrue(request.state.paid_provider_fallback)

    def test_skipped_primary_chat_alias_is_explicit_without_remaining_fallbacks(self) -> None:
        body = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]}
        request = _request(body)
        alias_config = {
            "backends": {
                "local": {"enabled": False},
                "fallback": {"host": "127.0.0.1", "port": 18124, "enabled": True},
            },
            "model_aliases": {"chat": {
                "stable": {"assistant": ["free", "paid"]},
                "engines": {
                    "free": {"service": "local", "model": "local-test"},
                    "paid": {"service": "fallback", "model": "paid-test"},
                },
            }},
        }
        handler = AsyncMock(return_value=proxy.Response(status_code=200))
        with patch.object(proxy, "config", alias_config), patch.object(proxy, "_handle_openai_litellm_request_once", handler):
            response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-fallback"], "free->paid (not_deployed)")
        self.assertEqual(getattr(request.state, "gateway_fallback_reasons"), ["free->paid (not_deployed)"])
        self.assertEqual(handler.await_count, 1)

    def test_no_alias_never_attempts_paid_fallback(self) -> None:
        request = _request({"model": "local-test", "messages": []})
        handler = AsyncMock(return_value=proxy.Response(status_code=503))
        with patch.object(proxy, "config", {"model_aliases": {}}), patch.object(proxy, "_handle_openai_litellm_request_once", handler):
            response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(handler.await_count, 1)

    def test_known_chat_alias_with_no_deployed_candidate_is_503(self) -> None:
        request = _request({"model": "assistant", "messages": []})
        alias_config = {
            "backends": {"local": {"enabled": False}},
            "model_aliases": {"chat": {
                "stable": {"assistant": ["free"]},
                "engines": {"free": {"service": "local", "model": "local-test"}},
            }},
        }
        with patch.object(proxy, "config", alias_config):
            response = asyncio.run(proxy._handle_openai_litellm_request_once(request, "/v1/chat/completions"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["error"]["code"], "model_not_deployed")

    def test_failed_chat_fallback_attempt_reports_actual_substitution(self) -> None:
        body = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]}
        request = _request(body)
        alias_config = {
            "backends": {
                "local": {"host": "127.0.0.1", "port": 18123, "enabled": True},
                "fallback": {"host": "127.0.0.1", "port": 18124, "enabled": True},
            },
            "model_aliases": {"chat": {
                "stable": {"assistant": ["free", "paid"]},
                "engines": {
                    "free": {"service": "local", "model": "local-test"},
                    "paid": {"service": "fallback", "model": "paid-test"},
                },
            }},
        }
        handler = AsyncMock(side_effect=[proxy.Response(status_code=503), proxy.Response(status_code=503)])
        with patch.object(proxy, "config", alias_config), patch.object(proxy, "_handle_openai_litellm_request_once", handler):
            response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["x-gateway-fallback"], "free->paid (http_503)")
        self.assertEqual(getattr(request.state, "gateway_fallback_reasons"), ["free->paid (http_503)"])
        self.assertEqual(handler.await_count, 2)

    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        self.runtime = SimpleNamespace(
            enabled=True,
            base_url="http://litellm.test",
            api_key="sk-litellm",
            service_id="litellm",
        )
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy.ADMIN_STORE, "get_service_registry", return_value=_registry()),
            patch.object(proxy, "_litellm_runtime_config", return_value=self.runtime),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", AsyncMock(return_value=None)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()

    def test_remote_provider_model_dispatches_to_paid_executor(self) -> None:
        execute = AsyncMock(
            return_value=proxy.Response(
                content=b'{"ok":true}',
                status_code=200,
                media_type="application/json",
            )
        )
        prepare = AsyncMock(return_value=(None, {}))

        with patch.object(proxy.provider_execution, "execute_paid_litellm", execute), patch.object(
            proxy, "_prepare_litellm_local_llm_request", prepare
        ), patch.object(proxy, "ensure_target", AsyncMock()) as ensure_target:
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "openrouter/openai/gpt-4.1-mini",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        execute.assert_awaited_once()
        kwargs = execute.call_args.kwargs
        self.assertEqual(kwargs["provider"].id, "openrouter")
        self.assertEqual(kwargs["body"]["model"], "openrouter/openai/gpt-4.1-mini")
        prepare.assert_not_called()
        ensure_target.assert_not_called()

    def test_remote_provider_vision_request_skips_local_activation(self) -> None:
        execute = AsyncMock(
            return_value=proxy.Response(
                content=b'{"ok":true}',
                status_code=200,
                media_type="application/json",
            )
        )

        with patch.object(proxy.provider_execution, "execute_paid_litellm", execute), patch.object(
            proxy, "_prepare_litellm_local_llm_request", AsyncMock()
        ) as prepare, patch.object(proxy, "ensure_target", AsyncMock()) as ensure_target:
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "openrouter/openai/gpt-4.1-mini",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe"},
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
        execute.assert_awaited_once()
        prepare.assert_not_called()
        ensure_target.assert_not_called()

    def test_unprefixed_external_litellm_group_keeps_local_litellm_behavior(self) -> None:
        forward = AsyncMock(
            return_value=proxy.Response(
                content=b'{"local":true}',
                status_code=200,
                media_type="application/json",
            )
        )

        with patch.object(proxy, "_forward_litellm_proxy_request", forward), patch.object(
            proxy, "_prepare_litellm_local_llm_request",
            AsyncMock(side_effect=lambda *args, **kwargs: (None, kwargs["body_json"])),
        ), patch.object(proxy.provider_execution, "execute_paid_litellm", AsyncMock()) as execute:
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "my-litellm-sidecar-group",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        forward.assert_awaited_once()
        execute.assert_not_called()

    def test_unknown_prefixed_provider_model_reaches_executor_for_allowlist_denial(self) -> None:
        execute = AsyncMock(
            return_value=proxy.Response(
                content=json.dumps({
                    "error": {
                        "code": "remote_provider_not_allowed",
                        "message": "Remote model is not declared in the provider model allow-list",
                    }
                }).encode(),
                status_code=403,
                media_type="application/json",
            )
        )

        with patch.object(proxy.provider_execution, "execute_paid_litellm", execute):
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "openrouter/not-listed",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        self.assertEqual(response.status_code, 403, response.text)
        execute.assert_awaited_once()
        self.assertEqual(execute.call_args.kwargs["provider"].id, "openrouter")

    def test_generated_registry_rejects_local_remote_model_name_collision(self) -> None:
        registry = _registry()
        registry["services"]["local"] = {
            "id": "local",
            "kind": "provider",
            "capabilities": ["openai"],
            "config": {
                "protocol": "openai-compatible",
                "base_url": "http://127.0.0.1:8080/v1",
                "models": [
                    {
                        "id": "collision",
                        "model_group": "openrouter/openai/gpt-4.1-mini",
                        "upstream_model": "local-model",
                    }
                ],
            },
        }
        with self.assertRaisesRegex(ValueError, "ambiguous across services"):
            litellm_substrate.compile_litellm_config_from_registry(
                copy.deepcopy(registry)
            )

    def test_lan_local_openai_compatible_backend_is_not_treated_as_paid_remote(self) -> None:
        compiled = litellm_substrate.compile_litellm_substrate_config(
            {
                "lan-llm": {
                    "id": "lan-llm",
                    "kind": "provider",
                    "capabilities": ["openai"],
                    "config": {
                        "protocol": "openai-compatible",
                        "base_url": "http://192.168.1.50:8080/v1",
                        "models": [{"id": "lan-chat", "upstream_model": "qwen"}],
                    },
                }
            }
        )

        self.assertEqual(compiled["model_list"][0]["model_name"], "lan-chat")
        self.assertEqual(compiled["model_list"][0]["litellm_params"]["model"], "openai/qwen")

    def test_litellm_forward_strips_caller_paid_fallback_override(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module

        body = {
            "model": "local-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "fallbacks": [{"local-chat": ["openrouter/openai/gpt-4.1-mini"]}],
        }
        request = _request(body)

        class _CapturingClient:
            def __init__(self) -> None:
                self.content = b""

            def build_request(self, **kwargs):
                self.content = kwargs["content"]
                return object()

            async def send(self, req, stream=True):
                return _Response(body=b'{"ok":true}')

            async def aclose(self) -> None:
                pass

        client = _CapturingClient()
        with patch.object(llm_execution_module.httpx, "AsyncClient", lambda **kwargs: client):
            response = asyncio.run(
                proxy._forward_litellm_proxy_request(
                    request,
                    request_path="/v1/chat/completions",
                    auth_context=proxy.NO_AUTH_CONTEXT,
                    body_json=body,
                    runtime_config=self.runtime,
                )
            )

        self.assertEqual(response.status_code, 200)
        outbound = json.loads(client.content.decode())
        self.assertEqual(outbound["model"], "local-chat")
        self.assertNotIn("fallbacks", outbound)

    def test_litellm_forward_reports_gateway_fallback_state(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module

        body = {
            "model": "paid-chat",
            "messages": [{"role": "user", "content": "hi"}],
        }
        request = _request(body)
        request.state.provider_requested_model = "assistant"
        request.state.gateway_fallback_reasons = ["free->paid (http_503)"]

        class _CapturingClient:
            def __init__(self) -> None:
                self.content = b""

            def build_request(self, **kwargs):
                self.content = kwargs["content"]
                return object()

            async def send(self, req, stream=True):
                return _Response(body=b'{"ok":true}')

            async def aclose(self) -> None:
                pass

        client = _CapturingClient()
        receipts = []
        with patch.object(llm_execution_module.httpx, "AsyncClient", lambda **kwargs: client), \
             patch.object(proxy.receipt_store, "begin", side_effect=lambda receipt: receipts.append(receipt)), \
             patch.object(proxy.receipt_store, "finalize", return_value=True):
            response = asyncio.run(
                proxy._forward_litellm_proxy_request(
                    request,
                    request_path="/v1/chat/completions",
                    auth_context=proxy.NO_AUTH_CONTEXT,
                    body_json=body,
                    runtime_config=self.runtime,
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-fallback"], "free->paid (http_503)")
        receipt = receipts[0]
        self.assertEqual(receipt.requested_model, "assistant")
        self.assertEqual(receipt.resolved_model, "paid-chat")
        self.assertIn("free->paid (http_503)", receipt.fallback_reasons)

    def test_proxy_wrapper_passes_paid_fallback_flag_to_executor(self) -> None:
        execute = AsyncMock(
            return_value=proxy.Response(
                content=b'{"ok":true}',
                status_code=200,
                media_type="application/json",
            )
        )
        body = {
            "model": "openrouter/openai/gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
        }
        request = _request(body)
        request.state.paid_provider_fallback = True
        request.state.provider_requested_model = "assistant-paid"

        with patch.object(proxy.provider_execution, "execute_paid_litellm", execute):
            response = asyncio.run(
                proxy._forward_litellm_proxy_request(
                    request,
                    request_path="/v1/chat/completions",
                    auth_context=proxy.NO_AUTH_CONTEXT,
                    body_json=body,
                    runtime_config=self.runtime,
                )
            )

        self.assertEqual(response.status_code, 200)
        execute.assert_awaited_once()
        self.assertTrue(execute.call_args.kwargs["paid_fallback"])
        self.assertEqual(execute.call_args.kwargs["requested_model"], "assistant-paid")
