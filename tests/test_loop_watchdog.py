import asyncio
import threading
import time

from gateway import loop_watchdog


def _blocking_culprit(gate: threading.Event) -> None:
    gate.wait(5)  # watchdog only; the test opens the gate once the stack was sampled


def test_reports_the_stack_that_blocks_the_loop(monkeypatch):
    lines: list[str] = []
    logged = threading.Event()
    sampled = threading.Event()
    samples = []
    real_stack_key = loop_watchdog._stack_key

    def counting_stack_key(frame):
        samples.append(1)
        if len(samples) >= 2:
            sampled.set()
        return real_stack_key(frame)

    def log(line):
        lines.append(line)
        logged.set()

    monkeypatch.setattr(loop_watchdog, "_stack_key", counting_stack_key)

    async def scenario():
        watchdog = loop_watchdog.LoopWatchdog(
            asyncio.get_running_loop(),
            threading.get_ident(),
            threshold_s=0.2,
            interval_s=0.05,
            sample_s=0.02,
            log=log,
        )
        watchdog.start()
        # The loop stays blocked until the watchdog has sampled it twice.
        _blocking_culprit(sampled)
        deadline = time.monotonic() + 5  # watchdog only; the log line is the event
        while not logged.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        await asyncio.to_thread(watchdog.stop)  # joining on the loop would starve its heartbeat

    asyncio.run(scenario())

    assert len(lines) == 1
    report = lines[0]
    assert "event loop blocked" in report
    assert "_blocking_culprit" in report
    assert int(report.split("(")[1].split(" stack samples")[0]) >= 2


def test_quiet_when_the_loop_keeps_answering():
    lines: list[str] = []

    async def scenario():
        # The threshold is far above scheduler jitter; the loop answers in microseconds.
        watchdog = loop_watchdog.LoopWatchdog(
            asyncio.get_running_loop(),
            threading.get_ident(),
            threshold_s=5.0,
            interval_s=0.02,
            log=lines.append,
        )
        watchdog.start()
        for _ in range(20):
            await asyncio.sleep(0.02)
        await asyncio.to_thread(watchdog.stop)  # joining on the loop would starve its heartbeat

    asyncio.run(scenario())

    assert lines == []


def test_non_positive_threshold_disables():
    async def scenario():
        return loop_watchdog.start(0)

    assert asyncio.run(scenario()) is None
