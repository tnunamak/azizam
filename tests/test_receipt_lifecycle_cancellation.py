"""ASGI handler cancellation regressions for accepted outer receipts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

import proxy


def _request(path: str, payload: dict) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        await asyncio.Future()

    return Request({"type": "http", "method": "POST", "path": path, "headers": []}, receive)


class ReceiptLifecycleCancellationTests(IsolatedAsyncioTestCase):
    async def test_responses_nonstream_asgi_cancellation_finalizes_same_receipt(self) -> None:
        began, finalized = [], []
        entered = asyncio.Event()

        async def delayed_forward(*_args, **_kwargs):
            entered.set()
            await asyncio.Future()

        route_auth = SimpleNamespace(auth_context=proxy.NO_AUTH_CONTEXT, delegated_to_litellm=True)
        with patch.object(proxy, "authenticate_runtime_or_litellm_request", return_value=route_auth), patch.object(
            proxy, "_forward_chat_completions", delayed_forward
        ), patch.object(proxy.receipt_store, "begin", side_effect=lambda r: began.append(r)), patch.object(
            proxy.receipt_store, "finalize", side_effect=lambda r: finalized.append(r) or True
        ):
            task = asyncio.create_task(proxy.create_response(_request("/v1/responses", {"model": "m", "input": "hi"})))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(len(began), 1)
        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "cancelled")

    async def test_provider_preheader_asgi_cancellation_finalizes_same_receipt(self) -> None:
        began, finalized = [], []
        entered = asyncio.Event()

        class DelayedContext:
            async def __aenter__(self):
                entered.set()
                await asyncio.Future()

        class DelayedClient:
            def stream(self, *_args, **_kwargs):
                return DelayedContext()

            async def aclose(self):
                return None

        target = {"backend": "test", "service": "test", "host": "http://test", "enabled": True}
        with patch.object(proxy, "_PROVIDER_NATIVE_TARGETS", {"test": target}), patch.object(
            proxy, "_auth_context_from_scope", return_value=proxy.NO_AUTH_CONTEXT
        ), patch.object(proxy, "evaluate_and_apply", return_value=SimpleNamespace(denied=False)), patch.object(
            proxy.httpx, "AsyncClient", return_value=DelayedClient()
        ), patch.object(proxy.receipt_store, "begin", side_effect=lambda r: began.append(r)), patch.object(
            proxy.receipt_store, "finalize", side_effect=lambda r: finalized.append(r) or True
        ):
            task = asyncio.create_task(proxy.provider_native_request(
                _request("/gateway/v1/providers/test/v1/chat/completions", {"x": 1}), "test", "v1/chat/completions"
            ))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(len(began), 1)
        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "cancelled")

    async def test_current_begins_before_active_backend_resolution_and_finishes_no_active(self) -> None:
        began, finalized = [], []

        async def no_active(*_args, **_kwargs):
            self.assertEqual(len(began), 1, "active backend resolution must follow receipt begin")
            return None, proxy.openai_error_response("no active", status_code=503)

        logical = SimpleNamespace(logical_service_id="current", base_model_id="current")
        with patch.object(proxy, "_resolve_request_target", return_value=logical), patch.object(
            proxy, "_model_policy_schema_error", return_value=None
        ), patch.object(proxy, "_current_loaded_llm_target", no_active), patch.object(
            proxy.receipt_store, "begin", side_effect=lambda r: began.append(r)
        ), patch.object(proxy.receipt_store, "finalize", side_effect=lambda r: finalized.append(r) or True):
            response = await proxy._forward_current_loaded_openai_request(
                _request("/v1/chat/completions", {"model": "current", "messages": []}),
                request_path="/v1/chat/completions",
                auth_context=proxy.NO_AUTH_CONTEXT,
                body_json={"model": "current", "messages": []},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "failed")

    async def test_direct_native_begins_before_health_effect_and_finishes_failure(self) -> None:
        began, finalized = [], []

        async def unhealthy(_backend):
            self.assertEqual(len(began), 1, "health probe must follow receipt begin")
            return False

        with patch.object(proxy, "authenticate_request", return_value=proxy.NO_AUTH_CONTEXT), patch.object(
            proxy, "_path_token_from_request", return_value=None
        ), patch.object(proxy, "LITELLM_BODY_EXECUTION_PATHS", frozenset()), patch.object(
            proxy, "MODEL_REQUIRED_PATHS", frozenset({"/v1/chat/completions"})
        ), patch.object(proxy, "backend_for_host", return_value="test"), patch.object(
            proxy, "_service_availability_response", return_value=None
        ), patch.object(proxy, "check_service_health", unhealthy), patch.object(
            proxy.receipt_store, "begin", side_effect=lambda r: began.append(r)
        ), patch.object(proxy.receipt_store, "finalize", side_effect=lambda r: finalized.append(r) or True):
            response = await proxy.proxy_request(
                _request("/v1/chat/completions", {"model": "native", "messages": []}),
                "v1/chat/completions", mapped_route=True,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "failed")
