import asyncio, cProfile, pstats, io, os
import httpx, pytest
import proxy
from tests.test_event_loop_guard import gateway  # noqa
from gateway import media_jobs

@pytest.mark.parametrize("path", [os.environ.get("PROF_PATH", "/me/api/home")])
def test_prof(gateway, path):
    async def run():
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=gateway["cookies"]) as c:
            for _ in range(3): await c.get(path)
            pr = cProfile.Profile(); pr.enable()
            for _ in range(20): r = await c.get(path)
            pr.disable()
            return pr
    pr = asyncio.run(run())
    out = io.StringIO(); pstats.Stats(pr, stream=out).sort_stats("cumulative").print_stats("checkout", 40)
    assert False, "\n" + out.getvalue()
