"""Commodity LLM execution transport.

The gateway owns auth, Profiles, local resource policy, and receipts. This
module owns only the final HTTP hop to a selected LLM execution substrate.
Keep it narrow so the substrate boundary does not become a second router.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping as MappingABC
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from gateway.backends.cancellation import CancellationToken
from gateway.service_registry import is_litellm_service_record

LITELLM_BODY_EXECUTION_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/embeddings",
    }
)
LITELLM_DELEGATED_KEY_HEADER = "X-Litellm-Key"
GATEWAY_REQUEST_ID_HEADER = "X-AI-Gateway-Request-Id"

ASGIReceive = Callable[[], Awaitable[MappingABC[str, Any]]]

_SSE_EVENT_END = re.compile(br"\r\n\r\n|\n\n|\r\r")
_OPENAI_LITELLM_EXECUTION_BODY_KEYS = frozenset({
    # OpenAI chat/completions/embeddings request body fields accepted at this
    # gateway boundary. Top-level metadata is intentionally excluded even
    # though OpenAI documents it for some APIs: LiteLLM consumes metadata as
    # router context, so forwarding caller-controlled metadata can alter
    # routing, retries, and fallback behavior. Nested metadata in user content,
    # tool schemas, response schemas, and messages is preserved.
    "audio",
    "best_of",
    "dimensions",
    "encoding_format",
    "echo",
    "frequency_penalty",
    "function_call",
    "functions",
    "input",
    "logit_bias",
    "logprobs",
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "modalities",
    "model",
    "moderation",
    "n",
    "parallel_tool_calls",
    "prediction",
    "presence_penalty",
    "prompt_cache_key",
    "prompt_cache_options",
    "prompt_cache_retention",
    "prompt",
    "reasoning_effort",
    "response_format",
    "safety_identifier",
    "seed",
    "service_tier",
    "stop",
    "store",
    "stream",
    "stream_options",
    "suffix",
    "temperature",
    "tool_choice",
    "tools",
    "top_logprobs",
    "top_p",
    "user",
    "verbosity",
    "web_search_options",
})


@dataclass(frozen=True)
class LlmExecutionRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    read_timeout: float
    trusted_num_retries: int | None = None
    trusted_max_retries: int | None = None


class LlmExecutionPredispatchError(httpx.HTTPError):
    """The upstream request failed before any bytes were dispatched."""


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


def sanitize_litellm_execution_request(request: LlmExecutionRequest) -> LlmExecutionRequest:
    url = _strip_litellm_router_query_overrides(request.url)
    body = _strip_litellm_router_body_overrides(
        request.body,
        trusted_num_retries=request.trusted_num_retries,
        trusted_max_retries=request.trusted_max_retries,
    )
    if url == request.url and body == request.body:
        return request
    return LlmExecutionRequest(
        method=request.method,
        url=url,
        headers=request.headers,
        body=body,
        read_timeout=request.read_timeout,
        trusted_num_retries=request.trusted_num_retries,
        trusted_max_retries=request.trusted_max_retries,
    )


def _strip_litellm_router_query_overrides(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.query:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))


def _strip_litellm_router_body_overrides(
    body: bytes,
    *,
    trusted_num_retries: int | None,
    trusted_max_retries: int | None,
) -> bytes:
    try:
        payload = json.loads(body or b"{}")
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(payload, dict):
        return body
    sanitized = {
        key: value
        for key, value in payload.items()
        if key in _OPENAI_LITELLM_EXECUTION_BODY_KEYS
    }
    if trusted_num_retries is not None:
        sanitized["num_retries"] = trusted_num_retries
    if trusted_max_retries is not None:
        sanitized["max_retries"] = trusted_max_retries
    if sanitized == payload:
        return body
    return json.dumps(sanitized, separators=(",", ":")).encode("utf-8")


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
    if delegated_api_key:
        headers[LITELLM_DELEGATED_KEY_HEADER] = f"Bearer {delegated_api_key}"
    elif api_key:
        headers[LITELLM_DELEGATED_KEY_HEADER] = f"Bearer {api_key}"

    for name, value in (gateway_metadata or {}).items():
        if value:
            headers[f"x-ai-gateway-{name}"] = value
    return headers


class RequestDisconnectWatcher:
    """Own exactly one persistent ASGI ``receive()`` task for one request.

    ASGI's ``receive()`` is not safe to cancel-and-reinvoke on a poll tick —
    Starlette/uvicorn document it as a single logical stream, and repeatedly
    wrapping it in ``asyncio.wait_for(receive(), timeout=...)`` cancels and
    restarts it every tick. That pattern was the cause of a proven bug: a
    deterministic probe observed five ``receive()`` starts and five
    cancellations in 40ms of one non-stream wait. This class instead starts
    one background task that calls ``receive()`` in a loop exactly once per
    request, after the request body has already been fully consumed (so this
    is the sole remaining consumer of the ASGI receive channel — competing
    with ``StreamingResponse``'s own receive loop would strand a response
    after headers are sent, so this watcher must never be built for a
    streaming response).

    ``wait()`` resolves when ``http.disconnect`` arrives (or immediately if it
    already has). ``aclose()`` cancels the background task and awaits it,
    tolerating cancellation while blocked inside ASGI ``receive()`` — the
    background task is the sole entity that ever awaits that raw call, so
    cancelling and awaiting *this* task (rather than a fresh
    ``wait_for``-wrapped call every tick) does not re-enter ``receive()``.
    Safe to call ``aclose()`` more than once, and safe to construct without
    ever calling ``wait()``.
    """

    def __init__(self, receive: ASGIReceive) -> None:
        self._disconnected = asyncio.Event()
        self._task: asyncio.Task[None] = asyncio.create_task(self._watch(receive))

    async def _watch(self, receive: ASGIReceive) -> None:
        try:
            while not self._disconnected.is_set():
                message = await receive()
                if message.get("type") == "http.disconnect":
                    self._disconnected.set()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException:
            # A receive-channel error is not itself a disconnect signal; the
            # caller's own read/dispatch will surface the real failure.
            return

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    async def wait(self) -> None:
        await self._disconnected.wait()

    async def aclose(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


async def send_llm_execution_request(
    request: LlmExecutionRequest,
    *,
    disconnect_watcher: RequestDisconnectWatcher | None = None,
    cancellation_token: CancellationToken | None = None,
    on_signal: Callable[[], Awaitable[None]] | None = None,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Dispatch the upstream request, racing pre-header disconnect/cancel.

    ``disconnect_watcher``, when given, races the connect+send against a
    local client disconnect observed *before* any upstream response headers
    have arrived — the outer gateway hop's own ASGI connection.

    ``cancellation_token``, when given, races the same connect+send against
    a cross-hop cancel signal (see ``gateway/backends/cancellation.py``).
    This is how the internal gateway hop — which has no local downstream
    client of its own, only the forwarded ``X-AI-Gateway-Request-Id`` — stops
    a pre-header wait on its own backend once the outer route has already
    signalled the registry; without this, a backend that never returns
    headers (idle, prompt-processing, etc.) leaves the internal hop stuck
    even after the outer client is long gone.

    Either, both, or neither may be given. If the client/cross-hop signal
    wins the race, the in-flight ``client.send`` task is cancelled, the
    client is closed promptly, and ``ClientDisconnectedBeforeHeaders`` is
    raised so the caller can stop without ever having dispatched a body to a
    downstream consumer.

    ``on_signal``, when given, is awaited the instant a disconnect/cancel is
    observed to have won the race — strictly before ``send_task`` is
    cancelled and before the client is closed. Cross-hop signalling must not
    wait on local teardown: a blocked ``client.aclose()`` must never delay
    the internal hop's own reader from waking. If ``on_signal`` itself
    raises, teardown still proceeds and ``ClientDisconnectedBeforeHeaders``
    is still raised (the on_signal failure is not swallowed silently, but it
    also must not prevent local cleanup — see the ``finally`` below, which
    always runs regardless of how the signalled branch exits).
    """
    request = sanitize_litellm_execution_request(request)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=request.read_timeout,
            write=60.0,
            pool=None,
        )
    )
    try:
        req = client.build_request(
            method=request.method,
            url=request.url,
            headers=dict(request.headers),
            content=request.body,
        )
    except Exception as exc:
        await client.aclose()
        raise LlmExecutionPredispatchError("LLM execution request could not be prepared") from exc

    if disconnect_watcher is None and cancellation_token is None:
        try:
            response = await client.send(req, stream=True)
        except BaseException:
            await client.aclose()
            raise
        return client, response

    send_task = asyncio.create_task(client.send(req, stream=True))
    watch_tasks: list[asyncio.Task[Any]] = []
    if disconnect_watcher is not None:
        watch_tasks.append(asyncio.create_task(disconnect_watcher.wait()))
    if cancellation_token is not None:
        watch_tasks.append(asyncio.create_task(cancellation_token.wait()))
    try:
        done, _ = await asyncio.wait(
            {send_task, *watch_tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # A disconnect/cancel observed in the same tick headers also arrived
        # still wins — checking watch_tasks membership in ``done`` first
        # (rather than "send_task not in done") means a response that
        # happened to complete in the exact instant the client vanished is
        # never dispatched to a caller who will just read a vanished client.
        if not any(task in done for task in watch_tasks):
            # ``send_task.result()`` re-raises here if the send itself
            # failed (connect error, protocol failure, ...) — the client
            # must still be closed on that path, same as the no-watcher
            # branch above; it is otherwise never referenced again.
            try:
                return client, send_task.result()
            except BaseException:
                await client.aclose()
                raise
        if on_signal is not None:
            await on_signal()
        send_task.cancel()
        await asyncio.gather(send_task, return_exceptions=True)
        await client.aclose()
        raise ClientDisconnectedBeforeHeaders()
    finally:
        for task in watch_tasks:
            if not task.done():
                task.cancel()
        if watch_tasks:
            await asyncio.gather(*watch_tasks, return_exceptions=True)


class ClientDisconnectedBeforeHeaders(Exception):
    """Raised when the client disconnects before upstream headers arrive."""


async def read_llm_response_body(
    client: httpx.AsyncClient,
    response: httpx.Response,
    *,
    disconnect_watcher: RequestDisconnectWatcher | None = None,
    cancellation_token: CancellationToken | None = None,
    on_signal: Callable[[], Awaitable[None]] | None = None,
) -> bytes | None:
    """Read an upstream body while owning downstream disconnect cleanup.

    ``httpx`` only knows about the upstream socket.  ASGI request handlers also
    need to stop the upstream request when their downstream client disappears;
    otherwise a local inference server can keep decoding after the client has
    aborted. The caller still owns the normal response lifetime, while this
    helper closes both resources on the disconnect path and returns ``None``.

    ``disconnect_watcher``, when given, is the single persistent ASGI receive
    watcher for this request (see ``RequestDisconnectWatcher``) — raced
    against the read, never polled.

    ``cancellation_token``, when given, is a second — engine-agnostic — signal
    raced alongside the disconnect watcher. It is how a cross-hop cancel
    (outer gateway -> LiteLLM -> internal gateway) reaches a backend read
    that has no direct view of the outer client's ASGI connection. See
    ``gateway/backends/cancellation.py``.

    ``on_signal``, when given, is awaited the instant a disconnect/cancel is
    observed to have won the race against the read — strictly before the
    read task is cancelled and before ``response``/``client`` are closed.
    Cross-hop signalling must never wait behind local teardown that can
    block (a stalled ``response.aclose()``); the internal hop's own reader
    must be woken first.
    """

    read_task = asyncio.create_task(response.aread())
    watch_tasks: list[asyncio.Task[Any]] = []
    if disconnect_watcher is not None:
        watch_tasks.append(asyncio.create_task(disconnect_watcher.wait()))
    if cancellation_token is not None:
        watch_tasks.append(asyncio.create_task(cancellation_token.wait()))

    try:
        if watch_tasks:
            done, pending = await asyncio.wait(
                {read_task, *watch_tasks},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # A disconnect/cancel observed in the same tick the read also
            # finished is still a disconnect — checking "did a watcher fire"
            # before "did the read finish" (rather than the reverse) means a
            # request whose body happened to complete in the exact instant
            # its client vanished is never reported as a normal success.
            if any(task in done for task in watch_tasks):
                if on_signal is not None:
                    await on_signal()
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                await close_llm_response(client, response)
                return None
        result = await read_task
    except BaseException:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        await close_llm_response(client, response)
        raise
    finally:
        for task in watch_tasks:
            if not task.done():
                task.cancel()
        if watch_tasks:
            await asyncio.gather(*watch_tasks, return_exceptions=True)
    return result


async def iter_llm_response_bytes(
    client: httpx.AsyncClient,
    response: httpx.Response,
    *,
    cancellation_token: CancellationToken | None = None,
    on_signal: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    """Yield upstream bytes and close the upstream on completion/cancellation.

    ``StreamingResponse`` already owns downstream disconnect detection.  Its
    cancellation propagates into this generator, whose ``finally`` closes the
    HTTPX stream.  Polling ``Request.is_disconnected`` here would introduce a
    second consumer of the ASGI receive channel and can strand a response after
    its headers have been sent — this generator deliberately never touches
    ASGI receive; streaming cancellation is the source of truth for the
    immediate hop.

    ``cancellation_token``, when given, is raced alongside upstream reads so a
    cross-hop cancel signal (see ``gateway/backends/cancellation.py``) can stop
    this reader even when the local ASGI cancellation path does not fire
    directly here — e.g. this generator is itself running behind LiteLLM on
    the internal gateway hop, where there is no local downstream client to
    disconnect from.

    ``on_signal``, when given, is awaited the instant a cancellation is
    observed — either the cross-hop token winning the race against the next
    chunk, or this generator itself being cancelled by Starlette (local ASGI
    disconnect) — strictly before the upstream response/client are closed.
    Signalling must never wait behind a stalled ``response.aclose()``; the
    internal hop's own reader must be woken first. Not called for the two
    non-cancellation terminal paths (upstream EOF, ``[DONE]`` detection) —
    those are ordinary completions, not cancellations.

    A complete OpenAI ``data: [DONE]`` SSE event is also a terminal outcome:
    it ends the semantic response even if the backend leaves its HTTP socket
    open.  The detector is framing-aware, so JSON text containing ``[DONE]``
    and partial SSE lines are ordinary bytes.

    Start the next upstream read before yielding the current chunk.  This lets
    HTTPX release its connection as soon as that read reaches any terminal
    outcome, even when ASGI is still blocked delivering the current chunk to a
    slow downstream client.
    """

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
            await close_llm_response(client, response)

    async def signal_cancelled() -> None:
        if on_signal is not None:
            try:
                await on_signal()
            except BaseException:
                # Signalling failure must not block or mask the caller's own
                # cancellation/close outcome — it is best-effort notification
                # to a registry, not part of this generator's own contract.
                pass

    async def next_chunk() -> bytes:
        try:
            return await anext(upstream_iterator)
        except asyncio.CancelledError as read_error:
            # A direct cancellation of THIS task (Starlette cancels the
            # generator's own suspended await point — here, mid-upstream-
            # read — when it delivers a local ASGI disconnect) must signal
            # before this task's own close_upstream() call, not after —
            # the outer generator's try/except never gets a chance to run
            # first, since this coroutine owns the cancellation and closes
            # the upstream itself before the CancelledError ever propagates
            # to the caller.
            await signal_cancelled()
            try:
                await close_upstream()
            except BaseException as close_error:
                raise read_error from close_error
            raise
        except BaseException as read_error:
            try:
                await close_upstream()
            except BaseException as close_error:
                # The upstream read outcome is more useful to the caller than
                # a cleanup failure, including when that outcome is
                # cancellation — but the cleanup failure must still be
                # observable, not silently dropped, so it is chained onto
                # the read error rather than discarded.
                raise read_error from close_error
            raise

    pending_chunk = asyncio.create_task(next_chunk())
    cancel_wait = (
        asyncio.create_task(cancellation_token.wait())
        if cancellation_token is not None
        else None
    )
    generator_cancelled = False
    try:
        while True:
            if cancel_wait is not None:
                done, _ = await asyncio.wait(
                    {pending_chunk, cancel_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancellation wins even if the next chunk also became ready
                # in the same tick — a cancel signal observed at the same
                # instant a read happens to finish is still a cancel; do not
                # let a race let one more chunk through before stopping.
                if cancel_wait in done:
                    await signal_cancelled()
                    if not pending_chunk.done():
                        pending_chunk.cancel()
                    await close_upstream()
                    await asyncio.gather(pending_chunk, return_exceptions=True)
                    return

            try:
                chunk = await pending_chunk
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                generator_cancelled = True
                raise

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
    except asyncio.CancelledError:
        generator_cancelled = True
        raise
    finally:
        if generator_cancelled:
            await signal_cancelled()
        if not pending_chunk.done():
            pending_chunk.cancel()
        await close_upstream()
        await asyncio.gather(pending_chunk, return_exceptions=True)
        if cancel_wait is not None and not cancel_wait.done():
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)


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


async def close_llm_response(
    client: httpx.AsyncClient,
    response: httpx.Response,
) -> None:
    """Attempt both response and client closure, always attempting both.

    ``response.aclose()`` failing must not skip ``client.aclose()`` — the
    client owns the underlying transport/connection pool, and skipping its
    teardown because the response's own close raised would leak a live
    connection even though the caller believes cleanup ran. Both are always
    attempted; if either raises, the failure propagates (the first failure
    if both raise, via exception chaining) rather than being silently
    treated as a successful close.
    """
    response_error: BaseException | None = None
    try:
        await response.aclose()
    except BaseException as exc:  # noqa: BLE001
        response_error = exc
    try:
        await client.aclose()
    except BaseException as client_error:  # noqa: BLE001
        if response_error is not None:
            raise client_error from response_error
        raise
    if response_error is not None:
        raise response_error


def _header(headers: Mapping[str, str], name: str) -> str:
    direct = headers.get(name)
    if direct is not None:
        return str(direct)
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return str(value)
    return ""
