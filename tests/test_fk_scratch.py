import httpx, asyncio
import pytest
import proxy
from tests.support.fake_upstream import fake_upstreams, load_scenario, StreamingASGITransport
from tests.support.gateway import use_admin_db, register_litellm


def test_chat_text(fake_upstreams, monkeypatch, tmp_path):
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/chat-text", model="fk-model", content="hi there"))
    db = use_admin_db(monkeypatch, tmp_path)
    register_litellm(monkeypatch, db, fake.base_url)

    async def go():
        async with httpx.AsyncClient(transport=StreamingASGITransport(proxy.app), base_url="http://gateway.test") as c:
            r = await c.post("/v1/chat/completions", json={"model": "fk-model", "messages": [{"role": "user", "content": "hi"}]})
            return r
    r = asyncio.run(go())
    print(r.status_code, r.text)
    print([q.summary() for q in fake.requests], [dict(q.headers) for q in fake.requests])
    assert r.status_code == 200


def test_chat_stream_loopback(fake_upstreams, monkeypatch, tmp_path):
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/chat-stream", model="fk-model"))
    db = use_admin_db(monkeypatch, tmp_path)
    register_litellm(monkeypatch, db, fake.base_url)

    async def go():
        async with httpx.AsyncClient(transport=StreamingASGITransport(proxy.app), base_url="http://gateway.test") as c:
            r = await c.post("/v1/chat/completions", json={"model": "fk-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
            return r
    r = asyncio.run(go())
    print(r.status_code, r.text)
    assert r.status_code == 200
