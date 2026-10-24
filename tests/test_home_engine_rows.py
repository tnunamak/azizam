"""Each engine probe has a deadline, so one slow engine cannot hold up the status rows of the others.

The engine rows are part of the status snapshot, which one background build refreshes (test_status_snapshot.py).
That build runs every engine probe at once. A probe that keeps its connection busy is ended at its deadline and
reads as slow, and a lifecycle check that does not finish reads as unknown. The other rows are still answered.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import status_endpoint
from tests.support.fake_upstream.loopback import LoopbackServer


def _rows() -> list[dict]:
    return [{"key": "chat", "service_id": "chat", "name": "Chat", "status": "up", "live_status": "up",
             "observed_at_s": 100}]


async def _trickling_body(scope, receive, send):
    """A health endpoint that answers 200 and then sends its body a few bytes at a time, slowly."""
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    for _ in range(200):
        await send({"type": "http.response.body", "body": b" ", "more_body": True})
        await asyncio.sleep(0.05)
    await send({"type": "http.response.body", "body": b"{}", "more_body": False})


async def _ready(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"status": "ready"}', "more_body": False})


def test_the_grid_deadline_ends_a_trickling_probe_and_keeps_the_other_rows():
    slow = LoopbackServer(_trickling_body).start()
    fast = LoopbackServer(_ready).start()
    try:
        config = {"backends": {
            "slow": {"name": "Slow", "health_endpoint": f"{slow.base_url}/health", "role": "required"},
            "fast": {"name": "Fast", "health_endpoint": f"{fast.base_url}/health", "role": "required"},
        }}
        started = time.monotonic()
        rows = asyncio.run(status_endpoint._backend_grid(config, deadline_s=0.3))
        elapsed = time.monotonic() - started
    finally:
        slow.stop()
        fast.stop()

    by_key = {row["key"]: row for row in rows}
    assert elapsed < 2.0, "the deadline, not the 10 s body, ends the slow probe"
    assert (by_key["slow"]["status"], by_key["slow"]["last_error"]) == ("degraded", "timeout")
    assert by_key["fast"]["status"] == "up"


def test_the_grid_deadline_reports_a_lifecycle_check_that_does_not_finish_as_unknown(monkeypatch):
    async def never_finishes(target, backends, state=None):  # state: the systemd batch's answer for this engine
        await asyncio.Event().wait()

    monkeypatch.setattr(status_endpoint, "_lifecycle_probe", never_finishes)
    config = {"backends": {"engine": {"name": "Engine", "role": "required", "lifecycle": {"driver": "systemd"}}}}

    (row,) = asyncio.run(status_endpoint._backend_grid(config, deadline_s=0.1))

    assert (row["status"], row["last_error"]) == ("unknown", "status check did not finish")
