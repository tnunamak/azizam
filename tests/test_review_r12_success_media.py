"""Independent review: preserve the encoding of a successful transcription."""
import httpx
import pytest
import proxy
import asyncio
from unittest.mock import AsyncMock, patch
from starlette.requests import Request
from gateway import client_errors, stt_chunking
from tests.support.access import access_context
from tests.support.fake_upstream.transport import HandlerTransport, HostRouter
from tests.test_stt_chunking import _config, _multipart_body


@pytest.mark.parametrize("content_type", [
    'text/plain; charset="iso-8859-1"',
    'text/plain; format=flowed; charset=iso-8859-1',
])
def test_stt_success_preserves_transcript_encoding(content_type):
    upstream = stt_chunking.UpstreamResponse(200, b"caf\xe9", {"content-type": content_type})
    response = proxy._stt_response_from_upstream(upstream)
    client_response = httpx.Response(response.status_code, content=response.body, headers=response.headers)
    assert client_response.text == "caf\u00e9"


@pytest.mark.parametrize("content_type", [
    "audio/mpeg", "text/event-stream; charset=utf-8", "application/problem+json",
])
def test_ordinary_media_types_are_preserved(content_type):
    assert client_errors.safe_content_type(content_type) == content_type


def test_stt_text_route_preserves_quoted_charset(tmp_path, monkeypatch):
    router = HostRouter(monkeypatch)

    async def run():
        body, content_type = _multipart_body(fields=(("model", "whisper-1"), ("response_format", "text")))
        sent = False
        requests = []

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Future()

        async def engine(request):
            requests.append(request)
            return httpx.Response(200, content=b"caf\xe9", headers={"content-type": 'text/plain; charset="iso-8859-1"'})

        router.add_transport("127.0.0.1:9876", HandlerTransport(engine))
        request = Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                           "headers": [(b"content-type", content_type.encode())]}, receive)
        with (
            patch.dict(proxy.config["backends"]),
            patch.object(proxy, "_stt_execution_snapshot", return_value=_config(tmp_path)),
            patch.object(stt_chunking, "probe_audio", new=AsyncMock(return_value=stt_chunking.ProbedAudio(2, 8000, 1))),
            patch.object(proxy.receipt_store, "begin"),
            patch.object(proxy.receipt_store, "finalize", return_value=True),
        ):
            proxy.config["backends"].pop("stt-parakeet-gpu", None)
            response = await proxy.proxy_stt_request(request, access_context())
        assert len(requests) == 1
        assert response.status_code == 200
        assert response.body == b"caf\xe9"
        assert httpx.Response(200, content=response.body, headers=response.headers).text == "caf\u00e9"

    asyncio.run(run())


@pytest.mark.parametrize("raw, expected", [
    ('text/plain; charset="iso-8859-1"', "text/plain; charset=iso-8859-1"),
    ("text/plain; format=flowed; charset=iso-8859-1", "text/plain; charset=iso-8859-1"),
    ("Text/Plain; CHARSET=ISO-8859-1", "text/plain; charset=iso-8859-1"),
    ("text/plain; charset=not-a-real-charset", "text/plain"),
    ('text/plain; charset="utf-8\r\nX: y"', "text/plain"),
    ("text/plain", "text/plain"),
    ("audio/mpeg", "audio/mpeg"),
    ("text/event-stream", "text/event-stream"),
    ("application/json; charset=utf-8", "application/json; charset=utf-8"),
    ("application/vnd.x+json; profile=a", "application/vnd.x+json"),
    ("multipart/mixed; boundary=abc", "multipart/mixed"),
    ("not a type", "application/json"),
    ("", "application/json"),
])
def test_safe_content_type_parses_parameters(raw, expected):
    assert client_errors.safe_content_type(raw) == expected
