"""D47 conformance against real, synthetic audio; no inference engines."""

import asyncio
import json
import struct
import subprocess
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.requests import Request

import proxy
from gateway import stt_chunking as stt
from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter, SttRun
from tests.support.access import access_context
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401
from tests.test_stt_chunking import _multipart_body
from tests.test_stt_round4_planner import config


def wav(path, seconds):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * round(seconds * 16000))
    return path


@pytest.fixture
def misleading_audio(tmp_path):
    """Matroska Duration is advisory: mutate it without changing any PCM packet."""
    source = wav(tmp_path / "source.wav", 0.6)
    container = tmp_path / "original.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:a", "pcm_s16le",
                    str(container)], check=True, capture_output=True)
    data = container.read_bytes()
    offset = data.index(b"\x44\x89\x88")

    def make(duration):
        path = tmp_path / f"duration-{duration}.mka"
        if duration is None:
            path.write_bytes(subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(source),
                "-c:a", "flac", "-f", "matroska", "pipe:1"]))
            return path
        element = b"\x44\x89\x88" + struct.pack(">d", duration * 1000)
        path.write_bytes(data[:offset] + element + data[offset + 11:])
        return path

    return make


@pytest.mark.parametrize("metadata_duration", [None, 0.1, 10.0])
def test_container_duration_does_not_refuse_or_shorten_decodable_audio(tmp_path, misleading_audio, metadata_duration):
    async def run():
        source = misleading_audio(metadata_duration)
        # Prove that the fixture's metadata really differs from the decoded samples.
        metadata = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)]))
        observed = metadata["format"].get("duration")
        assert observed is None if metadata_duration is None else float(observed) == metadata_duration
        execution = config(tmp_path, target_seconds=0.25, threshold_seconds=0.3,
                           max_segment_seconds=0.3, search_seconds=0.05,
                           max_duration_seconds=1, max_decoded_bytes=32000)
        sent_durations = []

        async def post(path, _filename):
            with wave.open(str(path)) as audio:
                sent_durations.append(audio.getnframes() / audio.getframerate())
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})

        outcome = await stt.execute_chunking(source_path=source, source_stem="audio", fields=[],
            config=execution, post_file=post, cancellation_waiters=(),
            deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)
        assert outcome.mode == "chunked"
        assert sum(sent_durations) == pytest.approx(0.6)
        assert max(sent_durations) <= 0.3

    asyncio.run(run())


def test_extraction_checks_decoded_piece_even_when_probe_understates_it(tmp_path):
    async def run():
        destination = tmp_path / "piece.wav"
        real_run = stt.run_media_command

        async def wrong_extraction(command, **kwargs):
            if command[-1] == str(destination):
                # Simulate an extractor that wrote 50 ms beyond the requested end.
                wav(destination, 0.35)
                return b"", b""
            return await real_run(command, **kwargs)

        with patch.object(stt, "run_media_command", side_effect=wrong_extraction), patch.object(
            stt, "probe_audio", new=AsyncMock(return_value=stt.ProbedAudio(0.3, 16000, 1))
        ):
            with pytest.raises(stt.SttSetupFailure, match="hard maximum"):
                await stt.extract_segment(tmp_path / "source.wav", destination, start_seconds=0,
                    end_seconds=0.3, config=config(tmp_path, target_seconds=0.25, max_segment_seconds=0.3),
                    cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)

    asyncio.run(run())


@pytest.mark.parametrize("budget", ["max_duration_seconds", "max_decoded_bytes"])
def test_understated_metadata_cannot_bypass_bounded_decode(tmp_path, misleading_audio, budget):
    async def run():
        source = misleading_audio(0.1)
        changes = {budget: 0.5 if budget == "max_duration_seconds" else 16000}
        destination = tmp_path / "bounded.pcm"
        execution = config(tmp_path, **changes)
        with pytest.raises(stt.SttResourceLimitExceeded):
            await stt.decode_planning_pcm(source, destination, config=execution,
                cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)
        limit = min(execution.max_decoded_bytes, int(execution.max_duration_seconds * 32000))
        assert limit < destination.stat().st_size <= limit + 2

    asyncio.run(run())


@pytest.mark.parametrize("width", [0, 0.0001])
def test_off_grid_narrow_search_uses_a_frame_centre_within_hard_maximum(tmp_path, width):
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(b"\0\0" * 16000)
    segments = stt.plan_quietest_frame_segments(duration_seconds=0.8,
        quietest_frame=stt.quietest_frame_reader(pcm), target_seconds=0.253,
        search_seconds=width, max_segment_seconds=0.253, max_segments=10)
    cuts = [end for _, end in segments[:-1]]
    assert all(cut * 100 == pytest.approx(round(cut * 100)) for cut in cuts)
    assert all(end - start <= 0.253 for start, end in segments)


