import asyncio
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

def test_dbg(fake_upstreams, monkeypatch, tmp_path):
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/stream-split-think", model="fk-model"))
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    async def go():
        stages=[1]; got=b""
        async with gateway_client() as c:
            async with c.stream("POST", "/v1/chat/completions", json={"model": "fk-model", "messages": [], "stream": True}) as r:
                async for ch in r.aiter_raw():
                    print("CHUNK", ch); got += ch
                    while stages and got.count(b"\n\n") >= stages[0]:
                        await fake_upstreams.clock.until_waiting(); fake_upstreams.clock.advance(1); stages.pop(0)
    asyncio.run(go())
