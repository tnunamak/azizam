import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

import metrics_sampler as sampler
import metrics_store


def test_sampler_recovers_and_retries_before_clean_cancellation(monkeypatch):
    clock = [3580.0]
    ticks = iter([3590.0, 3600.0, 3610.0])

    async def sleep(_delay):
        try:
            clock[0] = next(ticks)
        except StopIteration:
            raise asyncio.CancelledError()

    async def to_thread(fn, *args):
        return fn(*args)

    # A private asyncio facade avoids replacing asyncio.sleep globally.
    monkeypatch.setattr(sampler, "asyncio", SimpleNamespace(
        sleep=sleep, to_thread=to_thread, CancelledError=asyncio.CancelledError,
    ))
    monkeypatch.setattr(sampler, "time", SimpleNamespace(time=lambda: clock[0]))
    state = {
        **sampler._state, "paused_for_disk": False, "last_disk_warn_ts": 0,
        "last_minute_rolled": 59, "last_hour_rolled": 0, "last_prune_day": 0,
    }
    monkeypatch.setattr(sampler, "_state", state)
    disk = Mock(side_effect=[1, 2 * sampler._DISK_PAUSE_BYTES,
                             2 * sampler._DISK_PAUSE_BYTES])
    monkeypatch.setattr(metrics_store, "disk_avail_bytes", disk)
    writer = Mock()
    writer.maintain.side_effect = [False, False, True, True]
    get_writer = Mock(return_value=writer)
    close = Mock()
    fast, slow, fast_async = Mock(), Mock(), AsyncMock()
    monkeypatch.setattr(sampler, "get_writer", get_writer)
    monkeypatch.setattr(sampler, "close_writer", close)
    monkeypatch.setattr(sampler, "_emit_fast", fast)
    monkeypatch.setattr(sampler, "_emit_slow", slow)
    monkeypatch.setattr(sampler, "_emit_fast_async", fast_async)
    config = {"backends": {"fixture": {"name": "Fixture"}}}

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sampler.sampler_loop(lambda: config))

    assert disk.call_count == 3
    assert get_writer.call_count == 2
    assert fast.call_args_list == [call(writer, 3600), call(writer, 3610)]
    assert fast_async.await_args_list == [call(writer, 3600), call(writer, 3610)]
    assert slow.call_args_list == [call(writer, 3600, config)]
    assert writer.maintain.call_args_list == [
        call("minute", 3600), call("hour", 3600),
        call("minute", 3610), call("hour", 3610),
    ]
    assert state["paused_for_disk"] is False
    assert state["last_disk_warn_ts"] == 3590
    assert state["last_minute_rolled"] == 60
    assert state["last_hour_rolled"] == 1
    close.assert_called_once_with()
