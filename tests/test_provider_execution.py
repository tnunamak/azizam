from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

from gateway import provider_execution, receipt_store
from gateway.backends.llm_execution import LiteLlmRuntimeConfig, sanitize_litellm_execution_request
from gateway.policy import AppProfile, RemoteProviderPolicy
from gateway.providers import ProviderRef


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
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
        receive,
    )


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
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._body = body if body is not None else b"{}"
        self._chunks = chunks or []
        self.closed = False

    async def aread(self) -> bytes:
        return self._body

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _provider() -> ProviderRef:
    return ProviderRef(
        id="openrouter",
        base_url="https://openrouter.ai/api/v1",
        protocols=("openai",),
        api_key_env="OPENROUTER_API_KEY",
        cost_class="paid-remote",
        price={
            "input_usd_per_million_tokens": "1.00",
            "output_usd_per_million_tokens": "2.00",
        },
        config={
            "models": [{"id": "openai/gpt-4.1-mini"}],
            "price": {
                "input_usd_per_million_tokens": "1.00",
                "output_usd_per_million_tokens": "2.00",
            },
        },
    )


def _profile(
    *,
    providers: set[str] | None = None,
    allow_paid_fallback: bool = False,
) -> AppProfile:
    return AppProfile(
        id="paid-app",
        remote=RemoteProviderPolicy(
            providers=frozenset({"openrouter"} if providers is None else providers),
            max_usd_per_request=Decimal("1.00"),
            budget_usd_per_day=Decimal("5.00"),
            budget_usd_per_month=Decimal("20.00"),
            allow_paid_fallback=allow_paid_fallback,
        ),
    )


async def _body(response) -> bytes:
    if hasattr(response, "body"):
        return response.body
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


class ProviderExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "provider_spend.db"
        self.runtime = LiteLlmRuntimeConfig(
            enabled=True,
            base_url="http://litellm.test",
            api_key="sk-litellm",
            service_id="litellm",
        )
        self.provider = _provider()
        self.profile = _profile()
        self.body = {
            "model": "openrouter/openai/gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        }
        self.receipts = []
        self.env_patch = patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-provider"})
        self.begin_patch = patch.object(receipt_store, "begin", lambda _receipt: None)
        self.finalize_patch = patch.object(
            receipt_store,
            "finalize",
            side_effect=lambda receipt: self.receipts.append(receipt) or True,
        )
        self.env_patch.start()
        self.begin_patch.start()
        self.finalize_patch.start()

    def tearDown(self) -> None:
        self.finalize_patch.stop()
        self.begin_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    async def _execute(self, *, send=None, **kwargs):
        send = send or AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={
                        "content-type": "application/json",
                        "x-litellm-response-cost": "0.000123",
                    },
                    body=json.dumps(
                        {
                            "model": "openrouter/openai/gpt-4.1-mini",
                            "choices": [],
                            "usage": {
                                "cost": "0.000123",
                                "prompt_tokens": 3,
                                "completion_tokens": 2,
                            },
                        }
                    ).encode(),
                ),
            )
        )
        with patch.object(provider_execution, "send_llm_execution_request", send):
            response = await provider_execution.execute_paid_litellm(
                _request(self.body),
                request_path="/v1/chat/completions",
                body=self.body,
                provider=self.provider,
                profile=self.profile,
                runtime=self.runtime,
                ledger_path=self.ledger,
                **kwargs,
            )
        return response, send

    async def test_paid_app_success_records_cost_receipt(self) -> None:
        response, send = await self._execute()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-gateway-cost-usd"], "0.000123000")
        send.assert_awaited_once()
        self.assertEqual(len(self.receipts), 1)
        receipt = self.receipts[0]
        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(receipt.cost["provider"], "openrouter")
        self.assertEqual(receipt.cost["usd"], "0.000123000")

    async def test_no_permission_denies_before_provider_call(self) -> None:
        send = AsyncMock()
        self.profile = _profile(providers=set())

        response, send = await self._execute(send=send)

        self.assertEqual(response.status_code, 403)
        body = json.loads((await _body(response)).decode())
        self.assertEqual(body["error"]["code"], "remote_provider_not_allowed")
        self.assertIn("openrouter", body["error"]["message"])
        send.assert_not_called()

    async def test_delegated_litellm_request_is_denied_before_provider_call(self) -> None:
        send = AsyncMock()

        response, send = await self._execute(send=send, delegated=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn("remote_provider_not_allowed", (await _body(response)).decode())
        send.assert_not_called()

    async def test_unknown_remote_model_is_denied_before_provider_call(self) -> None:
        send = AsyncMock()
        self.body = {**self.body, "model": "openrouter/not-listed"}

        response, send = await self._execute(send=send)

        self.assertEqual(response.status_code, 403)
        self.assertIn("allow-list", (await _body(response)).decode())
        send.assert_not_called()

    async def test_vendor_401_403_map_to_502_provider_auth_failed(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                self.receipts.clear()
                send = AsyncMock(
                    return_value=(
                        _Client(),
                        _Response(status_code=status, body=b'{"error":"vendor secret text"}'),
                    )
                )

                response, _ = await self._execute(send=send)

                self.assertEqual(response.status_code, 502)
                body = (await _body(response)).decode()
                self.assertIn("provider_auth_failed", body)
                self.assertNotIn("vendor secret text", body)

    async def test_timeout_maps_to_504(self) -> None:
        send = AsyncMock(side_effect=httpx.TimeoutException("slow"))

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 504)
        self.assertIn("provider_timeout", (await _body(response)).decode())

    async def test_rate_limit_maps_to_429_with_retry_after(self) -> None:
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    status_code=429,
                    headers={"content-type": "application/json", "retry-after": "17"},
                    body=b'{"error":{"message":"too many"}}',
                ),
            )
        )

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "17")
        self.assertIn("provider_rate_limited", (await _body(response)).decode())

    async def test_litellm_408_timeout_maps_to_504_provider_timeout(self) -> None:
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    status_code=408,
                    headers={"content-type": "application/json"},
                    body=b'{"error":{"message":"upstream timed out"}}',
                ),
            )
        )

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 504)
        self.assertIn("provider_timeout", (await _body(response)).decode())

    async def test_invalid_stream_options_do_not_reserve_budget_or_dispatch(self) -> None:
        self.body = {**self.body, "stream": True, "stream_options": ["invalid"]}
        send = AsyncMock()

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 400)
        self.assertIn("stream_options", (await _body(response)).decode())
        send.assert_not_called()
        self.assertFalse(self.ledger.exists())

    async def test_paid_request_overrides_caller_num_retries_to_zero_outbound(self) -> None:
        self.body = {**self.body, "num_retries": 20}
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={
                        "content-type": "application/json",
                        "x-litellm-response-cost": "0.000123",
                    },
                    body=json.dumps({"model": "openrouter/openai/gpt-4.1-mini", "choices": []}).encode(),
                ),
            )
        )

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 200)
        outbound = json.loads(send.await_args.args[0].body.decode())
        self.assertEqual(outbound["num_retries"], 0)
        self.assertEqual(outbound["max_retries"], 0)

    async def test_paid_max_output_tokens_is_not_dropped_after_estimation(self) -> None:
        self.provider = ProviderRef(
            id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            protocols=("openai",),
            api_key_env="OPENROUTER_API_KEY",
            cost_class="paid-remote",
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "10",
            },
            config={
                "models": [{"id": "openai/gpt-4.1-mini"}],
                "price": {
                    "input_usd_per_million_tokens": "0",
                    "output_usd_per_million_tokens": "10",
                },
            },
        )
        self.profile = self.profile.with_overrides(
            remote=RemoteProviderPolicy(
                providers=frozenset({"openrouter"}),
                max_usd_per_request=Decimal("0.0001"),
                budget_usd_per_day=Decimal("5.00"),
                budget_usd_per_month=Decimal("20.00"),
            )
        )
        self.body = {
            "model": "openrouter/openai/gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_output_tokens": 1,
        }
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={
                        "content-type": "application/json",
                        "x-litellm-response-cost": "0.100000000",
                    },
                    body=json.dumps(
                        {
                            "model": "openrouter/openai/gpt-4.1-mini",
                            "choices": [],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 10000},
                        }
                    ).encode(),
                ),
            )
        )

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 400)
        self.assertIn("paid_option_unsupported", (await _body(response)).decode())
        send.assert_not_called()
        self.assertFalse(self.ledger.exists())

    async def test_paid_invalid_output_limits_are_rejected_before_dispatch(self) -> None:
        for field in ("max_tokens", "max_completion_tokens"):
            for value in (True, 0, -1, "8", 1.5):
                with self.subTest(field=field, value=value):
                    self.receipts.clear()
                    self.body = {
                        "model": "openrouter/openai/gpt-4.1-mini",
                        "messages": [{"role": "user", "content": "hi"}],
                        field: value,
                    }
                    send = AsyncMock()

                    response, _ = await self._execute(send=send)

                    self.assertEqual(response.status_code, 400)
                    body = json.loads((await _body(response)).decode())
                    self.assertEqual(body["error"]["code"], "invalid_request")
                    self.assertEqual(body["error"]["param"], field)
                    send.assert_not_called()
                    self.assertFalse(self.ledger.exists())

    async def test_paid_billing_options_without_pricing_are_rejected_before_dispatch(self) -> None:
        for option in (
            {
                "model": "openrouter/openai/gpt-4o-search-preview",
                "web_search_options": {"search_context_size": "high"},
            },
            {"service_tier": "priority"},
            {"audio": {"voice": "alloy", "format": "mp3"}},
            {"modalities": ["text", "audio"]},
            {"prediction": {"type": "content", "content": "cached answer"}},
            {"store": True},
            {"background": True},
            {"tools": [{"type": "web_search"}]},
            {"tools": [{"type": "code_interpreter"}]},
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "url",
                                    "url": "https://example.test/large.pdf",
                                },
                            },
                        ],
                    },
                ],
            },
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "cache this",
                                "cache_control": {"type": "ephemeral"},
                            },
                        ],
                    },
                ],
            },
            {"model": "openrouter/openai/gpt-4.1-mini:online"},
            {"unknown_paid_option": "must reject"},
        ):
            with self.subTest(option=option):
                self.receipts.clear()
                if self.ledger.exists():
                    self.ledger.unlink()
                option = dict(option)
                self.provider = ProviderRef(
                    id="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    protocols=("openai",),
                    api_key_env="OPENROUTER_API_KEY",
                    cost_class="paid-remote",
                    price={
                        "input_usd_per_million_tokens": "2.50",
                        "output_usd_per_million_tokens": "10.00",
                    },
                    config={
                        "models": [
                            {"id": "openai/gpt-4.1-mini"},
                            {"id": "openai/gpt-4o-search-preview"},
                            {"id": "openai/gpt-4.1-mini:online"},
                        ],
                        "price": {
                            "input_usd_per_million_tokens": "2.50",
                            "output_usd_per_million_tokens": "10.00",
                        },
                    },
                )
                self.profile = self.profile.with_overrides(
                    remote=RemoteProviderPolicy(
                        providers=frozenset({"openrouter"}),
                        max_usd_per_request=Decimal("0.002"),
                        budget_usd_per_day=Decimal("5.00"),
                        budget_usd_per_month=Decimal("20.00"),
                    )
                )
                self.body = {
                    "model": option.get("model", "openrouter/openai/gpt-4.1-mini"),
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    **{key: value for key, value in option.items() if key != "model"},
                }
                send = AsyncMock(
                    return_value=(
                        _Client(),
                        _Response(
                            headers={
                                "content-type": "application/json",
                                "x-litellm-response-cost": "0.050010000",
                            },
                            body=json.dumps(
                                {
                                    "model": self.body["model"],
                                    "choices": [],
                                    "usage": {
                                        "prompt_tokens": 650,
                                        "completion_tokens": 1,
                                    },
                                }
                            ).encode(),
                        ),
                    )
                )

                response, _ = await self._execute(send=send)

                self.assertEqual(response.status_code, 400)
                body = json.loads((await _body(response)).decode())
                self.assertEqual(body["error"]["code"], "paid_option_unsupported")
                send.assert_not_called()
                self.assertFalse(self.ledger.exists())

    async def test_paid_guard_ignores_function_parameter_schema_keywords(self) -> None:
        provider = ProviderRef(
            id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            protocols=("openai",),
            api_key_env="OPENROUTER_API_KEY",
            cost_class="paid-remote",
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "0",
            },
            config={"models": [{"id": "openai/gpt-4.1-mini"}]},
        )
        payload = provider_execution._paid_priced_outbound_payload(
            {
                "model": "openrouter/openai/gpt-4.1-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "save",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "cache_control": {"type": "string"},
                                    "file": {"type": "string"},
                                },
                            },
                        },
                    }
                ],
            },
            provider=provider,
        )

        self.assertIsInstance(payload, dict)

    async def test_paid_guard_rejects_operational_payload_that_looks_like_schema(self) -> None:
        self.body = {
            "model": "openrouter/openai/gpt-4.1-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "object",
                            "properties": {},
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
            "max_tokens": 1,
        }
        send = AsyncMock()

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 400)
        body = json.loads((await _body(response)).decode())
        self.assertEqual(body["error"]["code"], "paid_option_unsupported")
        self.assertEqual(body["error"]["param"], "cache_control")
        send.assert_not_called()

    async def test_paid_declared_price_dimensions_allow_known_paid_features(self) -> None:
        provider = ProviderRef(
            id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            protocols=("openai",),
            api_key_env="OPENROUTER_API_KEY",
            cost_class="paid-remote",
            price={
                "input_usd_per_million_tokens": "0",
                "output_usd_per_million_tokens": "0",
                "dimensions": {
                    "document": {"enabled": True},
                    "cache_control": {"enabled": True},
                    "model_modifier:online": {"enabled": True},
                },
            },
            config={"models": [{"id": "openai/gpt-4.1-mini:online"}]},
        )

        payload = provider_execution._paid_priced_outbound_payload(
            {
                "model": "openrouter/openai/gpt-4.1-mini:online",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {"type": "url", "url": "https://example.test/a.pdf"},
                            },
                            {
                                "type": "text",
                                "text": "cache this",
                                "cache_control": {"type": "ephemeral"},
                            },
                        ],
                    }
                ],
                "max_tokens": 1,
            },
            provider=provider,
        )

        self.assertIsInstance(payload, dict)

    async def test_paid_estimator_sees_exact_sanitized_outbound_payload(self) -> None:
        self.body = {
            "model": "openrouter/openai/gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "max_completion_tokens": 8,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "web_search_options": {"type": "string"},
                                "service_tier": {"type": "string"},
                            },
                        },
                    },
                }
            ],
            "metadata": {"model_group": "openrouter/other-paid"},
            "fallbacks": [{"openrouter/openai/gpt-4.1-mini": ["openrouter/other-paid"]}],
            "router_settings_override": {
                "model_group_retry_policy": {"openrouter/openai/gpt-4.1-mini": {"TimeoutErrorRetries": 2}}
            },
        }
        estimated_payloads: list[dict] = []
        outbound_payloads: list[dict] = []
        real_estimate = provider_execution.providers.estimate_request_cost

        def capture_estimate(provider, payload, *, protocol=None, intent=None):
            estimated_payloads.append(dict(payload))
            return real_estimate(provider, payload, protocol=protocol, intent=intent)

        async def send(request, **_kwargs):
            sanitized = sanitize_litellm_execution_request(request)
            outbound_payloads.append(json.loads(sanitized.body.decode()))
            return (
                _Client(),
                _Response(
                    headers={
                        "content-type": "application/json",
                        "x-litellm-response-cost": "0.000123",
                    },
                    body=json.dumps(
                        {"model": "openrouter/openai/gpt-4.1-mini", "choices": []}
                    ).encode(),
                ),
            )

        with patch.object(provider_execution.providers, "estimate_request_cost", capture_estimate):
            response, _ = await self._execute(send=AsyncMock(side_effect=send))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(estimated_payloads, outbound_payloads)
        self.assertEqual(estimated_payloads[0]["max_tokens"], 8)
        self.assertNotIn("max_completion_tokens", estimated_payloads[0])
        self.assertEqual(
            estimated_payloads[0]["tools"][0]["function"]["parameters"]["properties"]["web_search_options"],
            {"type": "string"},
        )

    def test_paid_priced_field_allowlist_is_explicit(self) -> None:
        self.assertEqual(
            provider_execution.PAID_PRICED_REQUEST_FIELDS,
            frozenset(
                {
                    "best_of",
                    "frequency_penalty",
                    "function_call",
                    "functions",
                    "input",
                    "logit_bias",
                    "logprobs",
                    "max_tokens",
                    "messages",
                    "model",
                    "n",
                    "parallel_tool_calls",
                    "presence_penalty",
                    "prompt",
                    "response_format",
                    "seed",
                    "stop",
                    "stream",
                    "stream_options",
                    "suffix",
                    "temperature",
                    "tool_choice",
                    "tools",
                    "top_logprobs",
                    "top_p",
                    "user",
                }
            ),
        )

    async def test_predispatch_transport_failure_releases_reserved_budget(self) -> None:
        from gateway.backends.llm_execution import LlmExecutionPredispatchError

        send = AsyncMock(side_effect=LlmExecutionPredispatchError("bad request"))

        response, _ = await self._execute(send=send)

        self.assertEqual(response.status_code, 502)
        self.assertIn("provider_unavailable", (await _body(response)).decode())
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute("SELECT status, actual_usd FROM provider_spend").fetchall()
        self.assertEqual(rows, [("released", None)])

    async def test_mixed_case_provider_prefix_uses_paid_provider_gate(self) -> None:
        registry = {
            "services": {
                "openrouter": {
                    "id": "openrouter",
                    "kind": "provider",
                    "enabled": True,
                    "config": {
                        "provider_id": "OpenRouter",
                        "provider_type": "paid-remote",
                        "api_key_env": "OPENROUTER_API_KEY",
                        "models": [{"id": "openai/gpt-4.1-mini"}],
                    },
                },
            },
        }

        provider = provider_execution.provider_for_model(registry, "OpenRouter/openai/gpt-4.1-mini")

        self.assertIsNotNone(provider)
        self.assertEqual(provider.id, "openrouter")

    async def test_stream_cost_is_settled_from_usage_event(self) -> None:
        self.body = {**self.body, "stream": True}
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={"content-type": "text/event-stream"},
                    chunks=[
                        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                        b'data: {"usage":{"cost":"0.000222","input_tokens":10,"output_tokens":4}}\n\n',
                        b"data: [DONE]\n\n",
                    ],
                ),
            )
        )

        response, _ = await self._execute(send=send)
        body = await _body(response)

        self.assertIn(b"[DONE]", body)
        self.assertEqual(len(self.receipts), 1)
        self.assertEqual(self.receipts[0].cost["usd"], "0.000222000")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status, actual_usd FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [("settled", "0.000222000")])

    async def test_stream_cost_settlement_accounts_for_cache_write_usage(self) -> None:
        import litellm
        self.provider = ProviderRef(
            id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            protocols=("openai",),
            api_key_env="OPENROUTER_API_KEY",
            cost_class="paid-remote",
            price={
                "input_usd_per_million_tokens": "3",
                "output_usd_per_million_tokens": "15",
            },
            config={
                "models": [{"id": "anthropic/claude-sonnet-4-5-20250929"}],
                "litellm_model": "anthropic/claude-sonnet-4-5-20250929",
                "price": {
                    "input_usd_per_million_tokens": "3",
                    "output_usd_per_million_tokens": "15",
                },
            },
        )
        self.body = {
            "model": "openrouter/anthropic/claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": True,
        }
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={"content-type": "text/event-stream"},
                    chunks=[
                        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                        (
                            b'data: {"usage":{"prompt_tokens":10000,'
                            b'"prompt_tokens_details":{"cache_creation_tokens":10000,'
                            b'"cache_creation_token_details":{"ephemeral_1h_input_tokens":10000}}}}\n\n'
                        ),
                        (
                            b'data: {"usage":{"prompt_tokens":10000,'
                            b'"completion_tokens":1}}\n\n'
                        ),
                        b"data: [DONE]\n\n",
                    ],
                ),
            )
        )

        # Supply a complete pricing snapshot to the real pinned SDK. Its
        # bundled offline catalog omits the one-hour cache-write price.
        with ExitStack() as prices:
            for model in (
                "claude-sonnet-4-5-20250929",
                "anthropic/claude-sonnet-4-5-20250929",
            ):
                row = litellm.model_cost.get(model)
                if isinstance(row, dict):
                    prices.enter_context(patch.dict(row, {
                        "cache_creation_input_token_cost_above_1hr": 0.000006,
                    }))
            cache_clear = getattr(litellm.get_model_info, "cache_clear", None)
            if callable(cache_clear):
                cache_clear()
            try:
                response, _ = await self._execute(send=send)
                self.assertEqual(response.status_code, 200)
                await _body(response)
            finally:
                if callable(cache_clear):
                    cache_clear()

        self.assertEqual(len(self.receipts), 1)
        self.assertEqual(self.receipts[0].cost["usd"], "0.060015000")
        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status, actual_usd FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [("settled", "0.060015000")])

    async def test_stream_success_without_actual_cost_keeps_reservation_open(self) -> None:
        self.body = {**self.body, "stream": True}
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={
                        "content-type": "text/event-stream",
                        "x-litellm-response-cost": "0",
                    },
                    chunks=[
                        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                        b"data: [DONE]\n\n",
                    ],
                ),
            )
        )

        response, _ = await self._execute(send=send)
        await _body(response)

        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status, actual_usd FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [("reserved", None)])

    async def test_stream_unknown_usage_extension_keeps_reservation_open(self) -> None:
        self.body = {**self.body, "stream": True}
        send = AsyncMock(
            return_value=(
                _Client(),
                _Response(
                    headers={"content-type": "text/event-stream"},
                    chunks=[
                        b'data: {"usage":{"input_tokens":10,"output_tokens":1,"future_billable_units":3}}\n\n',
                        b"data: [DONE]\n\n",
                    ],
                ),
            )
        )

        response, _ = await self._execute(send=send)
        await _body(response)

        with sqlite3.connect(self.ledger) as conn:
            rows = conn.execute(
                "SELECT status, actual_usd FROM provider_spend"
            ).fetchall()
        self.assertEqual(rows, [("reserved", None)])

    async def test_paid_fallback_denied_without_profile_opt_in_before_provider_call(self) -> None:
        send = AsyncMock()

        response, send = await self._execute(send=send, paid_fallback=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn("paid fallback disabled", (await _body(response)).decode())
        send.assert_not_called()

    async def test_paid_fallback_opt_in_can_succeed(self) -> None:
        self.profile = _profile(allow_paid_fallback=True)

        response, send = await self._execute(paid_fallback=True)

        self.assertEqual(response.status_code, 200)
        send.assert_awaited_once()
        self.assertIn("explicit alias selected a paid fallback", self.receipts[0].fallback_reasons)
