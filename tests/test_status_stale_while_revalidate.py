"""A status past its TTL is served at once while one background rebuild refreshes it."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import status_endpoint as status


def test_stale_status_is_served_without_waiting_for_the_rebuild():
    async def run():
        clock = [100.0]
        release = asyncio.Event()
        builds = []

        async def build(_config):
            builds.append(clock[0])
            if len(builds) > 1:
                await release.wait()  # the refresh is slow
            return {"build": len(builds)}

        with patch.object(status, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(status, "build_status_payload", build), \
                patch.object(status, "_CACHE", {"ts": 0.0, "data": None}), \
                patch.object(status, "_BUILD_TASK", None):
            assert (await status.get_cached_status({}))["build"] == 1  # cold: waits for the build
            clock[0] += status._CACHE_TTL_S + 1
            stale = await asyncio.wait_for(status.get_cached_status({}), timeout=0.5)
            assert stale["build"] == 1 and stale["cache_age_s"] > status._CACHE_TTL_S
            again = await asyncio.wait_for(status.get_cached_status({}), timeout=0.5)
            assert again["build"] == 1
            assert len(builds) == 2, "one background rebuild, however many requests arrive"
            release.set()
            await asyncio.sleep(0.05)
            assert (await status.get_cached_status({}))["build"] == 2
            clock[0] += status._CACHE_STALE_S + 1  # too old to serve: wait for a fresh build
            release.set()
            assert (await status.get_cached_status({}))["build"] == 3

    asyncio.run(run())
