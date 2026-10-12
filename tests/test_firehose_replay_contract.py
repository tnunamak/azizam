import json
from collections import defaultdict, deque
from types import SimpleNamespace

import pytest

import firehose_metrics as fm


def _activity():
    return {"endpoints": defaultdict(int), "last_seen": 0.0,
            "events": deque(maxlen=2000)}


def _request(**changes):
    row = {"kind": "request", "ts": 950, "name": "fixture-app",
           "path": "/v1/example", "status": 200, "latency_ms": 12.0}
    row.update(changes)
    return row


@pytest.mark.parametrize("bad_line", [
    "{broken-json",
    json.dumps([]),
    json.dumps(None),
    json.dumps(_request(ts="not-a-time")),
    json.dumps(_request(status="not-a-status")),
    json.dumps(_request(latency_ms="not-a-latency")),
])
def test_replay_skips_one_bad_record_and_keeps_later_good_rows(
    tmp_path, monkeypatch, bad_line,
):
    path = tmp_path / "activity.jsonl"
    path.write_text("\n".join([
        json.dumps(_request(ts=899)),  # Outside the 100-second window.
        json.dumps(_request(ts=900)),  # Inclusive cutoff.
        bad_line,
        json.dumps(_request(ts=999, status=503, latency_ms=20.0)),
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(fm, "time", SimpleNamespace(time=lambda: 1000))
    monkeypatch.setattr(fm, "_KEY_ACTIVITY", defaultdict(_activity))
    monkeypatch.setattr(fm, "_RECENT_EVENTS", deque(maxlen=200))
    monkeypatch.setattr(fm, "_WINDOW_STARTED_AT", 1000)

    stats = fm.replay_persisted(window_s=100, path=path)

    assert stats == {"requests_replayed": 2, "events_replayed": 0, "skipped": 1}
    row = fm._KEY_ACTIVITY["fixture-app"]
    assert list(row["events"]) == [(900.0, 200, 12.0), (999.0, 503, 20.0)]
    assert dict(row["endpoints"]) == {"/v1/example": 2}
    assert row["last_seen"] == 999.0
    assert fm.snapshot_window()["window_started_at"] == 900.0
