"""Skip the long poll gaps of a run, so a test does not wait out real seconds for a fake engine."""
from __future__ import annotations

import asyncio


class NoPollWait:
    """``asyncio`` as the graph round (gateway/engine_protocol/graph_round.py) sees it, with each sleep of a second or more
    skipped. The video workflows declare a 5 s poll gap, so the first look at the engine would wait 5 s of real time;
    the gap does not change what a run does. The declared schedule is pinned in tests/test_node_graph_poll_and_stages.py.
    Use: ``monkeypatch.setattr(graph_round, "asyncio", NoPollWait())``."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(delay=0, result=None):
        return await asyncio.sleep(0 if delay >= 1 else delay, result)
