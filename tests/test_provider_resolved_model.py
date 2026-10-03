from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from gateway import provider_execution, receipt_store
from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from gateway.policy import AppProfile, RemoteProviderPolicy
from gateway.providers import ProviderRef


class _Client:
    async def aclose(self) -> None:
        return None


class _Response:
    status_code = 200
    headers = {
        "content-type": "application/json",
        "x-litellm-response-cost": "0.000123",
    }

    async def aread(self) -> bytes:
        return json.dumps(
            {
                "model": "openrouter/basic",
                "choices": [],
                "usage": {
                    "cost": "0.000123",
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                },
            }
        ).encode()

    async def aclose(self) -> None:
        return None


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


async def _body(response) -> bytes:
    if hasattr(response, "body"):
        return response.body
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


class ProviderResolvedModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "provider_spend.db"
        self.runtime = LiteLlmRuntimeConfig(
            enabled=True,
            base_url="http://litellm.test",
            api_key="sk-litellm",
            service_id="litellm",
        )
        self.profile = AppProfile(
            id="paid-app",
            remote=RemoteProviderPolicy(
                providers=frozenset({"openrouter"}),
                max_usd_per_request=Decimal("1.00"),
                budget_usd_per_day=Decimal("5.00"),
                budget_usd_per_month=Decimal("20.00"),
            ),
        )
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

    async def test_alias_upstream_online_model_is_rejected_before_provider_call(self) -> None:
        provider = ProviderRef(
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
                "credential_origin": "https://openrouter.ai",
                "models": [
                    {
                        "id": "basic",
                        "upstream_model": "openai/gpt-4.1-mini:online",
                    }
                ],
            },
        )
        body = {
            "model": "openrouter/basic",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        }
        send = AsyncMock()

        with patch.object(provider_execution, "send_llm_execution_request", send):
            response = await provider_execution.execute_paid_litellm(
                _request(body),
                request_path="/v1/chat/completions",
                body=body,
                provider=provider,
                profile=self.profile,
                runtime=self.runtime,
                ledger_path=self.ledger,
                requested_model="openrouter/basic",
            )

        self.assertEqual(response.status_code, 400)
        payload = json.loads((await _body(response)).decode())
        self.assertEqual(payload["error"]["code"], "paid_option_unsupported")
        self.assertEqual(payload["error"]["param"], "model_modifier:online")
        send.assert_not_called()

    async def test_alias_estimator_uses_resolved_upstream_model_without_rewriting_dispatch(self) -> None:
        provider = ProviderRef(
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
                "credential_origin": "https://openrouter.ai",
                "models": [
                    {
                        "id": "basic",
                        "upstream_model": "openai/gpt-4.1-mini",
                    }
                ],
            },
        )
        body = {
            "model": "openrouter/basic",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
        }
        estimated_payloads: list[dict] = []
        dispatched_payloads: list[dict] = []
        real_estimate = provider_execution.providers.estimate_request_cost

        def capture_estimate(provider, payload, *, protocol=None, intent=None):
            estimated_payloads.append(dict(payload))
            return real_estimate(provider, payload, protocol=protocol, intent=intent)

        async def capture_send(execution, *, disconnect_watcher=None):
            dispatched_payloads.append(json.loads(execution.body or b"{}"))
            return _Client(), _Response()

        with (
            patch.object(provider_execution.providers, "estimate_request_cost", capture_estimate),
            patch.object(provider_execution, "send_llm_execution_request", capture_send),
        ):
            response = await provider_execution.execute_paid_litellm(
                _request(body),
                request_path="/v1/chat/completions",
                body=body,
                provider=provider,
                profile=self.profile,
                runtime=self.runtime,
                ledger_path=self.ledger,
                requested_model="openrouter/basic",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(estimated_payloads[0]["model"], "openai/gpt-4.1-mini")
        self.assertEqual(dispatched_payloads[0]["model"], "openrouter/basic")
