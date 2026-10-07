from __future__ import annotations

import ast
import asyncio
import json
import unittest
from pathlib import Path

from starlette.responses import StreamingResponse

from gateway.backends.cancellation import CancellationRegistry
from gateway.backends.llm_execution import (
    LITELLM_BODY_EXECUTION_PATHS,
    ClientDisconnectedBeforeHeaders,
    RequestDisconnectWatcher,
    close_llm_response,
    iter_llm_response_bytes,
    litellm_headers,
    litellm_runtime_from_registry,
    read_llm_response_body,
    send_llm_execution_request,
)


class _ClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _BlockingResponse:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    async def aread(self) -> bytes:
        await self.release.wait()
        return b"late"

    async def aclose(self) -> None:
        self.closed = True


class _ChunkResponse:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        await self.release.wait()
        yield b"late"

    async def aclose(self) -> None:
        self.closed = True


class _FiniteChunkResponse:
    def __init__(self) -> None:
        self.closed = False

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


class _CompletedButUnfinalizedResponse:
    def __init__(self) -> None:
        self.upstream_eof = asyncio.Event()
        self.closed = asyncio.Event()

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        yield b"data: [DONE]\n\n"
        self.upstream_eof.set()

    async def aclose(self) -> None:
        self.closed.set()


class _DoneThenBlocksResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.post_done_read_started = asyncio.Event()
        self.closed = asyncio.Event()
        self._never = asyncio.Event()

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk
        self.post_done_read_started.set()
        await self._never.wait()

    async def aclose(self) -> None:
        self.closed.set()


class _FalsePositiveResponse:
    def __init__(self) -> None:
        self.closed = False

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        yield b'data: {"choices":[{"delta":{"content":"[DONE]"}}]}\n\n'
        yield b"data: [DONE] still text\n\n"
        yield b"data: partial [DONE]"

    async def aclose(self) -> None:
        self.closed = True


class _PrefetchedErrorResponse:
    def __init__(self) -> None:
        self.read_failed = asyncio.Event()
        self.closed = asyncio.Event()

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        yield b"data: partial\n\n"
        self.read_failed.set()
        raise RuntimeError("upstream read failed")

    async def aclose(self) -> None:
        self.closed.set()


class _CloseFailingPrefetchedErrorResponse:
    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        yield b"data: partial\n\n"
        raise RuntimeError("upstream read failed")

    async def aclose(self) -> None:
        raise RuntimeError("upstream close failed")


class _ImmediateReadResponse:
    def __init__(self, payload: bytes = b'{"ok":true}') -> None:
        self._payload = payload
        self.closed = False

    async def aread(self) -> bytes:
        return self._payload

    async def aclose(self) -> None:
        self.closed = True


class _LiveReadResponse:
    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.closed = asyncio.Event()
        self._never = asyncio.Event()

    def aiter_bytes(self):
        return self._iter()

    async def _iter(self):
        self.read_started.set()
        try:
            await self._never.wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed.set()


def test_litellm_body_execution_paths_are_explicit_and_limited() -> None:
    assert "/v1/chat/completions" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/completions" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/embeddings" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/images/generations" not in LITELLM_BODY_EXECUTION_PATHS


def test_litellm_headers_do_not_forward_client_runtime_key() -> None:
    headers = litellm_headers(
        {
            "Authorization": "Bearer opk_client_key",
            "Content-Type": "application/json",
            "X-Api-Key": "opk_client_key",
        },
        api_key="sk-litellm-internal",
        gateway_metadata={
            "execution": "litellm",
            "profile": "family",
            "key-prefix": "sha256:abcdef",
        },
    )

    assert headers["X-Litellm-Key"] == "Bearer sk-litellm-internal"
    assert headers["content-type"] == "application/json"
    assert headers["x-azizam-execution"] == "litellm"
    assert "x-ai-gateway-execution" not in headers
    assert headers["x-azizam-profile"] == "family"
    assert headers["x-azizam-key-prefix"] == "sha256:abcdef"
    assert "authorization" not in headers
    assert "x-api-key" not in headers


def test_litellm_runtime_prefers_gateway_service_registry() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "name": "LiteLLM",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "priority": 5,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url": "http://127.0.0.1:4011/",
                        "api_key_env": "INTERNAL_LITELLM_KEY",
                    },
                }
            }
        },
        env={"INTERNAL_LITELLM_KEY": "sk-internal"},
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4011"
    assert runtime.api_key == "sk-internal"
    assert runtime.service_id == "litellm"


def test_litellm_runtime_uses_registry_default_when_env_override_is_absent() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url_env": "AZIZAM_LITELLM_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011/",
                        "api_key_env": "AZIZAM_LITELLM_API_KEY",
                    },
                }
            }
        },
        env={},
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4011"
    assert runtime.api_key == ""


def test_litellm_runtime_env_override_wins_over_registry_default() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url_env": "AZIZAM_LITELLM_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011",
                    },
                }
            }
        },
        env={"AZIZAM_LITELLM_BASE_URL": "http://127.0.0.1:4999/"},
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4999"


def test_litellm_runtime_ignores_unmarked_openai_providers() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "openai": {
                    "id": "openai",
                    "kind": "provider",
                    "enabled": True,
                    "capabilities": ["openai"],
                    "config": {"base_url": "https://api.openai.com/v1"},
                }
            }
        },
        env={},
    )

    assert not runtime.enabled


class LlmExecutionLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_done_event_ends_tool_call_turn_without_upstream_eof(self) -> None:
        client = _ClosableClient()
        upstream = _DoneThenBlocksResponse([
            b'data: {"choices":[{"delta":{"tool_calls":[{}]},"finish_reason":"tool_calls"}]}\n\n',
            b"data: [DONE]\n\n",
        ])

        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client,
                upstream,
            )
        ]

        self.assertEqual(chunks, [
            b'data: {"choices":[{"delta":{"tool_calls":[{}]},"finish_reason":"tool_calls"}]}\n\n',
            b"data: [DONE]\n\n",
        ])
        self.assertFalse(upstream.post_done_read_started.is_set())
        self.assertTrue(upstream.closed.is_set())
        self.assertTrue(client.closed)

    async def test_stream_done_event_handles_fragmentation_and_drops_trailing_bytes(self) -> None:
        client = _ClosableClient()
        upstream = _DoneThenBlocksResponse([
            b"data: [DO",
            b"NE]\r\n\r\ndata: must-not-reach-client\n\n",
        ])

        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client,
                upstream,
            )
        ]

        self.assertEqual(chunks, [b"data: [DO", b"NE]\r\n\r\n"])
        self.assertFalse(upstream.post_done_read_started.is_set())
        self.assertTrue(upstream.closed.is_set())
        self.assertTrue(client.closed)

    async def test_stream_does_not_treat_done_text_or_partial_line_as_terminal(self) -> None:
        client = _ClosableClient()
        upstream = _FalsePositiveResponse()

        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client,
                upstream,
            )
        ]

        self.assertEqual(chunks, [
            b'data: {"choices":[{"delta":{"content":"[DONE]"}}]}\n\n',
            b"data: [DONE] still text\n\n",
            b"data: partial [DONE]",
        ])
        self.assertTrue(upstream.closed)
        self.assertTrue(client.closed)

    async def test_stream_preserves_upstream_read_error_when_close_also_fails(self) -> None:
        client = _ClosableClient()
        chunks = iter_llm_response_bytes(
            client,
            _CloseFailingPrefetchedErrorResponse(),
        )

        self.assertEqual(await chunks.__anext__(), b"data: partial\n\n")
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "upstream read failed"):
            await chunks.__anext__()
        self.assertTrue(client.closed)

    async def test_stream_closes_upstream_when_prefetched_read_raises_while_asgi_send_is_stalled(self) -> None:
        client = _ClosableClient()
        upstream = _PrefetchedErrorResponse()
        body_send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def receive():
            self.fail("Starlette 2.4 streaming must not read ASGI receive")

        async def send(message):
            if message["type"] == "http.response.body" and message["more_body"]:
                body_send_started.set()
                await release_send.wait()

        response = StreamingResponse(
            iter_llm_response_bytes(
                client,
                upstream,
            )
        )
        response_task = asyncio.create_task(
            response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
        try:
            await asyncio.wait_for(body_send_started.wait(), timeout=0.25)
            await asyncio.wait_for(upstream.read_failed.wait(), timeout=0.25)
            await asyncio.wait_for(upstream.closed.wait(), timeout=0.25)
            self.assertTrue(client.closed)
            self.assertFalse(response_task.done())
        finally:
            release_send.set()
            with self.assertRaisesRegex(RuntimeError, "upstream read failed"):
                await asyncio.wait_for(response_task, timeout=0.25)

    async def test_stream_closes_done_upstream_while_asgi_send_is_stalled(self) -> None:
        client = _ClosableClient()
        upstream = _CompletedButUnfinalizedResponse()
        body_send_started = asyncio.Event()
        release_send = asyncio.Event()
        sent_messages = []

        async def receive():
            self.fail("Starlette 2.4 streaming must not read ASGI receive")

        async def send(message):
            sent_messages.append(message)
            if message["type"] == "http.response.body" and message["more_body"]:
                body_send_started.set()
                await release_send.wait()

        response = StreamingResponse(
            iter_llm_response_bytes(
                client,
                upstream,
            )
        )
        response_task = asyncio.create_task(
            response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        )
        try:
            await asyncio.wait_for(body_send_started.wait(), timeout=0.25)
            await asyncio.wait_for(upstream.closed.wait(), timeout=0.25)
            self.assertTrue(client.closed)
            self.assertFalse(response_task.done())
            self.assertFalse(upstream.upstream_eof.is_set())
            self.assertEqual(len(sent_messages), 2)
        finally:
            release_send.set()
            await asyncio.wait_for(response_task, timeout=0.25)

    async def test_asgi_disconnect_cancels_live_upstream_read_without_second_receive(self) -> None:
        client = _ClosableClient()
        upstream = _LiveReadResponse()
        receive_calls = 0

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            await upstream.read_started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            return None

        response = StreamingResponse(
            iter_llm_response_bytes(
                client,
                upstream,
            )
        )
        await asyncio.wait_for(
            response(
                {"type": "http", "asgi": {"spec_version": "2.3"}},
                receive,
                send,
            ),
            timeout=0.25,
        )

        self.assertEqual(receive_calls, 1)
        self.assertTrue(upstream.read_cancelled.is_set())
        self.assertTrue(upstream.closed.is_set())
        self.assertTrue(client.closed)

    async def test_stream_forwards_all_bytes_and_closes_at_upstream_eof(self) -> None:
        client = _ClosableClient()
        response = _FiniteChunkResponse()

        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client,
                response,
            )
        ]

        self.assertEqual(chunks, [b"data: first\n\n", b"data: [DONE]\n\n"])
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_body_read_closes_upstream_when_downstream_disconnects(self) -> None:
        client = _ClosableClient()
        response = _BlockingResponse()
        disconnect_event = asyncio.Event()

        async def receive():
            await disconnect_event.wait()
            return {"type": "http.disconnect"}

        watcher = RequestDisconnectWatcher(receive)
        read_task = asyncio.create_task(
            read_llm_response_body(
                client,
                response,
                disconnect_watcher=watcher,
            )
        )
        await asyncio.sleep(0.01)
        disconnect_event.set()

        self.assertIsNone(await asyncio.wait_for(read_task, timeout=1))
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        await watcher.aclose()

    async def test_stream_iteration_closes_upstream_when_handler_is_cancelled(self) -> None:
        client = _ClosableClient()
        response = _ChunkResponse()

        chunks = iter_llm_response_bytes(client, response)
        next_chunk = asyncio.create_task(chunks.__anext__())
        await asyncio.sleep(0.01)
        next_chunk.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await next_chunk
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_body_read_closes_upstream_when_handler_is_cancelled(self) -> None:
        client = _ClosableClient()
        response = _BlockingResponse()

        read_task = asyncio.create_task(
            read_llm_response_body(
                client,
                response,
            )
        )
        await asyncio.sleep(0.01)
        read_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await read_task
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_body_read_closes_upstream_when_cancellation_token_signals(self) -> None:
        """A cross-hop cancel (see gateway/backends/cancellation.py) must stop
        a non-stream backend read even though the local ASGI disconnect watcher
        never fires — the two signals are independent."""
        client = _ClosableClient()
        response = _BlockingResponse()
        registry = CancellationRegistry()
        handle = registry.register("req-nonstream")

        async def never_disconnects():
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        watcher = RequestDisconnectWatcher(never_disconnects)
        read_task = asyncio.create_task(
            read_llm_response_body(
                client,
                response,
                disconnect_watcher=watcher,
                cancellation_token=handle.token,
            )
        )
        await asyncio.sleep(0.01)
        registry.signal("req-nonstream")

        self.assertIsNone(await asyncio.wait_for(read_task, timeout=1))
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        await watcher.aclose()

    async def test_body_read_ignores_cancellation_token_for_a_different_request(self) -> None:
        client = _ClosableClient()
        response = _ImmediateReadResponse()
        registry = CancellationRegistry()
        handle = registry.register("req-a")
        registry.signal("req-b")

        content = await read_llm_response_body(
            client,
            response,
            cancellation_token=handle.token,
        )

        self.assertEqual(content, b'{"ok":true}')

    async def test_stream_closes_upstream_when_cancellation_token_signals(self) -> None:
        """A cross-hop cancel must stop a streaming backend read that has no
        local downstream client to disconnect from — e.g. the internal
        gateway hop running behind LiteLLM's own httpx client."""
        client = _ClosableClient()
        response = _ChunkResponse()
        registry = CancellationRegistry()
        handle = registry.register("req-stream")

        chunks = iter_llm_response_bytes(
            client,
            response,
            cancellation_token=handle.token,
        )
        next_chunk = asyncio.create_task(chunks.__anext__())
        await asyncio.sleep(0.01)
        registry.signal("req-stream")

        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(next_chunk, timeout=1)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_stream_ignores_cancellation_token_for_a_different_request(self) -> None:
        client = _ClosableClient()
        response = _FiniteChunkResponse()
        registry = CancellationRegistry()
        handle = registry.register("req-a")
        registry.signal("req-b")

        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client,
                response,
                cancellation_token=handle.token,
            )
        ]

        self.assertEqual(chunks, [b"data: first\n\n", b"data: [DONE]\n\n"])


class RequestDisconnectWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_resolves_on_http_disconnect(self) -> None:
        receive_calls = 0
        disconnect_now = asyncio.Event()

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            await disconnect_now.wait()
            return {"type": "http.disconnect"}

        watcher = RequestDisconnectWatcher(receive)
        self.assertFalse(watcher.disconnected)
        disconnect_now.set()
        await asyncio.wait_for(watcher.wait(), timeout=1)
        self.assertTrue(watcher.disconnected)
        self.assertEqual(receive_calls, 1)
        await watcher.aclose()

    async def test_issues_exactly_one_receive_call_across_a_long_wait(self) -> None:
        """Regression: the rejected implementation wrapped every poll tick in
        asyncio.wait_for(receive(), timeout=...), cancelling and restarting
        receive() every tick (proven: 5 receive starts / 5 cancels in 40ms).
        The persistent watcher must call receive() exactly once and keep
        awaiting that same call across an arbitrarily long idle period."""
        receive_calls = 0
        never = asyncio.Event()

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            await never.wait()
            return {"type": "http.disconnect"}

        watcher = RequestDisconnectWatcher(receive)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(receive_calls, 1)
        self.assertFalse(watcher.disconnected)
        await watcher.aclose()
        self.assertEqual(receive_calls, 1)

    async def test_aclose_is_idempotent_and_safe_without_wait(self) -> None:
        async def receive():
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        watcher = RequestDisconnectWatcher(receive)
        await watcher.aclose()
        await watcher.aclose()  # must not raise

    async def test_non_disconnect_message_does_not_signal_and_receive_continues(self) -> None:
        messages = [{"type": "http.request", "more_body": False}, {"type": "http.disconnect"}]

        async def receive():
            return messages.pop(0)

        watcher = RequestDisconnectWatcher(receive)
        await asyncio.wait_for(watcher.wait(), timeout=1)
        self.assertTrue(watcher.disconnected)
        await watcher.aclose()


class LiteLlmRoutingOverrideTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_router_overrides_are_stripped_for_all_supported_json_encodings(self) -> None:
        from gateway.backends.llm_execution import LlmExecutionRequest, sanitize_litellm_execution_request

        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                request = LlmExecutionRequest(
                    method="POST", url="http://litellm.test/v1/chat/completions",
                    headers={"content-type": "application/json"}, read_timeout=5.0,
                    body=json.dumps({"model": "local-chat", "fallbacks": ["openrouter/paid-chat"]}).encode(encoding),
                )
                outbound = sanitize_litellm_execution_request(request)
                self.assertNotIn("fallbacks", json.loads(outbound.body))


    def test_sanitizer_strips_router_settings_override_and_unknown_litellm_envelopes(self) -> None:
        from gateway.backends.llm_execution import LlmExecutionRequest, sanitize_litellm_execution_request

        messages = [{"role": "user", "content": "preserve nested user content"}]
        tools = [{
            "type": "function",
            "function": {
                "name": "preserve_schema",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "router_settings_override": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                },
            },
        }]
        request = LlmExecutionRequest(
            method="POST",
            url="http://litellm.test/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=json.dumps({
                "model": "local-chat",
                "messages": messages,
                "tools": tools,
                "router_settings_override": {
                    "fallbacks": [{"local-chat": ["openrouter/paid-chat"]}],
                    "model_group_retry_policy": {"local-chat": {"TimeoutErrorRetries": 2}},
                },
                "metadata": {"model_group": "openrouter/paid-chat"},
                "litellm_metadata": {"model_group": "openrouter/paid-chat"},
                "user_config": {"routing_strategy": "usage-based-routing"},
                "unknown_litellm_knob": "must not pass through",
            }).encode(),
            read_timeout=5.0,
        )

        sanitized = json.loads(sanitize_litellm_execution_request(request).body.decode())

        self.assertEqual(sanitized["model"], "local-chat")
        self.assertEqual(sanitized["messages"], messages)
        self.assertEqual(sanitized["tools"], tools)
        for key in (
            "router_settings_override",
            "metadata",
            "litellm_metadata",
            "user_config",
            "unknown_litellm_knob",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, sanitized)

    async def test_router_settings_override_fallback_cannot_reach_paid_model_after_sanitizer(self) -> None:
        import litellm
        from litellm import Router
        from gateway.backends.llm_execution import LlmExecutionRequest, sanitize_litellm_execution_request

        request = LlmExecutionRequest(
            method="POST",
            url="http://litellm.test/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=json.dumps({
                "model": "local-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "num_retries": 0,
                "router_settings_override": {
                    "fallbacks": [{"local-chat": ["openrouter/paid-chat"]}],
                },
            }).encode(),
            read_timeout=5.0,
            trusted_num_retries=0,
            trusted_max_retries=0,
        )
        sanitized = json.loads(sanitize_litellm_execution_request(request).body.decode())
        router_settings = sanitized.pop("router_settings_override", {})
        router = Router(
            model_list=[
                {"model_name": "local-chat", "litellm_params": {"model": "openai/local", "api_key": "sk-local"}},
                {"model_name": "openrouter/paid-chat", "litellm_params": {"model": "openai/paid", "api_key": "sk-paid"}},
            ],
            num_retries=0,
            fallbacks=[],
        )
        calls: list[str] = []

        async def fake_dispatch(**kwargs):
            model = str(kwargs.get("model") or "")
            calls.append(model)
            if model == "local-chat":
                raise litellm.InternalServerError(message="local failed", model=model, llm_provider="openai")
            return {"ok": model}

        try:
            await router.async_function_with_fallbacks(
                original_function=fake_dispatch,
                metadata={},
                **sanitized,
                **router_settings,
            )
        except litellm.InternalServerError:
            pass

        self.assertEqual(calls, ["local-chat"])

    async def test_router_settings_override_retry_policy_cannot_create_extra_dispatches_after_sanitizer(self) -> None:
        import litellm
        from litellm import Router
        from gateway.backends.llm_execution import LlmExecutionRequest, sanitize_litellm_execution_request

        request = LlmExecutionRequest(
            method="POST",
            url="http://litellm.test/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=json.dumps({
                "model": "local-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "num_retries": 0,
                "max_retries": 0,
                "router_settings_override": {
                    "model_group_retry_policy": {"local-chat": {"TimeoutErrorRetries": 2}},
                },
            }).encode(),
            read_timeout=5.0,
            trusted_num_retries=0,
            trusted_max_retries=0,
        )
        sanitized = json.loads(sanitize_litellm_execution_request(request).body.decode())
        router_settings = sanitized.pop("router_settings_override", {})
        router = Router(
            model_list=[{"model_name": "local-chat", "litellm_params": {"model": "openai/local", "api_key": "sk-local"}}],
            num_retries=0,
            fallbacks=[],
        )
        calls: list[str] = []

        async def fake_dispatch(**kwargs):
            model = str(kwargs.get("model") or "")
            calls.append(model)
            raise litellm.Timeout(message="slow", model=model, llm_provider="openai")

        with self.assertRaises(litellm.Timeout):
            await router.async_function_with_fallbacks(
                original_function=fake_dispatch,
                metadata={},
                **sanitized,
                **router_settings,
            )

        self.assertEqual(calls, ["local-chat"])

    async def test_send_request_strips_caller_litellm_router_overrides(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _Response:
            status_code = 200
            headers = {"content-type": "application/json"}

            async def aclose(self) -> None:
                pass

        class _CapturingClient:
            def __init__(self) -> None:
                self.content = b""
                self.url = ""

            def build_request(self, **kwargs):
                self.content = kwargs["content"]
                self.url = str(kwargs["url"])
                return object()

            async def send(self, req, stream=True):
                return _Response()

            async def aclose(self) -> None:
                pass

        router_overrides = {
            "allowed_fails": 1,
            "api_base": "https://attacker.invalid",
            "api_key": "sk-attacker",
            "context_window_fallback_dict": {"local-chat": "openrouter/paid-chat"},
            "custom_llm_provider": "openrouter",
            "default_fallbacks": ["openrouter/paid-chat"],
            "deployment_id": "attacker-deployment",
            "extra_body": {"router": "override"},
            "fallbacks": [{"local-chat": ["openrouter/paid-chat"]}],
            "litellm_params": {"api_base": "https://attacker.invalid"},
            "max_retries": 9,
            "metadata": {"model_group": "openrouter/paid-chat"},
            "model_list": [{"model_name": "local-chat", "litellm_params": {}}],
            "num_retries": 9,
            "retry_policy": {"TimeoutErrorRetries": 9},
            "routing_strategy": "usage-based-routing",
            "specific_deployment": "attacker-deployment",
        }
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "nested fallback text is user content"},
            ],
        }]
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "preserve_nested_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "api_key": {"type": "string"},
                        "fallbacks": {"type": "array"},
                    },
                },
            },
        }

        client = _CapturingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: client
        try:
            request = LlmExecutionRequest(
                method="POST",
                url=(
                    "http://litellm.test/v1/chat/completions?"
                    "keep=hello+world&num_retries=9&specific_deployment=bad&api_key=bad"
                ),
                headers={"content-type": "application/json"},
                body=json.dumps({
                    "model": "local-chat",
                    "messages": messages,
                    "response_format": response_format,
                    **router_overrides,
                }).encode(),
                read_timeout=5.0,
            )

            dispatch_client, response = await send_llm_execution_request(request)

            self.assertIs(dispatch_client, client)
            self.assertEqual(response.status_code, 200)
            sanitized = json.loads(client.content.decode())
            self.assertEqual(sanitized["model"], "local-chat")
            self.assertEqual(sanitized["messages"], messages)
            self.assertEqual(sanitized["response_format"], response_format)
            self.assertEqual(client.url, "http://litellm.test/v1/chat/completions")
            for key in router_overrides:
                with self.subTest(key=key):
                    self.assertNotIn(key, sanitized)
                    self.assertNotIn(f"{key}=", client.url)
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

    def test_sanitizer_strips_unknown_query_parameters(self) -> None:
        from gateway.backends.llm_execution import LlmExecutionRequest, sanitize_litellm_execution_request

        request = LlmExecutionRequest(
            method="POST",
            url="http://litellm.test/v1/chat/completions?model=openrouter/paid&keep=hello%20world&blank=",
            headers={},
            body=b"{}",
            read_timeout=5.0,
        )

        sanitized = sanitize_litellm_execution_request(request)

        self.assertEqual(sanitized.url, "http://litellm.test/v1/chat/completions")

    async def test_build_request_failure_closes_client_and_raises_predispatch_error(self) -> None:
        import httpx
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import (
            LlmExecutionPredispatchError,
            LlmExecutionRequest,
        )

        class _BuildFailingClient:
            def __init__(self) -> None:
                self.closed = False
                self.send_called = False

            def build_request(self, **kwargs):
                raise httpx.InvalidURL("bad url")

            async def send(self, req, stream=True):
                self.send_called = True
                raise AssertionError("send must not run after build_request fails")

            async def aclose(self) -> None:
                self.closed = True

        client = _BuildFailingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: client
        try:
            request = LlmExecutionRequest(
                method="POST",
                url="http://litellm.test/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )

            with self.assertRaises(LlmExecutionPredispatchError):
                await send_llm_execution_request(request)

            self.assertTrue(client.closed)
            self.assertFalse(client.send_called)
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

class PreHeaderDisconnectRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_disconnect_before_headers_cancels_dispatch_and_closes_client(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _StallingClient:
            def __init__(self) -> None:
                self.closed = False
                self.send_cancelled = asyncio.Event()

            def build_request(self, **kwargs):
                return object()

            async def send(self, req, stream=True):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.send_cancelled.set()
                    raise

            async def aclose(self) -> None:
                self.closed = True

        stalling_client = _StallingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: stalling_client
        try:
            disconnect_now = asyncio.Event()

            async def receive():
                await disconnect_now.wait()
                return {"type": "http.disconnect"}

            watcher = RequestDisconnectWatcher(receive)
            execution_request = LlmExecutionRequest(
                method="POST",
                url="http://127.0.0.1:1/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )
            dispatch_task = asyncio.create_task(
                send_llm_execution_request(execution_request, disconnect_watcher=watcher)
            )
            await asyncio.sleep(0.01)
            disconnect_now.set()

            with self.assertRaises(ClientDisconnectedBeforeHeaders):
                await asyncio.wait_for(dispatch_task, timeout=1)
            self.assertTrue(await asyncio.wait_for(stalling_client.send_cancelled.wait(), timeout=1))
            self.assertTrue(stalling_client.closed)
            await watcher.aclose()
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

    async def test_normal_dispatch_without_disconnect_returns_response(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _RespondingClient:
            def __init__(self) -> None:
                self.closed = False

            def build_request(self, **kwargs):
                return object()

            async def send(self, req, stream=True):
                return "a-response"

            async def aclose(self) -> None:
                self.closed = True

        responding_client = _RespondingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: responding_client
        try:
            async def never_disconnects():
                await asyncio.Event().wait()
                return {"type": "http.disconnect"}

            watcher = RequestDisconnectWatcher(never_disconnects)
            execution_request = LlmExecutionRequest(
                method="POST",
                url="http://127.0.0.1:1/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )
            client, resp = await send_llm_execution_request(
                execution_request, disconnect_watcher=watcher
            )
            self.assertIs(client, responding_client)
            self.assertEqual(resp, "a-response")
            self.assertFalse(responding_client.closed)
            await watcher.aclose()
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

    async def test_send_failure_closes_client_when_racing_a_watcher(self) -> None:
        """A pre-header protocol failure (connect error, TLS failure, ...)
        must close the client even when a disconnect_watcher/cancellation_token
        was supplied — the failing branch used to return early via
        ``send_task.result()`` re-raising without ever calling ``aclose()``."""
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _FailingClient:
            def __init__(self) -> None:
                self.closed = False

            def build_request(self, **kwargs):
                return object()

            async def send(self, req, stream=True):
                raise RuntimeError("protocol failure")

            async def aclose(self) -> None:
                self.closed = True

        failing_client = _FailingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: failing_client
        try:
            async def never_disconnects():
                await asyncio.Event().wait()
                return {"type": "http.disconnect"}

            watcher = RequestDisconnectWatcher(never_disconnects)
            execution_request = LlmExecutionRequest(
                method="POST",
                url="http://127.0.0.1:1/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )
            with self.assertRaises(RuntimeError):
                await send_llm_execution_request(
                    execution_request, disconnect_watcher=watcher
                )
            self.assertTrue(failing_client.closed)
            await watcher.aclose()
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

    async def test_send_failure_without_any_watcher_still_closes_client(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _FailingClient:
            def __init__(self) -> None:
                self.closed = False

            def build_request(self, **kwargs):
                return object()

            async def send(self, req, stream=True):
                raise RuntimeError("protocol failure")

            async def aclose(self) -> None:
                self.closed = True

        failing_client = _FailingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: failing_client
        try:
            execution_request = LlmExecutionRequest(
                method="POST",
                url="http://127.0.0.1:1/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )
            with self.assertRaises(RuntimeError):
                await send_llm_execution_request(execution_request)
            self.assertTrue(failing_client.closed)
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

    async def test_disconnect_in_same_tick_as_headers_still_wins(self) -> None:
        """If the watcher's disconnect and the send both become ready in the
        same asyncio.wait tick, the disconnect must still win — a response
        must never be handed to a caller whose client already vanished."""
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _InstantRespondingClient:
            def __init__(self) -> None:
                self.closed = False

            def build_request(self, **kwargs):
                return object()

            async def send(self, req, stream=True):
                return "a-response"

            async def aclose(self) -> None:
                self.closed = True

        responding_client = _InstantRespondingClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: responding_client
        try:
            already_disconnected = asyncio.Event()
            already_disconnected.set()

            async def already_disconnected_receive():
                return {"type": "http.disconnect"}

            watcher = RequestDisconnectWatcher(already_disconnected_receive)
            # Give the watcher's background task a chance to observe the
            # disconnect before dispatch starts, so both the send and the
            # watcher are complete before send_llm_execution_request's own
            # asyncio.wait is even reached.
            await asyncio.sleep(0)
            execution_request = LlmExecutionRequest(
                method="POST",
                url="http://127.0.0.1:1/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )
            with self.assertRaises(ClientDisconnectedBeforeHeaders):
                await send_llm_execution_request(
                    execution_request, disconnect_watcher=watcher
                )
            self.assertTrue(responding_client.closed)
            await watcher.aclose()
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client


class SimultaneousCompletionCancellationTests(unittest.IsolatedAsyncioTestCase):
    """A disconnect/cancel observed in the same tick a read/chunk also
    completes must still be treated as a cancellation — a request must never
    be reported "succeeded" merely because its body happened to finish
    reading in the same instant its client vanished."""

    async def test_body_read_prefers_cancellation_over_a_simultaneously_ready_read(self) -> None:
        class _AlreadyDoneResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aread(self) -> bytes:
                return b"already ready"

            async def aclose(self) -> None:
                self.closed = True

        client = _ClosableClient()
        response = _AlreadyDoneResponse()

        class _AlreadySignalledWatcher:
            disconnected = True

            async def wait(self) -> None:
                return None

        content = await read_llm_response_body(
            client, response, disconnect_watcher=_AlreadySignalledWatcher()
        )
        self.assertIsNone(content)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_stream_prefers_cancellation_over_a_simultaneously_ready_chunk(self) -> None:
        registry = CancellationRegistry()
        handle = registry.register("req-tie")
        registry.signal("req-tie")  # already cancelled before iteration starts

        client = _ClosableClient()
        response = _FiniteChunkResponse()
        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client, response, cancellation_token=handle.token
            )
        ]
        self.assertEqual(chunks, [])
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        handle.release()


class BlockedTeardownSignalOrderingTests(unittest.IsolatedAsyncioTestCase):
    """Deterministic proof that ``on_signal`` fires BEFORE teardown, not as a
    side effect of it — for all three transport helpers.

    Each probe makes the relevant close call (``client.aclose()`` for the
    pre-header path, ``response.aclose()``/``client.aclose()`` for the
    nonstream and streaming reads) block forever on an event the test never
    sets. If ``on_signal`` were only called after teardown completed (the
    rejected ordering — see
    ``/home/tnunamak/.tmp/gateway-reliability-0714/independent-gate-3.md``
    finding 1), the signal would never be observed since the blocking close
    never returns. Observing the signal fire while the close is still
    provably blocked is the only way to prove the ordering rather than infer
    it from timing.
    """

    async def test_preheader_signal_fires_while_client_close_is_still_blocked(self) -> None:
        import gateway.backends.llm_execution as llm_execution_module
        from gateway.backends.llm_execution import LlmExecutionRequest

        class _BlockingCloseClient:
            def __init__(self) -> None:
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()
                self.closed = False

            def build_request(self, **kwargs):
                return object()

            async def send(self, req, stream=True):
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                self.close_started.set()
                await self.release_close.wait()
                self.closed = True

        blocking_client = _BlockingCloseClient()
        original_async_client = llm_execution_module.httpx.AsyncClient
        llm_execution_module.httpx.AsyncClient = lambda **kwargs: blocking_client
        try:
            disconnect_now = asyncio.Event()
            signalled = asyncio.Event()

            async def receive():
                await disconnect_now.wait()
                return {"type": "http.disconnect"}

            async def on_signal():
                signalled.set()

            watcher = RequestDisconnectWatcher(receive)
            execution_request = LlmExecutionRequest(
                method="POST",
                url="http://127.0.0.1:1/v1/chat/completions",
                headers={},
                body=b"{}",
                read_timeout=5.0,
            )
            dispatch_task = asyncio.create_task(
                send_llm_execution_request(
                    execution_request, disconnect_watcher=watcher, on_signal=on_signal
                )
            )
            await asyncio.sleep(0.01)
            disconnect_now.set()

            # The signal must be observable while the client's own close is
            # still provably stuck — not merely "eventually", which could
            # also be explained by the rejected after-teardown ordering
            # racing a fast test.
            await asyncio.wait_for(blocking_client.close_started.wait(), timeout=1)
            await asyncio.wait_for(signalled.wait(), timeout=1)
            self.assertFalse(blocking_client.closed)

            blocking_client.release_close.set()
            with self.assertRaises(ClientDisconnectedBeforeHeaders):
                await asyncio.wait_for(dispatch_task, timeout=1)
            self.assertTrue(blocking_client.closed)
            await watcher.aclose()
        finally:
            llm_execution_module.httpx.AsyncClient = original_async_client

    async def test_nonstream_signal_fires_while_response_close_is_still_blocked(self) -> None:
        class _BlockingCloseResponse:
            def __init__(self) -> None:
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()
                self.closed = False

            async def aread(self) -> bytes:
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                self.close_started.set()
                await self.release_close.wait()
                self.closed = True

        client = _ClosableClient()
        response = _BlockingCloseResponse()
        disconnect_event = asyncio.Event()
        signalled = asyncio.Event()

        async def receive():
            await disconnect_event.wait()
            return {"type": "http.disconnect"}

        async def on_signal():
            signalled.set()

        watcher = RequestDisconnectWatcher(receive)
        read_task = asyncio.create_task(
            read_llm_response_body(
                client, response, disconnect_watcher=watcher, on_signal=on_signal
            )
        )
        await asyncio.sleep(0.01)
        disconnect_event.set()

        await asyncio.wait_for(response.close_started.wait(), timeout=1)
        await asyncio.wait_for(signalled.wait(), timeout=1)
        self.assertFalse(response.closed)
        self.assertFalse(client.closed)

        response.release_close.set()
        self.assertIsNone(await asyncio.wait_for(read_task, timeout=1))
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        await watcher.aclose()

    async def test_stream_signal_fires_while_upstream_close_is_still_blocked(self) -> None:
        class _BlockingCloseChunkResponse:
            def __init__(self) -> None:
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()
                self.closed = False

            def aiter_bytes(self):
                return self._iter()

            async def _iter(self):
                await asyncio.Event().wait()
                yield b"unreachable"

            async def aclose(self) -> None:
                self.close_started.set()
                await self.release_close.wait()
                self.closed = True

        client = _ClosableClient()
        response = _BlockingCloseChunkResponse()
        registry = CancellationRegistry()
        handle = registry.register("req-blocked-teardown")
        signalled = asyncio.Event()

        async def on_signal():
            signalled.set()

        chunks = iter_llm_response_bytes(
            client, response, cancellation_token=handle.token, on_signal=on_signal
        )
        next_chunk = asyncio.create_task(chunks.__anext__())
        await asyncio.sleep(0.01)
        registry.signal("req-blocked-teardown")

        await asyncio.wait_for(response.close_started.wait(), timeout=1)
        await asyncio.wait_for(signalled.wait(), timeout=1)
        self.assertFalse(response.closed)
        self.assertFalse(client.closed)

        response.release_close.set()
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(next_chunk, timeout=1)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        handle.release()

    async def test_stream_signal_fires_on_generator_cancellation_while_upstream_close_is_blocked(self) -> None:
        """The local-ASGI-disconnect path (Starlette cancelling this
        generator directly, not the cross-hop token) must also signal before
        teardown — the same ordering guarantee, reached via
        ``asyncio.CancelledError`` instead of ``cancellation_token``."""
        class _BlockingCloseChunkResponse:
            def __init__(self) -> None:
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()
                self.closed = False
                self.read_started = asyncio.Event()

            def aiter_bytes(self):
                return self._iter()

            async def _iter(self):
                self.read_started.set()
                await asyncio.Event().wait()
                yield b"unreachable"

            async def aclose(self) -> None:
                self.close_started.set()
                await self.release_close.wait()
                self.closed = True

        client = _ClosableClient()
        response = _BlockingCloseChunkResponse()
        signalled = asyncio.Event()

        async def on_signal():
            signalled.set()

        chunks = iter_llm_response_bytes(client, response, on_signal=on_signal)
        next_chunk = asyncio.create_task(chunks.__anext__())
        await asyncio.wait_for(response.read_started.wait(), timeout=1)
        next_chunk.cancel()

        await asyncio.wait_for(response.close_started.wait(), timeout=1)
        await asyncio.wait_for(signalled.wait(), timeout=1)
        self.assertFalse(response.closed)
        self.assertFalse(client.closed)

        response.release_close.set()
        with self.assertRaises(asyncio.CancelledError):
            await next_chunk
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)


class CloseLlmResponseTests(unittest.IsolatedAsyncioTestCase):
    """Unit coverage for the shared cleanup primitive itself.

    The four gate-flagged proxy.py nonstream routes are proven to call this
    exact helper by ``ProxyNonstreamCloseSiteTests`` below (AST-level, so it
    survives refactors); this class proves the helper's own contract once,
    at the source of truth.
    """

    async def test_response_close_failure_still_attempts_client_close_and_propagates(self) -> None:
        client = _ClosableClient()

        class _RaisingResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True
                raise RuntimeError("response close failed")

        response = _RaisingResponse()
        with self.assertRaises(RuntimeError):
            await close_llm_response(client, response)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_client_close_failure_propagates_and_chains_response_error(self) -> None:
        class _RaisingClient:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True
                raise RuntimeError("client close failed")

        class _RaisingResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True
                raise ValueError("response close failed")

        client = _RaisingClient()
        response = _RaisingResponse()
        with self.assertRaises(RuntimeError) as ctx:
            await close_llm_response(client, response)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    async def test_both_close_successfully_when_neither_raises(self) -> None:
        client = _ClosableClient()

        class _OkResponse:
            def __init__(self) -> None:
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        response = _OkResponse()
        await close_llm_response(client, response)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)


class ProxyNonstreamCloseSiteTests(unittest.TestCase):
    """AST invariant: every successful nonstream route in proxy.py must
    route response/client cleanup through the shared close_llm_response
    helper, never a bare sequential resp.aclose()/client.aclose() pair.

    A sequential pair skips client.aclose() (leaking the connection-pool
    transport) whenever resp.aclose() raises. This is a source-level
    boundary check rather than N duplicated end-to-end tests per route: it
    fails the moment any current or future nonstream route reintroduces the
    unguarded pattern, regardless of which route added it.
    """

    def test_no_bare_sequential_resp_and_client_aclose_outside_generators(self) -> None:
        proxy_path = Path(__file__).resolve().parent.parent / "proxy.py"
        tree = ast.parse(proxy_path.read_text(encoding="utf-8"), filename=str(proxy_path))

        violations: list[int] = []

        def _is_generator(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
            found = False

            class _OwnBodyYieldFinder(ast.NodeVisitor):
                def visit_FunctionDef(self, _n: ast.FunctionDef) -> None:
                    pass  # nested def's yields belong to it, not this function

                def visit_AsyncFunctionDef(self, _n: ast.AsyncFunctionDef) -> None:
                    pass

                def visit_Lambda(self, _n: ast.Lambda) -> None:
                    pass

                def visit_Yield(self, _n: ast.Yield) -> None:
                    nonlocal found
                    found = True

                def visit_YieldFrom(self, _n: ast.YieldFrom) -> None:
                    nonlocal found
                    found = True

            for stmt in node.body:
                _OwnBodyYieldFinder().visit(stmt)
            return found

        class _FunctionScanner(ast.NodeVisitor):
            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                # Generators (stream_response/_provider_native_body_stream/etc.)
                # own their own try/finally cleanup and are out of scope — this
                # invariant is about ordinary nonstream request handlers.
                if not _is_generator(node):
                    self._scan_body(node.body, skip_nested_defs=True)
                self.generic_visit(node)

            def _scan_body(self, body: list[ast.stmt], *, skip_nested_defs: bool) -> None:
                for prev, curr in zip(body, body[1:]):
                    if _is_resp_aclose_call(prev) and _is_client_aclose_call(curr):
                        violations.append(curr.lineno)
                for stmt in body:
                    if skip_nested_defs and isinstance(
                        stmt, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        # Nested defs (e.g. stream_response()) are scanned by
                        # their own visit_AsyncFunctionDef call, with their own
                        # generator check — don't double-scan them here.
                        continue
                    for child_body in _nested_bodies(stmt):
                        self._scan_body(child_body, skip_nested_defs=skip_nested_defs)

        def _is_await_call_on(stmt: ast.stmt, obj_name: str, method_name: str) -> bool:
            if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Await):
                return False
            call = stmt.value.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                return False
            if call.func.attr != method_name:
                return False
            return isinstance(call.func.value, ast.Name) and call.func.value.id == obj_name

        def _is_resp_aclose_call(stmt: ast.stmt) -> bool:
            return _is_await_call_on(stmt, "resp", "aclose")

        def _is_client_aclose_call(stmt: ast.stmt) -> bool:
            return _is_await_call_on(stmt, "client", "aclose")

        def _nested_bodies(stmt: ast.stmt):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(stmt, field, None)
                if block:
                    yield block
            for handler in getattr(stmt, "handlers", []) or []:
                yield handler.body

        _FunctionScanner().visit(tree)

        self.assertEqual(
            violations,
            [],
            "Found bare sequential 'await resp.aclose()' followed by "
            "'await client.aclose()' at proxy.py line(s) "
            f"{violations} — route cleanup through close_llm_response so "
            "client teardown isn't skipped when response close raises.",
        )
