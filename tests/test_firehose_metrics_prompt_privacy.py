import json
from pathlib import Path
from unittest.mock import patch

import firehose_metrics


def test_video_submit_metadata_does_not_store_prompt_text(tmp_path: Path) -> None:
    secret = "SECRET firehose video prompt should not persist"
    log_path = tmp_path / "key_activity.jsonl"

    with (
        patch.object(firehose_metrics, "_VIDEO_META", {}),
        patch.object(firehose_metrics, "_DEFAULT_CACHE_PATH", log_path),
    ):
        firehose_metrics.capture_video_submit(
            "pid-1",
            alias="ltx-2.5",
            workflow="workflow",
            size="960x512",
            seconds=8,
            key_name="Robin app",
            prompt_text=secret,
        )

        snapshot = firehose_metrics.snapshot_video_meta()

    assert "prompt_truncated" not in snapshot["pid-1"]
    assert secret not in json.dumps(snapshot)
    assert not log_path.exists() or secret not in log_path.read_text(encoding="utf-8")


def test_record_event_drops_prompt_shaped_extra_fields(tmp_path: Path) -> None:
    secret = "SECRET event prompt should not persist"
    log_path = tmp_path / "key_activity.jsonl"

    with (
        patch.object(firehose_metrics, "_RECENT_EVENTS", firehose_metrics.deque(maxlen=200)),
        patch.object(firehose_metrics, "_DEFAULT_CACHE_PATH", log_path),
    ):
        firehose_metrics.record_event(
            "image_route_receipt",
            "image request",
            prompt_preview=secret,
            prompt=secret,
            model="default",
        )

        events = firehose_metrics.snapshot_recent_events()

    assert events == [{"ts": events[0]["ts"], "type": "image_route_receipt", "summary": "image request", "model": "default"}]
    assert secret not in json.dumps(events)
    assert secret not in log_path.read_text(encoding="utf-8")


def test_replayed_events_drop_prompt_shaped_extra_fields(tmp_path: Path) -> None:
    secret = "SECRET replayed prompt should not resurface"
    log_path = tmp_path / "key_activity.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "kind": "event",
                "ts": 1_800_000_000,
                "type": "image_route_receipt",
                "summary": "image request",
                "prompt_preview": secret,
                "model": "default",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with (
        patch.object(firehose_metrics, "_RECENT_EVENTS", firehose_metrics.deque(maxlen=200)),
        patch.object(firehose_metrics.time, "time", return_value=1_800_000_001),
    ):
        firehose_metrics.replay_persisted(path=log_path)
        events = firehose_metrics.snapshot_recent_events()

    assert events == [{"ts": 1_800_000_000, "type": "image_route_receipt", "summary": "image request", "model": "default"}]
    assert secret not in json.dumps(events)
