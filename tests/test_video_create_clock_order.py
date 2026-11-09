"""A video's ``updated_at`` is never earlier than its ``created_at``.

The queued video's progress is seeded and its ``created_at`` is taken in one submit. Two clock readings made on
either side of a second boundary put ``updated_at`` one second before ``created_at``, and the golden trace check
failed about once in a long suite run. The test steps the clock one whole second on every reading, so any two
readings made in the wrong order differ by a second on every run.
"""
from __future__ import annotations

import itertools
import time

from tests import test_adapter_differential_s7 as s7
from tests.support.adapter_differential_rig import play


def test_vc_updated_at_is_not_before_created_at_when_the_clock_steps_a_second(
        tmp_path, record_testsuite_property, monkeypatch):
    real = time.time
    start = real()
    ticks = itertools.count()
    monkeypatch.setattr(time, "time", lambda: start + next(ticks))
    setup, drive, description = s7.build(s7.ROUTES["vc"], "replay_same")
    play(s7.fixture_name("vc", "replay_same"), tmp_path, record_testsuite_property, setup, drive,
         description=description)