def test_reference_transcription_refuses_known_over_limit_audio(tmp_path, fake_upstreams):
    source = wav(tmp_path / "reference.wav", 0.6)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "words"})

    fake_upstreams.router.add_transport("reference.fk.test:9876", HandlerTransport(handle))
    service = {"host": "reference.fk.test", "port": 9876, "default_model": "test",
               "chunking": {"target_seconds": 0.25, "max_segment_seconds": 0.3,
                            "spool_dir": str(tmp_path / "spool")}}
    with patch.dict(proxy.config["backends"], {"stt": service}), patch.object(proxy, "_voice_store"), \
         patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()):
        with pytest.raises(stt.SttAudioNotSplittable):
            asyncio.run(proxy._reference_transcript(None, {"id": "test"}, source))
    assert seen == [], "a known-over-limit reference must never be posted whole"
    assert list((tmp_path / "spool").iterdir()) == []


@pytest.mark.parametrize("envelope", ["disabled", "timestamps", "streaming", "multiple_files", "setup_failure"])
@pytest.mark.parametrize("policy", ["known", "unknown", "native_offline", "native_streaming"])
def test_final_whole_file_guard_covers_every_run_path(tmp_path, fake_upstreams, envelope, policy):
    async def run():
        paths = [wav(tmp_path / "request.wav", 0.6)]
        if envelope == "multiple_files":
            paths.append(wav(tmp_path / "request-2.wav", 0.7))
        contacted = []

        def handle(request):
            contacted.append(request)
            return httpx.Response(200, json={"text": "whole"})

        fake_upstreams.router.add_transport("guard.fk.test:9876", HandlerTransport(handle))
        execution = config(tmp_path, enabled=envelope != "disabled", target_seconds=0.25,
            threshold_seconds=0.3, search_seconds=0.05,
            max_segment_seconds=None if policy == "unknown" else 0.3,
            native_long_audio_offline=policy == "native_offline",
            native_long_audio_streaming=policy == "native_streaming")
        fields = [("stream", "true")] if envelope == "streaming" else (
            [("timestamp_granularities[]", "word")] if envelope == "timestamps" else [])
        uploads = [("file", SimpleNamespace(file=path.open("rb"), filename=path.name,
                                            content_type="audio/wav")) for path in paths]
        cleanup = stt.SttCleanupOwner()
        attempts = []
        request = SttRun(op="stt.transcribe", execution=execution, fields=fields, multipart_fields=dict(fields),
            uploads=uploads, workspace=tmp_path, cleanup=cleanup, cancellation_waiters=(),
            deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=lambda: None,
            on_attempt_started=lambda count: None, on_outcome=lambda outcome: None,
            on_whole_file_attempt=lambda: attempts.append(1), stream=envelope == "streaming")
        permits_whole = policy == "unknown" or policy == ("native_streaming" if request.stream else "native_offline")
        try:
            with patch.object(stt, "plan_quietest_frame_segments", side_effect=stt.SttSetupFailure("test setup")):
                adapter = ChunkedSttAdapter()
                if permits_whole:
                    await adapter._run("http://guard.fk.test:9876/v1/audio/transcriptions", httpx.Timeout(5), request)
                else:
                    with pytest.raises(stt.SttAudioNotSplittable):
                        await adapter._run("http://guard.fk.test:9876/v1/audio/transcriptions", httpx.Timeout(5), request)
            assert len(contacted) == len(attempts) == int(permits_whole)
        finally:
            await cleanup.aclose()
            for _, upload in uploads:
                upload.file.close()

    asyncio.run(run())


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("result", ["success", "refusal", "timeout", "cancel"])
def test_receipts_count_each_started_attempt_once(tmp_path, fake_upstreams, chunked, result):
    async def run():
        calls = 0
        started = asyncio.Event()

        async def handle(request):
            nonlocal calls
            calls += 1
            await request.aread()
            started.set()
            if result == "timeout":
                raise httpx.ReadTimeout("test timeout")
            if result == "cancel":
                await asyncio.Future()
            return httpx.Response(400 if result == "refusal" else 200, json={"text": "piece"})

        fake_upstreams.router.add_transport("attempts.fk.test:9876", HandlerTransport(handle))
        execution = replace(config(tmp_path, enabled=chunked, target_seconds=0.25,
            threshold_seconds=0.3, max_segment_seconds=0.3, search_seconds=0.05),
            host="http://attempts.fk.test:9876")
        source = wav(tmp_path / "request.wav", 0.6 if chunked else 0.2)
        body, content_type = _multipart_body(content=source.read_bytes())
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Future()

        request = Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                           "headers": [(b"content-type", content_type.encode())]}, receive)
        finalized = []
        with patch.dict(proxy.config["backends"]), patch.object(proxy, "_stt_execution_snapshot", return_value=execution), \
             patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()), \
             patch.object(proxy.receipt_store, "begin"), patch.object(proxy.receipt_store, "finalize",
                side_effect=lambda receipt: finalized.append(receipt) or True):
            proxy.config["backends"].pop("stt-parakeet-gpu", None)
            task = asyncio.create_task(proxy.proxy_stt_request(request, access_context()))
            if result == "cancel":
                await asyncio.wait_for(started.wait(), 10)
                task.cancel()
            if result == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
                response = None
            else:
                response = await asyncio.wait_for(task, 10)
        assert len(finalized) == 1
        assert calls == (3 if chunked and result == "success" else 1)
        assert finalized[0].metadata["stt"]["attempts_started"] == calls
        if result == "cancel":
            assert finalized[0].status == "cancelled"
        else:
            assert response.status_code == {"success": 200, "refusal": 400, "timeout": 504}[result]

    asyncio.run(run())
