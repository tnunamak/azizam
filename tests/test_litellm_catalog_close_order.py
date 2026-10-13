"""The LiteLLM model-catalog fetch closes its client even when the response cannot close."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starlette.requests import Request

import proxy

def _request():
    return Request({
        "type": "http", "method": "GET", "path": "/v1/models",
        "scheme": "http", "server": ("testserver", 80),
        "headers": [], "query_string": b"",
    })


def _transport(monkeypatch, *, body=b'{"data":[]}', status=200):
    events = []
    calls = []
    reads = []

    class Client:
        async def aclose(self):
            events.append("client.close")

    class Response:
        status_code = status

        async def aclose(self):
            events.append("response.close")

    client, response = Client(), Response()

    async def send(execution_request):
        calls.append(execution_request)
        return client, response

    async def read(got_client, got_response):
        assert got_client is client
        assert got_response is response
        reads.append(True)
        return body

    monkeypatch.setattr(proxy, "send_llm_execution_request", send)
    monkeypatch.setattr(proxy, "read_llm_response_body", read)
    monkeypatch.setattr(proxy, "litellm_headers", lambda *a, **kw: {})
    monkeypatch.setattr(proxy, "_litellm_gateway_metadata", lambda *a, **kw: {})
    runtime = SimpleNamespace(
        enabled=True, base_url="http://fixture.invalid", service_id="fixture",
        api_key="",
    )

    async def invoke():
        return await proxy._litellm_models_payload(
            _request(), object(), runtime_config=runtime
        )

    return SimpleNamespace(
        invoke=invoke, events=events, calls=calls, reads=reads,
        client=client, response=response,
    )


def test_response_close_failure_still_closes_catalog_client(monkeypatch):
    fake = _transport(monkeypatch)

    async def broken_close():
        fake.events.append("response.close")
        raise RuntimeError("synthetic response close failure")

    monkeypatch.setattr(fake.response, "aclose", broken_close)

    async def scenario():
        try:
            await fake.invoke()
        except RuntimeError as exc:
            assert str(exc) == "synthetic response close failure"

    asyncio.run(scenario())
    assert fake.events == ["response.close", "client.close"]


