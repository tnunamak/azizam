"""Commodity LLM execution transport.

The gateway owns auth, Profiles, local resource policy, and receipts. This
module owns only the final HTTP hop to a selected LLM execution substrate.
Keep it narrow so the substrate boundary does not become a second router.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import os
import re
from typing import Any, Mapping

import httpx

from gateway.service_registry import is_litellm_service_record

LITELLM_BODY_EXECUTION_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/embeddings",
    }
)
LITELLM_DELEGATED_KEY_HEADER = "X-Litellm-Key"
DISCONNECT_POLL_INTERVAL_SECONDS = 0.1

DownstreamDisconnectCheck = Callable[[], Awaitable[bool]]

_SSE_EVENT_END = re.compile(br"\r\n\r\n|\n\n|\r\r")


@dataclass(frozen=True)
class LlmExecutionRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    read_timeout: float


@dataclass(frozen=True)
class LiteLlmRuntimeConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    service_id: str = ""


def litellm_runtime_from_registry(
    registry: Mapping[str, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> LiteLlmRuntimeConfig:
    env = os.environ if env is None else env
    for service in _litellm_service_candidates(registry):
        if not bool(service.get("enabled", True)):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        base_url = _base_url_from_config(config, env)
        if not base_url:
            continue
        return LiteLlmRuntimeConfig(
            enabled=True,
            base_url=base_url,
            api_key=_api_key_from_config(config, env),
            service_id=str(service.get("id") or "litellm"),
        )
    return LiteLlmRuntimeConfig()


def _litellm_service_candidates(
    registry: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(registry, Mapping):
        return []
    services = registry.get("services")
    if not isinstance(services, Mapping):
        return []
    candidates: list[Mapping[str, Any]] = []
    for service_id, raw in services.items():
        if not isinstance(raw, Mapping):
            continue
        service = dict(raw)
        service.setdefault("id", service_id)
        if is_litellm_service_record(service_id, service):
            candidates.append(service)
    return sorted(candidates, key=lambda item: int(item.get("priority") or 100))


def _api_key_from_config(config: Mapping[str, Any], env: Mapping[str, str]) -> str:
    env_name = str(config.get("api_key_env") or config.get("api_key_ref") or "").strip()
    if env_name:
        return env.get(env_name, "")
    return ""


def _base_url_from_config(config: Mapping[str, Any], env: Mapping[str, str]) -> str:
    env_name = str(config.get("base_url_env") or config.get("api_base_env") or "").strip()
    if env_name:
        env_value = str(env.get(env_name) or "").strip()
        if env_value:
            return env_value.rstrip("/")
    return str(
        config.get("base_url")
        or config.get("api_base")
        or config.get("default_base_url")
        or ""
    ).rstrip("/")


def litellm_url(*, base_url: str, path: str, query: str = "") -> str:
    url = f"{str(base_url).rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    return url


def litellm_headers(
    source_headers: Mapping[str, str],
    *,
    api_key: str = "",
    gateway_metadata: Mapping[str, str] | None = None,
    delegated_api_key: str = "",
) -> dict[str, str]:
    headers: dict[str, str] = {
        "content-type": _header(source_headers, "content-type") or "application/json",
        "accept": _header(source_headers, "accept") or "application/json",
    }
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    if delegated_api_key:
        headers[LITELLM_DELEGATED_KEY_HEADER] = f"Bearer {delegated_api_key}"

    for name, value in (gateway_metadata or {}).items():
        if value:
            headers[f"x-ai-gateway-{name}"] = value
    return headers


async def send_llm_execution_request(
    request: LlmExecutionRequest,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=request.read_timeout,
            write=60.0,
            pool=None,
        )
    )
    req = client.build_request(
        method=request.method,
        url=request.url,
        headers=dict(request.headers),
        content=request.body,
    )
    response = await client.send(req, stream=True)
    return client, response


async def read_llm_response_body(
    client: httpx.AsyncClient,
    response: httpx.Response,
    *,
    is_downstream_disconnected: DownstreamDisconnectCheck,
    poll_interval: float = DISCONNECT_POLL_INTERVAL_SECONDS,
) -> bytes | None:
    """Read an upstream body while owning downstream disconnect cleanup.

    ``httpx`` only knows about the upstream socket.  ASGI request handlers also
    need to stop the upstream request when their downstream client disappears;
    otherwise a local inference server can keep decoding after Pi has aborted.
    The caller still owns the normal response lifetime, while this helper closes
    both resources on the disconnect path and returns ``None``.
    """

    async def read() -> bytes:
        return await response.aread()

    try:
        result = await _run_with_disconnect_watch(
            read(),
            is_downstream_disconnected=is_downstream_disconnected,
            poll_interval=poll_interval,
        )
    except BaseException:
        await _close_llm_response(client, response)
        raise
    if result is None:
        await _close_llm_response(client, response)
    return result


async def iter_llm_response_bytes(
    client: httpx.AsyncClient,
    response: httpx.Response,
    *,
    is_downstream_disconnected: DownstreamDisconnectCheck,
    poll_interval: float = DISCONNECT_POLL_INTERVAL_SECONDS,
) -> AsyncIterator[bytes]:
    """Yield upstream bytes and close the upstream on completion/cancellation.

    ``StreamingResponse`` already owns downstream disconnect detection.  Its
    cancellation propagates into this generator, whose ``finally`` closes the
    HTTPX stream.  Polling ``Request.is_disconnected`` here would introduce a
    second consumer of the ASGI receive channel and can strand a response after
    its headers have been sent.

    ``is_downstream_disconnected`` and ``poll_interval`` remain in the
    signature for caller compatibility; streaming cancellation is the source
    of truth for this path.

    A complete OpenAI ``data: [DONE]`` SSE event is also a terminal outcome:
    it ends the semantic response even if the backend leaves its HTTP socket
    open.  The detector is framing-aware, so JSON text containing ``[DONE]``
    and partial SSE lines are ordinary bytes.

    Start the next upstream read before yielding the current chunk.  This lets
    HTTPX release its connection as soon as that read reaches any terminal
    outcome, even when ASGI is still blocked delivering the current chunk to a
    slow downstream client.
    """

    del is_downstream_disconnected, poll_interval

    upstream_iterator = response.aiter_bytes().__aiter__()
    done_detector = _OpenAiSseDoneDetector()
    close_lock = asyncio.Lock()
    closed = False

    async def close_upstream() -> None:
        nonlocal closed
        async with close_lock:
            if closed:
                return
            closed = True
            await _close_llm_response(client, response)

    async def next_chunk() -> bytes:
        try:
            return await anext(upstream_iterator)
        except BaseException:
            try:
                await close_upstream()
            except BaseException:
                # The upstream read outcome is more useful to the caller than
                # a cleanup failure, including when that outcome is cancellation.
                pass
            raise

    pending_chunk = asyncio.create_task(next_chunk())
    try:
        while True:
            try:
                chunk = await pending_chunk
            except StopAsyncIteration:
                return

            pending_chunk = asyncio.create_task(next_chunk())
            output, is_terminal = done_detector.consume(chunk)
            if is_terminal:
                if not pending_chunk.done():
                    pending_chunk.cancel()
                await close_upstream()
                await asyncio.gather(pending_chunk, return_exceptions=True)
                if output:
                    yield output
                return
            if output:
                yield output
    finally:
        if not pending_chunk.done():
            pending_chunk.cancel()
        await close_upstream()
        await asyncio.gather(pending_chunk, return_exceptions=True)


class _OpenAiSseDoneDetector:
    """Recognize a complete OpenAI SSE ``data: [DONE]`` event without rewriting bytes."""

    def __init__(self) -> None:
        self._unfinished_event = b""

    def consume(self, chunk: bytes) -> tuple[bytes, bool]:
        """Return bytes safe to forward and whether this chunk ends the stream."""
        combined = self._unfinished_event + chunk
        previous_length = len(self._unfinished_event)
        event_start = 0

        while match := _SSE_EVENT_END.search(combined, event_start):
            if _is_openai_sse_done_event(combined[event_start:match.start()]):
                event_end = match.end()
                self._unfinished_event = b""
                return chunk[:max(0, event_end - previous_length)], True
            event_start = match.end()

        self._unfinished_event = combined[event_start:]
        return chunk, False


def _is_openai_sse_done_event(event: bytes) -> bool:
    data_lines: list[bytes] = []
    for line in event.splitlines():
        if not line.startswith(b"data:"):
            continue
        data = line[5:]
        if data.startswith(b" "):
            data = data[1:]
        data_lines.append(data)
    return b"\n".join(data_lines) == b"[DONE]"


async def _run_with_disconnect_watch(
    operation: Awaitable[Any],
    *,
    is_downstream_disconnected: DownstreamDisconnectCheck,
    poll_interval: float,
) -> Any | None:
    operation_task = asyncio.create_task(operation)
    disconnected_task = asyncio.create_task(
        _wait_for_downstream_disconnect(
            is_downstream_disconnected,
            poll_interval,
        )
    )
    try:
        done, pending = await asyncio.wait(
            {operation_task, disconnected_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        if disconnected_task in done:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            return None
        return operation_task.result()
    finally:
        if not operation_task.done():
            operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        if not disconnected_task.done():
            disconnected_task.cancel()


async def _wait_for_downstream_disconnect(
    is_downstream_disconnected: DownstreamDisconnectCheck,
    poll_interval: float,
) -> None:
    while True:
        try:
            disconnected = await asyncio.wait_for(
                is_downstream_disconnected(),
                timeout=poll_interval,
            )
        except asyncio.TimeoutError:
            disconnected = False
        if disconnected:
            return
        await asyncio.sleep(poll_interval)


async def _close_llm_response(
    client: httpx.AsyncClient,
    response: httpx.Response,
) -> None:
    try:
        await response.aclose()
    finally:
        await client.aclose()


def _header(headers: Mapping[str, str], name: str) -> str:
    direct = headers.get(name)
    if direct is not None:
        return str(direct)
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return str(value)
    return ""
