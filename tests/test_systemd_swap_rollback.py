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
