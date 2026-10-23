"""The lifetime lock a gateway holds on its database, whichever launcher started it (owner visibility step 2, design §8.1).

These tests use a temporary database rather than the ``gateway`` fixture, because that fixture holds the lock for
its whole test: a release check here would then see the fixture's own hold.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from gateway import app_binding_cutover, gateway_lifetime


@pytest.fixture
def database(tmp_path, monkeypatch):
    import proxy

    path = tmp_path / "admin.db"
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    return path


def _held(path) -> bool:
    """True when apply's exclusive attempt is refused, which is what a running gateway causes."""
    try:
        app_binding_cutover._hold_lifetime_lock_exclusively(path).close()
    except app_binding_cutover.CutoverError:
        return True
    return False


@pytest.fixture
def start_up_skipped(monkeypatch):
    """The lifespan's store start-up and background tasks are not under test here; only its lock is."""
    import proxy

    @asynccontextmanager
    async def nothing_to_start(app):
        yield

    monkeypatch.setattr(proxy, "_serve", nothing_to_start)


def test_the_lock_is_re_entrant_and_frees_only_when_its_last_holder_releases(tmp_path):
    path = tmp_path / "admin.db"

    gateway_lifetime.hold(path)
    gateway_lifetime.hold(path)
    gateway_lifetime.release(path)
    assert _held(path)

    gateway_lifetime.release(path)
    assert not _held(path)


def test_the_app_lifespan_holds_the_lock_while_it_runs_and_releases_it_at_shutdown(database, start_up_skipped):
    import proxy

    async def run_once():
        async with proxy.lifespan(proxy.app):
            return _held(database)

    assert asyncio.run(run_once()) is True
    assert not _held(database)


def test_a_second_lifespan_on_the_same_database_leaves_the_first_one_holding_the_lock(database, start_up_skipped):
    import proxy

    async def run_nested():
        async with proxy.lifespan(proxy.app):
            async with proxy.lifespan(proxy.app):
                pass
            return _held(database)

    assert asyncio.run(run_nested()) is True
    assert not _held(database)
