"""Fresh-process STT configuration/dispatch and status grid/sampler contracts.

The direct sender is a scoped test adapter, not the out-of-scope public route.
All real socket calls remain forbidden, including during application imports.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.fake_upstream import fake_upstreams  # noqa: F401

ORACLE = Path(__file__).parent / "support" / "fresh_gateway_oracle.py"
PATHS = ("stt-direct", "stt-chunked", "grid", "history")
STATES = ("absent", "disabled", "partial", "enabled")


def _run(tmp_path, path, state):
    done = subprocess.run(
        [sys.executable, str(ORACLE), str(tmp_path), "--path", path, "--state", state],
        capture_output=True, text=True, cwd=ORACLE.parents[2], timeout=300, check=False)
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("state", STATES)
def test_scoped_engine_path_has_no_egress_without_an_enabled_complete_row(tmp_path, path, state):
    report = _run(tmp_path, path, state)
    assert report["preloaded_at_guard"] == []
    assert report["attempts"] == [], report["attempts"]
    assert report["wrong_origins"] == [], report["wrong_origins"]
    assert report["leaked_tasks"] == 0
    assert report["sampler_joined"] is True
    assert report["violations"] == [], report["violations"]
    assert report["problems"] == [], report["problems"]

    if state != "enabled":
        assert report["requests"] == [], report["requests"]
        assert report["dispatches"] == [], report["dispatches"]
        assert report["writer_calls"] == 0
        assert report["history"] == []
        if path.startswith("stt-"):
            assert report["refused_at"] in {"config", "sender"}  # "config": selection gate or address reader
        elif state == "partial":
            assert [(row["key"], row["status"], row["live_status"]) for row in report["rows"]] == [
                ("stt", "not_configured", "not_configured")]
        else:
            assert report["rows"] == []
    else:
        count = {"stt-direct": 1, "stt-chunked": 3, "grid": 1, "history": 3}[path]
        endpoint = "/v1/audio/transcriptions" if path.startswith("stt-") else "/health"
        method = "POST" if path.startswith("stt-") else "GET"
        assert [(r["method"], r["path"]) for r in report["requests"]] == [(method, endpoint)] * count
        assert report["dispatches"] == ["http://127.0.0.1:18991" + endpoint] * count
        if path == "stt-direct":
            assert report["mode"] == "direct" and report["response_status"] == 200
            assert report["text"] == "heard"
            assert report["requests"][0]["filenames"] == ["source.wav"]
        elif path == "stt-chunked":
            assert report["mode"] == "chunked" and report["chunk_attempts"] == 3
            assert report["text"] == "heard heard heard"
            assert [r["filenames"] for r in report["requests"]] == [
                ["source.chunk000.wav"], ["source.chunk001.wav"], ["source.chunk002.wav"]]
        else:
            assert [(row["key"], row["status"], row["live_status"]) for row in report["rows"]] == [
                ("stt", "up", "up")]
            if path == "history":
                assert report["writer_calls"] == 2
                up = [row for row in report["history"] if row[1] == "service_up"]
                assert len(up) == 2 and all(row[3] == 1.0 for row in up)
                assert all(json.loads(row[2])["backend"] == "stt" for row in report["history"])
            else:
                assert report["writer_calls"] == 0 and report["history"] == []

    if path == "history":
        assert report["ticks"] == [[1000.0, 60.0], [1060.0, 60.0]]


def test_voice_reference_transcription_does_not_dial_a_disabled_stt_row(tmp_path, fake_upstreams):
    """The real reference-transcript path: a disabled STT row is refused before any request is sent."""
    import asyncio
    from unittest.mock import patch

    import httpx

    import proxy
    from gateway import custom_voices
    from tests.support.fake_upstream import HandlerTransport

    source = tmp_path / "reference.wav"
    source.write_bytes(b"fixture audio")
    sent = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={"text": "words"})

    fake_upstreams.router.add_transport("disabled-stt.fk.test:9876", HandlerTransport(handler))
    service = {"host": "disabled-stt.fk.test", "port": 9876, "enabled": False, "default_model": "test"}
    with patch.dict(proxy.config["backends"], {"stt": service}), patch.object(proxy, "_voice_store"):
        with pytest.raises(custom_voices.VoiceError) as caught:
            asyncio.run(proxy._reference_transcript(None, {"id": "test"}, source))
    assert caught.value.status == 503
    assert sent == []
