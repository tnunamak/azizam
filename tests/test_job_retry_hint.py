# Test design: input class | expected result
# No events / unrelated event types | no hint
# One valid event | its positive integer step
# Several valid events | last valid hint in iteration order
# Zero, negative, bool, float, string, None, missing step | ignored; keep prior valid hint
# None payload / missing type / missing payload | ignored; keep prior valid hint
# Write integer / float / bool | append exactly once with int(seconds), then read it back

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import job_retry_hint


def hint(step):
    return SimpleNamespace(type="retry_after", payload={"phase": "busy", "step": step})


def test_read_empty_or_unrelated_events():
    assert job_retry_hint.read([]) is None
    assert job_retry_hint.read([SimpleNamespace(type="other", payload={"step": 19})]) is None


def test_read_returns_one_valid_hint_and_prefers_last_valid_event():
    assert job_retry_hint.read([hint(7)]) == 7
    assert job_retry_hint.read(iter([hint(30), hint(12), hint(4)])) == 4


@pytest.mark.parametrize(
    "payload",
    [{"step": 0}, {"step": -3}, {"step": True}, {"step": 2.5}, {"step": "5"}, {"step": None}, {}],
    ids=["zero", "negative", "bool", "float", "string", "none", "missing-step"],
)
def test_read_ignores_invalid_steps_without_erasing_earlier_hint(payload):
    invalid = SimpleNamespace(type="retry_after", payload=payload)
    assert job_retry_hint.read([invalid]) is None
    assert job_retry_hint.read([hint(7), invalid]) == 7


def test_read_ignores_missing_event_attributes_or_none_payload():
    invalid = [
        SimpleNamespace(type="retry_after", payload=None),
        SimpleNamespace(payload={"step": 9}),
        SimpleNamespace(type="retry_after"),
    ]
    assert job_retry_hint.read(invalid) is None
    assert job_retry_hint.read([hint(7), *invalid]) == 7


@pytest.mark.parametrize(("seconds", "expected"), [(6, 6), (6.75, 6), (True, 1)])
def test_write_appends_once_and_round_trips(monkeypatch, seconds, expected):
    calls = []
    path = Path("unused-fake-job-store")

    def record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(job_retry_hint.media_jobs, "append_event", record)
    job_retry_hint.write("fake-job", seconds, path=path)

    assert calls == [
        (("fake-job", "retry_after", {"phase": "busy", "step": expected}), {"path": path}),
    ]
    _, event_type, payload = calls[0][0]
    assert type(payload["step"]) is int
    assert job_retry_hint.read([SimpleNamespace(type=event_type, payload=payload)]) == expected
