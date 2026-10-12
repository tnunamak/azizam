"""ASGI handler cancellation regressions for accepted outer receipts."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

import proxy
from tests.support.access import plan_for, access, access_context, auth_identity  # noqa: E402
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
    async def test_provider_native_rejects_paid_remote_record_before_dispatch(self) -> None:
        class Upstream:
            status_code = 200
            headers = {"content-type": "application/json"}

            async def aiter_bytes(self):
                yield b'{"ok": true}'

        class Context:
            async def __aenter__(self):
                client.opened = True
                return Upstream()

            async def __aexit__(self, *_args):
                return False

        class Client:
            opened = False

            def stream(self, *_args, **_kwargs):
                return Context()

            async def aclose(self):
                return None

        native_profile = plan_for("native-only", paid=sorted(set()))
        auth_context = proxy.AuthContext(
            identity=auth_identity(
                name="native-paid-test",
                key_hash="sha256:native-paid-test",
                access=access(native_profile),
                app_id="app_native_paid_test",
            )
        )
        backend_cfg = {
            "provider_type": "paid-remote",
            "remote_provider": True,
            "native_passthrough": True,
            "api_key_env": "OPENROUTER_API_KEY",
            "auth_inject": [
                {"header": "authorization", "secret_env": "OPENROUTER_API_KEY"}
            ],
        }
        target = {
            "backend": "openrouter",
            "service": "openrouter",
            "host": "https://openrouter.test",
            "enabled": True,
            "timeout": 1,
        }
        client = Client()
        began, finalized = [], []

        with patch.object(proxy, "_PROVIDER_NATIVE_TARGETS", {"openrouter": target}), patch.object(
            proxy, "_auth_context_from_scope", return_value=auth_context
        ), patch.dict(proxy.config, {"backends": {"openrouter": backend_cfg}}, clear=False), patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-provider-key"}, clear=False
        ), patch.object(proxy.httpx, "AsyncClient", return_value=client), patch.object(
            proxy.receipt_store, "begin", side_effect=began.append
        ), patch.object(
            proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True
        ):
            response = await proxy.provider_native_request(
                _request(
                    "/gateway/v1/providers/openrouter/v1/chat/completions",
                    {"model": "openrouter/paid-chat", "messages": []},
                ),
                "openrouter",
                "v1/chat/completions",
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(client.opened)

    async def test_provider_native_rejects_local_alias_to_paid_downstream_service(self) -> None:
        class Client:
            opened = False

            def stream(self, *_args, **_kwargs):
                self.opened = True
                raise AssertionError("native guard must run before upstream dispatch")

            async def aclose(self):
                return None

        native_profile = plan_for("native-only", paid=sorted(set()))
        auth_context = proxy.AuthContext(
            identity=auth_identity(
                name="native-paid-test",
                key_hash="sha256:native-paid-test",
                access=access(native_profile),
                app_id="app_native_paid_test",
            )
        )
        local_alias_cfg = {
            "native_passthrough": True,
            "downstream_service": "openrouter",
            "host": "127.0.0.1",
            "port": 9999,
        }
        paid_downstream_cfg = {
            "provider_type": "paid-remote",
            "remote_provider": True,
            "api_key_env": "OPENROUTER_API_KEY",
            "auth_inject": [
                {"header": "authorization", "secret_env": "OPENROUTER_API_KEY"}
            ],
        }
        target = {
            "backend": "local-alias",
            "service": "openrouter",
            "host": "http://local-alias.test",
            "enabled": True,
            "timeout": 1,
        }
        client = Client()

        with patch.object(proxy, "_PROVIDER_NATIVE_TARGETS", {"local-alias": target}), patch.object(
            proxy, "_auth_context_from_scope", return_value=auth_context
        ), patch.dict(
            proxy.config,
            {"backends": {"local-alias": local_alias_cfg, "openrouter": paid_downstream_cfg}},
            clear=False,
        ), patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-provider-key"}, clear=False
        ), patch.object(proxy.httpx, "AsyncClient", return_value=client):
            response = await proxy.provider_native_request(
                _request(
                    "/gateway/v1/providers/local-alias/v1/chat/completions",
                    {"model": "openrouter/paid-chat", "messages": []},
                ),
                "local-alias",
                "v1/chat/completions",
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(client.opened)

    async def test_responses_post_begin_await_failures_are_owned(self) -> None:
        """Both outer Responses dispatch shapes finalize through the owner."""
        for stream in (False, True):
            for error in (asyncio.CancelledError(), KeyboardInterrupt("stop")):
                with self.subTest(stream=stream, error=type(error).__name__):
                    began, finalized = [], []

                    async def failing_forward(*_args, **_kwargs):
                        raise error

                    route_auth = SimpleNamespace(auth_context=access_context(), delegated_to_litellm=True)
                    with patch.object(proxy, "authenticate_runtime_request", return_value=route_auth), patch.object(
                        proxy, "_forward_chat_completions", failing_forward
                    ), patch.object(proxy.receipt_store, "begin", side_effect=began.append), patch.object(
                        proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True
                    ):
                        with self.assertRaises(type(error)):
                            await proxy.create_response(_request(
                                "/v1/responses", {"model": "m", "input": "hi", "stream": stream}
                            ))

                    self.assertEqual(finalized, began)
                    self.assertEqual(
                        finalized[0].status,
                        "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                    )

    async def test_responses_stream_iteration_failures_are_owned(self) -> None:
        for error in (asyncio.CancelledError(), KeyboardInterrupt("stop")):
            with self.subTest(error=type(error).__name__):
                began, finalized = [], []

                class Upstream:
                    status_code = 200

                    async def aclose(self):
                        return None

                class Client:
                    async def aclose(self):
                        return None

                async def forward(*_args, **_kwargs):
                    return Client(), Upstream()

                async def failing_iter(*_args, **_kwargs):
                    raise error
                    yield b"unreachable"

                route_auth = SimpleNamespace(auth_context=access_context(), delegated_to_litellm=True)
                with patch.object(proxy, "authenticate_runtime_request", return_value=route_auth), patch.object(
                    proxy, "_forward_chat_completions", forward
                ), patch.object(proxy, "iter_llm_response_bytes", failing_iter), patch.object(
                    proxy.receipt_store, "begin", side_effect=began.append
                ), patch.object(proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True):
                    response = await proxy.create_response(_request(
                        "/v1/responses", {"model": "m", "input": "hi", "stream": True}
                    ))
                    with self.assertRaises(type(error)):
                        async for _chunk in response.body_iterator:
                            pass

                self.assertEqual(finalized, began)
                self.assertEqual(finalized[0].status, "cancelled" if isinstance(error, asyncio.CancelledError) else "failed")

    async def test_responses_nonstream_asgi_cancellation_finalizes_same_receipt(self) -> None:
        began, finalized = [], []
        entered = asyncio.Event()

        async def delayed_forward(*_args, **_kwargs):
            entered.set()
            await asyncio.Future()

        route_auth = SimpleNamespace(auth_context=access_context(), delegated_to_litellm=True)
        with patch.object(proxy, "authenticate_runtime_request", return_value=route_auth), patch.object(
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
            proxy, "_auth_context_from_scope", return_value=access_context()
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

    async def test_provider_post_begin_preheader_base_exception_is_owned(self) -> None:
        began, finalized = [], []

        class FailingContext:
            async def __aenter__(self):
                raise KeyboardInterrupt("stop")

        class Client:
            def stream(self, *_args, **_kwargs):
                return FailingContext()

            async def aclose(self):
                return None

        target = {"backend": "test", "service": "test", "host": "http://test", "enabled": True}
        with patch.object(proxy, "_PROVIDER_NATIVE_TARGETS", {"test": target}), patch.object(
            proxy, "_auth_context_from_scope", return_value=access_context()
        ), patch.object(proxy, "evaluate_and_apply", return_value=SimpleNamespace(denied=False)), patch.object(
            proxy.httpx, "AsyncClient", return_value=Client()
        ), patch.object(proxy.receipt_store, "begin", side_effect=began.append), patch.object(
            proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True
        ):
            with self.assertRaises(KeyboardInterrupt):
                await proxy.provider_native_request(
                    _request("/gateway/v1/providers/test/v1/chat/completions", {"x": 1}),
                    "test", "v1/chat/completions",
                )

        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "failed")

    async def test_provider_stream_iteration_failures_are_owned(self) -> None:
        for error in (asyncio.CancelledError(), KeyboardInterrupt("stop")):
            with self.subTest(error=type(error).__name__):
                began, finalized = [], []

                class Upstream:
                    status_code = 200
                    headers = {"content-type": "application/octet-stream"}

                    async def aiter_bytes(self):
                        raise error
                        yield b"unreachable"

                class Context:
                    exited = False

                    async def __aenter__(self):
                        return Upstream()

                    async def __aexit__(self, *_args):
                        self.exited = True
                        return False

                class Client:
                    closed = False

                    def stream(self, *_args, **_kwargs):
                        self.context = Context()
                        return self.context

                    async def aclose(self):
                        self.closed = True
                        return None

                target = {"backend": "test", "service": "test", "host": "http://test", "enabled": True}
                client = Client()
                with patch.object(proxy, "_PROVIDER_NATIVE_TARGETS", {"test": target}), patch.object(
                    proxy, "_auth_context_from_scope", return_value=access_context()
                ), patch.object(proxy, "evaluate_and_apply", return_value=SimpleNamespace(denied=False)), patch.object(
                    proxy.httpx, "AsyncClient", return_value=client
                ), patch.object(proxy.receipt_store, "begin", side_effect=began.append), patch.object(
                    proxy.receipt_store, "finalize", side_effect=lambda receipt: finalized.append(receipt) or True
                ):
                    response = await proxy.provider_native_request(
                        _request("/gateway/v1/providers/test/v1/chat/completions", {"x": 1}),
                        "test", "v1/chat/completions",
                    )
                    with self.assertRaises(type(error)):
                        async for _chunk in response.body_iterator:
                            pass

                self.assertEqual(finalized, began)
                self.assertEqual(finalized[0].status, "cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
                self.assertTrue(client.context.exited)
                self.assertTrue(client.closed)

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
                auth_context=access_context(),
                body_json={"model": "current", "messages": []},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "failed")

    async def test_current_dispatch_cancellation_finalizes_cancelled_and_reraises(self) -> None:
        began, finalized = [], []
        entered = asyncio.Event()

        async def delayed_dispatch(_span, _request):
            entered.set()
            await asyncio.Future()

        logical = SimpleNamespace(logical_service_id="current", base_model_id="current")
        target = proxy.CurrentLoadedLlmTarget("local", "http://upstream.test", "loaded.gguf")
        with patch.object(proxy, "_resolve_request_target", return_value=logical), patch.object(
            proxy, "_model_policy_schema_error", return_value=None
        ), patch.object(proxy, "_current_loaded_llm_target", AsyncMock(return_value=(target, None))), patch.object(
            proxy, "_prepare_resolved_openai_request", return_value=(None, {"model": "current", "messages": []}, None)
        ), patch.object(proxy, "prepare_downstream_headers", return_value={}), patch.object(
            proxy._OuterCancellationSpan, "dispatch", delayed_dispatch
        ), patch.object(proxy.receipt_store, "begin", side_effect=lambda r: began.append(r)), patch.object(
            proxy.receipt_store, "finalize", side_effect=lambda r: finalized.append(r) or True
        ):
            task = asyncio.create_task(proxy._forward_current_loaded_openai_request(
                _request("/v1/chat/completions", {"model": "current", "messages": []}),
                request_path="/v1/chat/completions", auth_context=access_context(),
                body_json={"model": "current", "messages": []},
            ))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(finalized, began)
        self.assertEqual(finalized[0].status, "cancelled")
        self.assertEqual(finalized[0].metadata["terminal_reason"], "cancelled")
