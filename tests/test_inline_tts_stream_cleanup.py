"""Synthetic direct-path checks for streamed response ownership."""
import asyncio

import httpx
import pytest

from gateway.engine_protocol import inline_tts
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.types import Reply, StreamReply


class FakeResponse:
    def __init__(self, calls, status=200, close_error=None):
        self.calls = calls
        self.status_code = status
        self.headers = httpx.Headers({"content-type": "application/octet-stream"})
        self.close_error = close_error

    async def aread(self):
        return b"synthetic reply"

    async def aiter_raw(self):
        yield b"synthetic chunk"

    async def aclose(self):
        self.calls.append("response")
        if self.close_error is not None:
            raise self.close_error


class FakeClient:
    def __init__(self, calls, response=None, send_error=None, close_error=None):
        self.calls = calls
        self.response = response
        self.send_error = send_error
        self.close_error = close_error

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, *, stream):
        assert stream is True
        if self.send_error is not None:
            raise self.send_error
        return self.response

    async def aclose(self):
        self.calls.append("client")
        if self.close_error is not None:
            raise self.close_error


def install_client(monkeypatch, client):
    monkeypatch.setattr(inline_tts.engine_egress, "async_client", lambda **kwargs: client)


async def streamed():
    return await inline_tts.InlineTtsAdapter._streamed(
        "https://synthetic.invalid/speech", {}, b"{}", httpx.Timeout(1)
    )


def test_stream_close_closes_client_after_response_error(monkeypatch):
    calls = []
    error = RuntimeError("synthetic response cleanup")
    client = FakeClient(calls, FakeResponse(calls, close_error=error))
    install_client(monkeypatch, client)

    async def exercise():
        reply = await streamed()
        assert isinstance(reply, StreamReply)
        assert calls == []
        assert [chunk async for chunk in reply] == [b"synthetic chunk"]
        with pytest.raises(RuntimeError) as caught:
            await reply.aclose()
        assert caught.value is error
        assert calls == ["response", "client"]

    asyncio.run(exercise())


@pytest.mark.parametrize("client_fails", [False, True])
def test_error_reply_survives_cleanup_errors(monkeypatch, client_fails):
    calls = []
    client = FakeClient(
        calls, FakeResponse(calls, status=429, close_error=RuntimeError("response cleanup")),
        close_error=RuntimeError("client cleanup") if client_fails else None,
    )
    install_client(monkeypatch, client)
    reply = asyncio.run(streamed())
    assert isinstance(reply, Reply)
    assert reply.status == 429
    assert reply.raw == b"synthetic reply"
    assert reply.headers["content-type"] == "application/octet-stream"
    assert calls == ["response", "client"]


def test_connect_failure_survives_client_cleanup_error(monkeypatch):
    calls = []
    primary = httpx.ConnectError("synthetic connection failure")
    client = FakeClient(calls, send_error=primary, close_error=RuntimeError("client cleanup"))
    install_client(monkeypatch, client)
    with pytest.raises(TransportFailure) as caught:
        asyncio.run(streamed())
    assert caught.value.kind == "unreachable"
    assert caught.value.code == "transport_failed"
    assert str(caught.value) == "The engine could not be reached (ConnectError)."
    assert caught.value.__cause__ is primary
    assert calls == ["client"]


@pytest.mark.parametrize(
    ("waiting_close", "client_fails"),
    [("response", False), ("response", True), ("client", False)],
    ids=["response", "response-client-error", "client"],
)
def test_error_reply_does_not_swallow_close_cancellation(monkeypatch, waiting_close, client_fails):
    calls = []

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        response = FakeResponse(calls, status=429)
        client = FakeClient(
            calls, response,
            close_error=RuntimeError("client cleanup") if client_fails else None,
        )
        install_client(monkeypatch, client)
        target = response if waiting_close == "response" else client

        async def suspended_close():
            calls.append(waiting_close)
            entered.set()
            await release.wait()

        monkeypatch.setattr(target, "aclose", suspended_close)
        task = asyncio.create_task(streamed())
        try:
            # Cancel only once the buffered 429 has reached the selected close.
            # An Event provides a real suspension point, not a timing sleep.
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert calls == (["response"] if waiting_close == "response"
                             else ["response", "client"])
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert task.cancelled()
            assert calls == ["response", "client"]
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
