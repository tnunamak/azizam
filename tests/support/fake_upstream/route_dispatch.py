"""Offline route dispatch transports: exact deployments, controllable event boundaries."""
import asyncio
import json

import httpx
from starlette.requests import Request


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events, *, delay=0):
        self.events, self.delay, self.closed = events, delay, False

    async def __aiter__(self):
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(event, BaseException):
                raise event
            yield event

    async def aclose(self):
        self.closed = True


def event(**value):
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


def request_for(body, *, surface="responses", disconnected=None):
    sent = False
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        if disconnected is None:
            await asyncio.Future()
        else:
            await disconnected.wait()
        return {"type": "http.disconnect"}
    return Request({"type": "http", "method": "POST", "scheme": "http", "server": ("example.test", 80),
                    "path": "/v1/responses" if surface == "responses" else "/v1/chat/completions",
                    "headers": [], "query_string": b""}, receive)


def paid_sender(paid_calls):
    async def send_paid(execution, **kwargs):
        paid_calls.append(json.loads(execution.body))
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
        data = {"id": "paid-generation", "model": "wire-exact", "choices": [{"index": 0, "message": {"role": "assistant", "content": "paid"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        if json.loads(execution.body).get("stream"):
            chunks = [event(id="paid-generation", choices=[{"index": 0, "delta": {"role": "assistant", "content": "paid"}, "finish_reason": None}]),
                      event(id="paid-generation", choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage=data["usage"]), b"data: [DONE]\n\n"]
            response = httpx.Response(200, stream=EventStream(chunks), headers={"content-type": "text/event-stream"})
        else:
            response = httpx.Response(200, json=data)
        return client, response
    return send_paid


class ReadyDisconnectWatcher:
    """Signal disconnect in the same scheduler step as a completed start."""

    def __init__(self, ready):
        self.ready = ready

    async def wait(self):
        await self.ready.wait()

    async def aclose(self):
        pass


def loopback_sender(app):
    """Run the real chat ingress in process, retaining its headers and SSE."""
    async def forward(request, auth_context, body, *, stream):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test")
        try:
            response = await client.post('/v1/chat/completions', json=body, headers={'authorization': 'Bearer k'})
        except BaseException:
            await client.aclose()
            raise
        if stream:
            return client, response
        await client.aclose()
        return response
    return forward
