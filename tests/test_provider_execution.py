from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

from gateway import provider_execution, receipt_store
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
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
                            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
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
