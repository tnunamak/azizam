"""A failed systemd swap must leave the unit on its last working activation.

Before this fix, ``SystemdDriver.swap`` deleted the managed drop-in after a
failed restart or health check and did not restart the unit. The previous
activation (the last working model) was lost, and the unit stayed down or on
the failed one until the next swap. These tests drive the real driver against
the fake engine in tests/fake_systemd_engine.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import lifecycle_drivers as ld  # noqa: E402
from tests.fake_systemd_engine import (  # noqa: E402
    MODEL,
    FakeEngine,
    dropin_text,
    fake_svc,
    install_fake_engine,
)


@pytest.fixture
def engine(tmp_path, monkeypatch) -> FakeEngine:
    return install_fake_engine(monkeypatch, tmp_path, profile="p-a")


def _swap_to_p_b(svc: dict | None = None) -> bool:
    driver = ld.SystemdDriver()
    svc = svc or fake_svc()
    activation = driver.build_activation(svc, model=MODEL, profile="p-b")
    ctx = ld.SwapContext(loaded_model=MODEL, target_model=MODEL, target_profile="p-b")
    return asyncio.run(asyncio.wait_for(driver.swap(svc, activation, ctx=ctx), 10))


def test_successful_swap_keeps_the_new_dropin(engine) -> None:
    assert _swap_to_p_b() is True
    assert engine.dropin.read_text() == dropin_text("p-b")
    assert engine.events == [("daemon-reload", 0), ("restart", "p-b", 0)]


def test_failed_restart_restores_the_previous_dropin_and_restarts_on_it(engine) -> None:
    engine.restart_codes = [1]  # the new activation fails to start; the old one starts
    assert _swap_to_p_b() is False
    assert engine.dropin.read_text() == dropin_text("p-a")
    assert engine.events == [
        ("daemon-reload", 0),
        ("restart", "down", 1),
        ("daemon-reload", 0),
        ("restart", "p-a", 0),
    ]
    assert engine.loaded == "p-a"


def test_failed_health_check_restores_the_previous_dropin_and_restarts_on_it(engine) -> None:
    engine.unhealthy_profiles = {"p-b"}
    assert _swap_to_p_b(fake_svc(health_timeout_s=0.1)) is False
    assert engine.dropin.read_text() == dropin_text("p-a")
    assert engine.restarts() == ["p-b", "p-a"]
    assert engine.loaded == "p-a" and engine.healthy()


def test_failed_swap_without_a_previous_dropin_falls_back_to_the_base_unit(engine) -> None:
    engine.dropin.unlink()
    engine.loaded = "base-unit"
    engine.restart_codes = [1]
    assert _swap_to_p_b() is False
    assert not engine.dropin.exists()
    assert engine.restarts() == ["down", "base-unit"]
    assert engine.loaded == "base-unit"


def test_failed_daemon_reload_restores_the_dropin_without_a_restart(engine) -> None:
    engine.reload_codes = [1]
    assert _swap_to_p_b() is False
    assert engine.dropin.read_text() == dropin_text("p-a")
    assert engine.restarts() == [], "the running engine was never touched"
    assert engine.events == [("daemon-reload", 1), ("daemon-reload", 0)]


# ---------------------------------------------------------------------------
# Cancellation: once the drop-in changed, the swap finishes or rolls back
# ---------------------------------------------------------------------------


def _start_swap_to_p_b(svc: dict | None = None) -> asyncio.Task:
    driver = ld.SystemdDriver()
    svc = svc or fake_svc()
    activation = driver.build_activation(svc, model=MODEL, profile="p-b")
    ctx = ld.SwapContext(loaded_model=MODEL, target_model=MODEL, target_profile="p-b")
    return asyncio.create_task(driver.swap(svc, activation, ctx=ctx))


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _assert_runs(engine: FakeEngine, profile: str) -> None:
    """The drop-in on disk is the activation the healthy engine runs."""
    assert engine.dropin.read_text() == dropin_text(profile)
    assert engine.loaded == profile
    assert engine.healthy()


def _cancel_requester(engine: FakeEngine, gate_name: str) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        setattr(engine, gate_name, gate)
        swap = _start_swap_to_p_b()
        await _settle()
        assert engine.dropin.read_text() == dropin_text("p-b"), "the drop-in has changed"
        swap.cancel()  # the requester goes away mid-swap
        await _settle()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(swap, 5)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_cancel_during_daemon_reload_still_finishes_the_swap(engine) -> None:
    _cancel_requester(engine, "reload_gate")
    _assert_runs(engine, "p-b")
    assert engine.events == [("daemon-reload", 0), ("restart", "p-b", 0)]


def test_cancel_during_restart_still_finishes_the_swap(engine) -> None:
    _cancel_requester(engine, "restart_gate")
    _assert_runs(engine, "p-b")
    assert engine.restarts() == ["p-b"]


def test_cancel_during_health_polling_still_rolls_back_a_failed_swap(engine) -> None:
    engine.unhealthy_profiles = {"p-b"}

    async def scenario() -> None:
        swap = _start_swap_to_p_b(fake_svc(health_timeout_s=0.3))
        while engine.restarts() != ["p-b"]:
            await asyncio.sleep(0.01)
        swap.cancel()  # the requester goes away while the new engine is unhealthy
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(swap, 5)

    asyncio.run(asyncio.wait_for(scenario(), 10))
    _assert_runs(engine, "p-a")
    assert engine.restarts() == ["p-b", "p-a"]


# ---------------------------------------------------------------------------
# Writes are atomic, and a failed rollback is reported as such
# ---------------------------------------------------------------------------


class _TornWriter:
    """A file whose write stores half the text, then fails (disk full)."""

    def __init__(self, handle) -> None:
        self._handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self._handle.close()
        return False

    def write(self, text: str) -> int:
        self._handle.write(text[: len(text) // 2])
        self._handle.flush()
        raise OSError(28, "No space left on device")

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


def test_a_torn_write_leaves_the_previous_dropin_bytes_intact(engine, monkeypatch) -> None:
    before = engine.dropin.read_bytes()
    real_fdopen = ld.os.fdopen
    monkeypatch.setattr(ld.os, "fdopen", lambda *a, **k: _TornWriter(real_fdopen(*a, **k)))
    assert _swap_to_p_b() is False
    assert engine.dropin.read_bytes() == before
    assert [path.name for path in engine.dropin.parent.iterdir()] == [engine.dropin.name], "no temp file left"
    assert engine.events == [], "nothing was reloaded or restarted"


def test_a_failed_restore_write_is_reported_as_a_recovery_failure(engine, monkeypatch, capsys) -> None:
    engine.restart_codes = [1]
    real_replace = ld._replace_file_atomically
    writes: list[str] = []

    def fail_second_write(path, text):
        writes.append(text)
        if len(writes) == 2:
            raise OSError(28, "No space left on device")
        real_replace(path, text)

    monkeypatch.setattr(ld, "_replace_file_atomically", fail_second_write)
    with pytest.raises(ld.SwapRecoveryFailed) as caught:
        _swap_to_p_b()
    assert "restore" in str(caught.value)
    assert "recovery failed" in capsys.readouterr().out


def test_a_failed_rollback_restart_is_reported_as_a_recovery_failure(engine) -> None:
    engine.restart_codes = [1, 1]  # neither the new nor the previous activation starts
    with pytest.raises(ld.SwapRecoveryFailed):
        _swap_to_p_b()
    assert engine.dropin.read_text() == dropin_text("p-a"), "the previous drop-in is back on disk"
    assert engine.loaded == "down"


def test_an_unhealthy_previous_activation_is_reported_as_a_recovery_failure(engine) -> None:
    engine.unhealthy_profiles = {"p-a", "p-b"}
    with pytest.raises(ld.SwapRecoveryFailed):
        _swap_to_p_b(fake_svc(health_timeout_s=0.1))
    assert engine.dropin.read_text() == dropin_text("p-a")
    assert engine.restarts() == ["p-b", "p-a"]


# ---------------------------------------------------------------------------
# Durability is reported, and every systemctl call is bounded
# ---------------------------------------------------------------------------


def test_a_failed_directory_fsync_is_a_logged_durability_warning(engine, monkeypatch, capsys) -> None:
    real_open = ld.os.open

    def open_without_directory_sync(path, flags, *args, **kwargs):
        if flags & getattr(ld.os, "O_DIRECTORY", 0):
            raise OSError(5, "Input/output error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ld.os, "open", open_without_directory_sync)
    assert _swap_to_p_b() is True, "the drop-in is in place; durability is best effort"
    _assert_runs(engine, "p-b")
    out = capsys.readouterr().out
    assert "directory fsync failed" in out and "power loss" in out


def test_a_hung_systemctl_call_times_out_and_the_swap_ends(engine, monkeypatch) -> None:
    """A restart that never returns must not hold the swap lock forever: the
    call times out, the rollback's restart times out too, and the swap ends
    with SwapRecoveryFailed and the previous drop-in on disk."""
    monkeypatch.setattr(ld.SystemdDriver, "_SYSTEMCTL_JOB_TIMEOUT_S", 0.1)
    engine.restart_gate = asyncio.Event()  # never set: every restart hangs
    with pytest.raises(ld.SwapRecoveryFailed):
        _swap_to_p_b()
    assert engine.dropin.read_text() == dropin_text("p-a")


def test_a_hung_systemctl_process_is_killed(monkeypatch) -> None:
    class HungProc:
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self) -> None:
            self.killed = True

    proc = HungProc()

    async def spawn(*_cmd, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ld.SystemdDriver, "_SYSTEMCTL_JOB_TIMEOUT_S", 0.1)
    ok = asyncio.run(asyncio.wait_for(ld.SystemdDriver()._run_systemctl(["systemctl", "--user", "daemon-reload"]), 5))
    assert ok is False
    assert proc.killed
