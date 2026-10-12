import asyncio, pytest
import proxy
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

FRONT = {
 "anthropic": ("/v1/messages", lambda s: {"model": "fk-model", "max_tokens": 16, "stream": s, "messages": [{"role": "user", "content": "hi"}]}),
 "ollama": ("/api/chat", lambda s: {"model": "fk-model", "stream": s, "messages": [{"role": "user", "content": "hi"}]}),
}
CASES = [("stream-split-event", True, True, [1,2,4]), ("stream-disconnect", True, True, []), ("rate-limited", True, False, []), ("rate-limited", False, False, []), ("slow-first-byte", True, True, []), ("slow-first-byte", False, True, []), ("chat-stream", True, False, []), ("chat-text", False, False, [])]

@pytest.mark.parametrize("front", list(FRONT))
@pytest.mark.parametrize("name,stream,wire,stages", CASES)
def test_obs(fake_upstreams, monkeypatch, tmp_path, front, name, stream, wire, stages):
    sc = load_scenario(f"openai-compatible/{name}", model="fk-model", content="hey")
    fake = fake_upstreams.loopback(sc) if wire else fake_upstreams.asgi(sc)
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", proxy.app)
    monkeypatch.setattr(proxy, "UPSTREAM_READ_TIMEOUT", 0.3)
    path, body = FRONT[front]
    st = list(stages)
    async def go():
        got = b""; err = None
        async with asyncio.timeout(10), gateway_client() as c:
            async with c.stream("POST", path, json=body(stream)) as r:
                try:
                    async for ch in r.aiter_raw():
                        got += ch
                        sep = b"\n\n" if front == "anthropic" else b"\n"
                        while st and got.count(sep) >= st[0]:
                            await fake_upstreams.clock.until_waiting(); fake_upstreams.clock.advance(1); st.pop(0)
                except Exception as e:
                    err = repr(e)
        return r, got, err
    r, got, err = asyncio.run(go())
    print(f"\n=== {front} {name} stream={stream} -> {r.status_code} {r.headers.get('content-type')} err={err}\n{got.decode('utf-8','replace')}")
    print("FWD", [q.summary()["body"] for q in fake.requests])
