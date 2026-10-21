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
    json.dumps(_request(ts=1301)),
    json.dumps(_request(ts=float("nan"))),
    json.dumps(_request(ts=float("inf"))),
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


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), 1301])
def test_record_event_rejects_invalid_timestamp_before_memory_mutation(
    tmp_path, monkeypatch, timestamp,
):
    now = 1000
    monkeypatch.setattr(fm.time, "time", lambda: now)
    monkeypatch.setattr(fm, "_RECENT_EVENTS", deque(maxlen=200))
    monkeypatch.setattr(fm, "_DEFAULT_CACHE_PATH", tmp_path / "activity.jsonl")

    fm.record_event("test", "must be rejected", ts=timestamp)

    assert fm.snapshot_recent_events() == []
    fm.flush_persisted()
    assert not (tmp_path / "activity.jsonl").exists()


def test_compaction_keeps_only_finite_in_window_timestamps(tmp_path, monkeypatch):
    now = 1000
    path = tmp_path / "activity.jsonl"
    rows = [
        _request(ts=899),
        _request(ts=900),
        _request(ts=1300),
        _request(ts=1301),
        _request(ts=float("nan")),
        _request(ts=float("inf")),
    ]
    path.write_text("".join(json.dumps(row, allow_nan=True) + "\n" for row in rows), encoding="utf-8")
    monkeypatch.setattr(fm.time, "time", lambda: now)

    assert fm.compact_log(window_s=100, path=path) == 2
    assert [json.loads(line)["ts"] for line in path.read_text(encoding="utf-8").splitlines()] == [900, 1300]
    assert path.stat().st_mode & 0o777 == 0o600


def test_compaction_reports_only_failure_class(tmp_path, monkeypatch, capsys):
    path = tmp_path / "activity.jsonl"
    path.write_text(json.dumps(_request(ts=999)) + "\n", encoding="utf-8")

    def fail_replace(*_args):
        raise PermissionError("private path must not be logged")

    monkeypatch.setattr(fm.os, "replace", fail_replace)

    assert fm.compact_log(path=path) is None
    output = capsys.readouterr().out
    assert "PermissionError" in output
    assert "private path" not in output


def test_append_survives_compaction_replace_race(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "private" / "activity.jsonl"
    monkeypatch.setattr(fm.time, "time", lambda: 1000)
    fm._append_record(path, _request(ts=999))
    entered_replace = threading.Event()
    continue_replace = threading.Event()
    real_replace = fm.os.replace

    def paused_replace(source, target):
        entered_replace.set()
        assert continue_replace.wait(5)
        real_replace(source, target)

    monkeypatch.setattr(fm.os, "replace", paused_replace)
    compactor = threading.Thread(target=fm.compact_log, kwargs={"path": path})
    compactor.start()
    assert entered_replace.wait(5)

    monkeypatch.setattr(fm, "_DEFAULT_CACHE_PATH", path)
    fm.record_request("racing-app", "/v1/race", 200, 1)
    continue_replace.set()
    compactor.join(5)
    fm.flush_persisted()

    assert not compactor.is_alive()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row.get("name") for row in rows if row["kind"] == "request"] == ["fixture-app", "racing-app"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
