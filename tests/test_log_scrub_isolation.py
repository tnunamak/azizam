"""A value one test registers with the log scrubber is gone for the next test (conftest ``_log_scrub_registry_is_restored``)."""
from __future__ import annotations

from gateway import log_scrub

_ran = {"registered": False}


def test_a_test_registers_a_short_word_through_an_environment_secret(monkeypatch):
    monkeypatch.setenv("ISOLATION_PROBE_KEY", "test")
    assert log_scrub.env_secret("ISOLATION_PROBE_KEY") == "test"
    assert log_scrub.scrub("http://host.test:80") == "http://host.[removed]:80"
    _ran["registered"] = True


def test_the_next_test_scrubs_nothing_that_the_last_one_registered():
    if not _ran["registered"]:
        return  # run alone or out of order: nothing was left to see
    assert "env:ISOLATION_PROBE_KEY" not in log_scrub.sources()
    assert log_scrub.scrub("http://host.test:80") == "http://host.test:80"
