import asyncio, httpx
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa
import gateway.backends.llm_execution as le

def test_dbg(fake_upstreams, monkeypatch, tmp_path):
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/stream-split-think", model="fk-model"))
    async def go():
        async with httpx.AsyncClient() as c:
            req = c.build_request("POST", fake.base_url + "/v1/chat/completions", json={"model": "fk-model", "stream": True})
            r = await c.send(req, stream=True)
            n = 0
            async for ch in le.iter_llm_response_bytes(c, r):
                print("CHUNK", ch); n += ch.count(b"\n\n")
                if n >= 1 and fake_upstreams.clock.waiting:
                    fake_upstreams.clock.advance(1)
    asyncio.run(go())
