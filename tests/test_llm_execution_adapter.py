from __future__ import annotations

import asyncio
import unittest

from starlette.responses import StreamingResponse

from gateway.backends.llm_execution import (
    LITELLM_BODY_EXECUTION_PATHS,
    iter_llm_response_bytes,
    litellm_headers,
    litellm_runtime_from_registry,
    read_llm_response_body,
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

    assert headers["authorization"] == "Bearer sk-litellm-internal"
    assert headers["content-type"] == "application/json"
    assert headers["x-ai-gateway-execution"] == "litellm"
    assert headers["x-ai-gateway-profile"] == "family"
    assert headers["x-ai-gateway-key-prefix"] == "sha256:abcdef"
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
                        "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011/",
                        "api_key_env": "AI_GATEWAY_LITELLM_API_KEY",
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
                        "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011",
                    },
                }
            }
        },
        env={"AI_GATEWAY_LITELLM_BASE_URL": "http://127.0.0.1:4999/"},
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
                is_downstream_disconnected=self._must_not_poll_disconnect,
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
                is_downstream_disconnected=self._must_not_poll_disconnect,
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
                is_downstream_disconnected=self._must_not_poll_disconnect,
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
            is_downstream_disconnected=self._must_not_poll_disconnect,
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
                is_downstream_disconnected=self._must_not_poll_disconnect,
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
                is_downstream_disconnected=self._must_not_poll_disconnect,
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
                is_downstream_disconnected=self._must_not_poll_disconnect,
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

    async def _must_not_poll_disconnect(self) -> bool:
        self.fail("stream adapter must not compete for the ASGI receive channel")

    async def test_stream_forwards_all_bytes_and_closes_at_upstream_eof(self) -> None:
        client = _ClosableClient()
        response = _FiniteChunkResponse()

        chunks = [
            chunk
            async for chunk in iter_llm_response_bytes(
                client,
                response,
                is_downstream_disconnected=self._must_not_poll_disconnect,
            )
        ]

        self.assertEqual(chunks, [b"data: first\n\n", b"data: [DONE]\n\n"])
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_body_read_closes_upstream_when_downstream_disconnects(self) -> None:
        client = _ClosableClient()
        response = _BlockingResponse()
        disconnected = False

        async def is_disconnected() -> bool:
            return disconnected

        read_task = asyncio.create_task(
            read_llm_response_body(
                client,
                response,
                is_downstream_disconnected=is_disconnected,
                poll_interval=0.001,
            )
        )
        await asyncio.sleep(0.01)
        disconnected = True

        self.assertIsNone(await asyncio.wait_for(read_task, timeout=1))
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

    async def test_stream_iteration_closes_upstream_when_handler_is_cancelled(self) -> None:
        client = _ClosableClient()
        response = _ChunkResponse()
        async def is_disconnected() -> bool:
            return False

        chunks = iter_llm_response_bytes(
            client,
            response,
            is_downstream_disconnected=is_disconnected,
            poll_interval=0.001,
        )
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

        async def is_disconnected() -> bool:
            return False

        read_task = asyncio.create_task(
            read_llm_response_body(
                client,
                response,
                is_downstream_disconnected=is_disconnected,
                poll_interval=0.001,
            )
        )
        await asyncio.sleep(0.01)
        read_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await read_task
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
