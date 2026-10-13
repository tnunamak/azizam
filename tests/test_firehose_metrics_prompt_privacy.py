import json
from pathlib import Path
from unittest.mock import patch

import firehose_metrics


def test_status_renderer_neutral_host_defaults() -> None:
    import firehose_render

    rendered = firehose_render.render_html({})
    assert "Azizam · status" in rendered
    assert "served at <code>Azizam</code>" in rendered
    assert "served at <code>Azizam</code>" in firehose_render._render_banner({})
    assert "github.com/" not in firehose_render._HTML_FOOT_TEMPLATE
    assert firehose_render._render_quick_test().count("https://gateway.example/openai/") == 6


def test_status_renderer_request_host_is_display_only_and_escaped() -> None:
    import firehose_render

    host = '<script>alert("host")</script>'
    rendered = firehose_render.render_html({}, host=host, public_url="https://configured.example")
    assert host not in rendered
    assert firehose_render._esc(host) + " · status" in rendered
    assert "served at <code>" + firehose_render._esc(host) + "</code>" in rendered
    assert "configured.example" not in rendered
    assert "https://gateway.example/openai/" in firehose_render._render_quick_test()


def test_status_renderer_configured_url_fallback() -> None:
    import firehose_render

    for public_url, expected in [
        ("https://gateway.example:8443/prefix?ignored=yes#fragment", "gateway.example:8443"),
        ("http://[::1]:8080/status", "[::1]:8080"),
    ]:
        rendered = firehose_render.render_html({}, host="  ", public_url=public_url)
        assert expected + " · status" in rendered
        assert "served at <code>" + expected + "</code>" in rendered
    assert firehose_render._display_host(" localhost:5000 ") == "localhost:5000"


def test_status_renderer_rejects_invalid_public_url_fallback() -> None:
    import firehose_render

    for public_url in [
        "", "gateway.example", "//gateway.example", "javascript:alert(1)",
        "file:///tmp/status", "https:///missing-host", "https://user:password@gateway.example",
        "https://gateway.example:invalid", "https://gateway.example:65536",
        "https://[broken", "https://gate\nway.example", "https://gateway.example/with space",
        "https://gateway.example\\evil.example",
    ]:
        assert firehose_render._display_host(None, public_url) == "Azizam"


def test_status_renderer_configured_host_is_escaped() -> None:
    import firehose_render

    public_url = 'https://gateway.example<bad>/path'
    rendered = firehose_render.render_html({}, public_url=public_url)
    assert "<bad>" not in rendered
    assert "gateway.example&lt;bad&gt;" in rendered


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
