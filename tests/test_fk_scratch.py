import httpx, asyncio, json
import pytest
import proxy
from tests.support.fake_upstream import fake_upstreams, load_scenario, StreamingASGITransport
from tests.support.gateway import use_admin_db, register_litellm

CHAT = {"model": "fk-model", "messages": [{"role": "user", "content": "hi"}]}

def client():
    return httpx.AsyncClient(transport=StreamingASGITransport(proxy.app), base_url="http://gateway.test")

def setup(fake_upstreams, monkeypatch, tmp_path, name, loop=False, **v):
    sc = load_scenario(f"openai-compatible/{name}", model="fk-model", **v)
    fake = fake_upstreams.loopback(sc) if loop else fake_upstreams.asgi(sc)
    db = use_admin_db(monkeypatch, tmp_path)
    register_litellm(monkeypatch, db, fake.base_url)
    return fake

@pytest.mark.parametrize("stream", [False, True])
def test_429(fake_upstreams, monkeypatch, tmp_path, stream):
    setup(fake_upstreams, monkeypatch, tmp_path, "rate-limited")
    async def go():
        async with client() as c:
            return await c.post("/v1/chat/completions", json={**CHAT, "stream": stream})
    r = asyncio.run(go()); print("429", stream, r.status_code, dict(r.headers), r.text)

@pytest.mark.parametrize("stream", [False, True])
def test_slow(fake_upstreams, monkeypatch, tmp_path, stream):
    setup(fake_upstreams, monkeypatch, tmp_path, "slow-first-byte", loop=True)
    monkeypatch.setattr(proxy, "UPSTREAM_READ_TIMEOUT", 0.2)
    async def go():
        async with client() as c:
            return await c.post("/v1/chat/completions", json={**CHAT, "stream": stream})
    r = asyncio.run(go()); print("slow", stream, r.status_code, dict(r.headers), r.text)

def test_disconnect(fake_upstreams, monkeypatch, tmp_path):
    setup(fake_upstreams, monkeypatch, tmp_path, "stream-disconnect", loop=True)
    async def go():
        got = b""
        async with client() as c:
            async with c.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as r:
                print("disc status", r.status_code)
                try:
                    async for ch in r.aiter_raw():
                        got += ch
                except Exception as e:
                    print("disc error", type(e), e, repr(e.__cause__))
        return got
    print("disc got", asyncio.run(go()))

@pytest.mark.parametrize("name", ["stream-split-event", "stream-split-think"])
def test_split(fake_upstreams, monkeypatch, tmp_path, name):
    setup(fake_upstreams, monkeypatch, tmp_path, name, loop=True)
    stages = [1, 2, 4] if name == "stream-split-event" else [1]
    clock = fake_upstreams.clock
    async def go():
        got = b""
        async with asyncio.timeout(10):
          async with client() as c:
            async with c.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as r:
                async for ch in r.aiter_raw():
                    got += ch
                    n = got.count(b"\n\n")
                    while stages and n >= stages[0]:
                        await clock.until_waiting(); clock.advance(1); stages.pop(0)
        return got
    print(name, asyncio.run(go()).decode())

def test_anthropic(fake_upstreams, monkeypatch, tmp_path):
    setup(fake_upstreams, monkeypatch, tmp_path, "chat-text", content="hey")
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", proxy.app)
    async def go():
        async with client() as c:
            return await c.post("/v1/messages", json={"model": "fk-model", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})
    r = asyncio.run(go()); print("anth", r.status_code, r.text)

def test_ollama(fake_upstreams, monkeypatch, tmp_path):
    setup(fake_upstreams, monkeypatch, tmp_path, "chat-text", content="hey")
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", proxy.app)
    async def go():
        async with client() as c:
            return await c.post("/api/chat", json={"model": "fk-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]})
    r = asyncio.run(go()); print("ollama", r.status_code, r.text)
