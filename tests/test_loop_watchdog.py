import asyncio
import threading
import time

from gateway import loop_watchdog


def _blocking_culprit(seconds: float) -> None:
    time.sleep(seconds)


def test_reports_the_stack_that_blocks_the_loop():
    lines: list[str] = []

    async def scenario():
        watchdog = loop_watchdog.LoopWatchdog(
            asyncio.get_running_loop(),
            threading.get_ident(),
            threshold_s=0.2,
            interval_s=0.05,
            sample_s=0.02,
            log=lines.append,
        )
        watchdog.start()
        await asyncio.sleep(0.1)
        _blocking_culprit(0.6)
        await asyncio.sleep(0.1)
        watchdog.stop()

    asyncio.run(scenario())

    assert len(lines) == 1
    report = lines[0]
    assert "event loop blocked" in report
    assert "_blocking_culprit" in report
    blocked = float(report.split("blocked ")[1].split("s ")[0])
    assert 0.5 <= blocked < 1.5


def test_quiet_when_the_loop_keeps_answering():
    lines: list[str] = []

    async def scenario():
        watchdog = loop_watchdog.LoopWatchdog(
            asyncio.get_running_loop(),
            threading.get_ident(),
            threshold_s=0.2,
            interval_s=0.02,
            log=lines.append,
        )
        watchdog.start()
        for _ in range(20):
            await asyncio.sleep(0.02)
        watchdog.stop()

    asyncio.run(scenario())

    assert lines == []


def test_non_positive_threshold_disables():
    async def scenario():
        return loop_watchdog.start(0)

    assert asyncio.run(scenario()) is None
