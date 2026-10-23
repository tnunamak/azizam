"""Shutdown cancels and joins the Home refreshes that outlive the reader that started them."""
import asyncio

import proxy


def test_shutdown_stops_refreshes_still_in_flight():
    async def scenario():
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(60)

        proxy._ENGINE_ROWS_TASK = asyncio.get_running_loop().create_task(slow())
        proxy._HOME_HEALTH_TASK = asyncio.get_running_loop().create_task(slow())
        engine, health = proxy._ENGINE_ROWS_TASK, proxy._HOME_HEALTH_TASK
        await started.wait()
        await asyncio.wait_for(proxy._stop_home_refreshes(), 2)
        return engine, health

    engine, health = asyncio.run(scenario())
    assert engine.cancelled() and health.cancelled()
    assert proxy._ENGINE_ROWS_TASK is None and proxy._HOME_HEALTH_TASK is None


def test_shutdown_with_nothing_in_flight_is_a_no_op():
    proxy._ENGINE_ROWS_TASK = proxy._HOME_HEALTH_TASK = None
    asyncio.run(proxy._stop_home_refreshes())
    assert proxy._ENGINE_ROWS_TASK is None and proxy._HOME_HEALTH_TASK is None
