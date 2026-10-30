"""The lifetime lock a gateway holds on its database, whichever launcher started it (owner visibility step 2, design §8.1).

These tests use a temporary database rather than the ``gateway`` fixture, because that fixture holds the lock for
its whole test: a release check here would then see the fixture's own hold.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from gateway import app_binding_cutover, gateway_lifetime

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _apply_holding_the_lock(path):
    """Another process that takes the lifetime lock exclusively, as apply does, until its stdin closes."""
    script = ("import fcntl, sys\nfrom pathlib import Path\nfrom gateway import gateway_lifetime\n"
              "handle = gateway_lifetime.open_lock_file(Path(sys.argv[1]))\n"
              "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\nprint('applying', flush=True)\nsys.stdin.read()\n")
    proc = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, cwd=REPO_ROOT)
    assert proc.stdout.readline().strip() == "applying", "the other process did not take the exclusive lock"
    return proc


async def _until(condition, what: str) -> None:
    """Let the event loop run until ``condition`` holds, for up to ten seconds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_start_up_cancelled_while_it_waits_for_apply_does_not_leave_the_lock_held(database, start_up_skipped, monkeypatch):
    """The worker thread keeps waiting after its await is cancelled, so a hold it takes later must be released too."""
    import proxy

    waiting = threading.Event()
    acquired = threading.Event()
    take_shared = gateway_lifetime.hold

    def hold_and_mark(path):
        waiting.set()
        take_shared(path)
        acquired.set()

    monkeypatch.setattr(gateway_lifetime, "hold", hold_and_mark)
    apply = _apply_holding_the_lock(database)

    async def cancel_start_up_while_apply_holds_the_lock():
        async def start_up():
            async with proxy.lifespan(proxy.app):
                pass

        task = asyncio.create_task(start_up())
        await _until(waiting.is_set, "the start-up to wait for the lock")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        apply.stdin.close()  # apply finishes and lets go
        await _until(acquired.is_set, "the cancelled start-up's hold to land")
        await _until(lambda: not _held(database), "the cancelled start-up's hold to be released")

    try:
        asyncio.run(cancel_start_up_while_apply_holds_the_lock())
    finally:
        apply.stdin.close()
        apply.wait(timeout=30)


def test_a_second_lifespan_on_the_same_database_leaves_the_first_one_holding_the_lock(database, start_up_skipped):
    import proxy

    async def run_nested():
        async with proxy.lifespan(proxy.app):
            async with proxy.lifespan(proxy.app):
                pass
            return _held(database)

    assert asyncio.run(run_nested()) is True
    assert not _held(database)
