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
from gateway import custom_voices, stt_chunking as stt
from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter, SttRun
from tests.support.access import access_context
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401
from tests.test_stt_chunking import _multipart_body
from tests.test_stt_round4_planner import config


def wav(path, seconds, rate=16000, channels=1):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * round(seconds * rate) * channels)
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


def test_missing_duration_does_not_need_an_unbounded_packet_listing(tmp_path):
    async def run():
        source = wav(tmp_path / "input.wav", 0.6)
        container = tmp_path / "recording.webm"
        container.write_bytes(subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(source),
            "-c:a", "libopus", "-f", "webm", "pipe:1"]))
        metadata = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "json", str(container)])
        assert "duration" not in json.loads(metadata)["format"]
        post = AsyncMock(return_value=stt.UpstreamResponse(200, b'{"text":"piece"}', {}))
        outcome = await stt.execute_chunking(source_path=container, source_stem="audio", fields=[],
            config=config(tmp_path, target_seconds=0.25, threshold_seconds=0.3,
                          max_segment_seconds=0.3, search_seconds=0.05, max_media_output_bytes=256),
            post_file=post, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10,
            on_cancel=None)
        assert outcome.mode == "chunked"
        assert post.await_count > 1

    asyncio.run(run())


@pytest.mark.parametrize("channels", [1, 2])
def test_whole_file_guard_decodes_only_the_known_maximum_plus_one_frame(tmp_path, channels):
    async def run():
        source = wav(tmp_path / "long.wav", 3, 48000, channels)
        observed = []
        decoder = stt.decode_planning_pcm

        async def decode(*args, **kwargs):
            await decoder(*args, **kwargs)
            observed.append(args[1].stat().st_size)

        with patch.object(stt, "decode_planning_pcm", side_effect=decode):
            with pytest.raises(stt.SttAudioNotSplittable):
                await stt.guard_uncut_uploads([source], config=config(tmp_path, target_seconds=0.25,
                    max_segment_seconds=0.3), cancellation_waiters=(),
                    deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)
        assert observed and 0 < observed[0] <= (round(0.3 * 48000) + 1) * channels * 2
        assert not list(tmp_path.glob("uncut-*.pcm"))

    asyncio.run(run())


@pytest.mark.parametrize("rate,maximum", [(22050, 0.31), (11025, 0.3)])
def test_fractional_source_sample_limits_remain_cuttable(tmp_path, rate, maximum):
    async def run():
        source = wav(tmp_path / "input.wav", 0.9, rate)
        sent = []

        async def post(path, _filename):
            with wave.open(str(path)) as audio:
                sent.append(audio.getnframes() / audio.getframerate())
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})

        outcome = await stt.execute_chunking(source_path=source, source_stem="audio", fields=[],
            config=config(tmp_path, target_seconds=maximum, threshold_seconds=0.4,
                          max_segment_seconds=maximum, search_seconds=0), post_file=post,
            cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)
        assert outcome.mode == "chunked"
        assert sent and max(sent) <= maximum

    asyncio.run(run())


@pytest.mark.parametrize("rate,extra_samples", [(16000, 800), (48000, 1), (48000, 0)])
def test_extraction_checks_decoded_piece_even_when_probe_understates_it(tmp_path, rate, extra_samples):
    async def run():
        destination = tmp_path / "piece.wav"
        real_run = stt.run_media_command

        async def wrong_extraction(command, **kwargs):
            if command[-1] == str(destination):
                # Include a one-source-sample overrun and an exact-boundary control.
                wav(destination, 0.3 + extra_samples / rate, rate)
                return b"", b""
            return await real_run(command, **kwargs)

        with patch.object(stt, "run_media_command", side_effect=wrong_extraction), patch.object(
            stt, "probe_audio", new=AsyncMock(return_value=stt.ProbedAudio(0.3, 16000, 1))
        ):
            async def extract():
                await stt.extract_segment(tmp_path / "source.wav", destination, start_seconds=0,
                    end_seconds=0.3, source_format=(rate, 1), config=config(tmp_path, target_seconds=0.25, max_segment_seconds=0.3),
                    cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)
            if extra_samples:
                with pytest.raises(stt.SttSetupFailure, match="hard maximum"):
                    await extract()
            else:
                await extract()

    asyncio.run(run())


def test_on_grid_hard_maximum_is_not_refused_by_float_subtraction(tmp_path):
    async def run():
        source = wav(tmp_path / "input.wav", 30)
        sent = []

        async def post(path, _filename):
            with wave.open(str(path)) as audio:
                sent.append(audio.getnframes() / audio.getframerate())
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})

        outcome = await stt.execute_chunking(source_path=source, source_stem="audio", fields=[],
            config=config(tmp_path, target_seconds=7.06, threshold_seconds=8,
                          max_segment_seconds=7.06, search_seconds=0), post_file=post,
            cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time() + 10, on_cancel=None)
        assert outcome.mode == "chunked"
        assert outcome.attempts_started == len(sent) == 5
        assert max(sent) <= 7.06

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
@pytest.mark.parametrize("target", [0.253, 0.353])
def test_off_grid_narrow_search_uses_a_frame_centre_within_hard_maximum(tmp_path, width, target):
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(b"\0\0" * 16000)
    segments = stt.plan_quietest_frame_segments(duration_seconds=0.8,
        quietest_frame=stt.quietest_frame_reader(pcm), target_seconds=target,
        search_seconds=width, max_segment_seconds=target, max_segments=10)
    cuts = [end for _, end in segments[:-1]]
    assert all(cut * 100 == pytest.approx(round(cut * 100)) for cut in cuts)
    assert all(end - start <= target for start, end in segments)
    # A single off-grid cut must also inspect a frame, independently of drift
    # or floating-point failure at later tight-maximum cuts.
    simple = stt.plan_quietest_frame_segments(duration_seconds=2 * target,
        quietest_frame=stt.quietest_frame_reader(pcm), target_seconds=target,
        search_seconds=width, max_segment_seconds=target + 0.05, max_segments=10)
    assert simple[0][1] == round(target * 100) / 100


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
    receipt = proxy.RouteReceipt(path="/v1/audio/speech", protocol="openai.audio", intent="speech.synthesis")
    with patch.dict(proxy.config["backends"], {"stt": service}), patch.object(proxy, "_voice_store"), \
         patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()):
        with pytest.raises(custom_voices.VoiceError, match="STT service limits"):
            asyncio.run(proxy._reference_transcript(None, {"id": "test"}, source, receipt=receipt))
    assert seen == [], "a known-over-limit reference must never be posted whole"
    assert receipt.metadata["stt"]["attempts_started"] == 0
    assert list((tmp_path / "spool").iterdir()) == []


@pytest.mark.parametrize("result", ["success", "refusal", "timeout", "cancel"])
def test_internal_reference_attempt_is_counted_once(tmp_path, fake_upstreams, result):
    async def run():
        source = wav(tmp_path / "reference.wav", 0.2)
        entered = asyncio.Event()
        calls = []

        async def handle(request):
            calls.append(request)
            await request.aread()
            entered.set()
            if result == "timeout":
                raise httpx.ReadTimeout("test timeout")
            if result == "cancel":
                await asyncio.Future()
            return httpx.Response(400 if result == "refusal" else 200, json={"text": "words"})

        fake_upstreams.router.add_transport("reference.fk.test:9876", HandlerTransport(handle))
        service = {"host": "reference.fk.test", "port": 9876, "default_model": "test",
                   "chunking": {"spool_dir": str(tmp_path / "spool")}}
        receipt = proxy.RouteReceipt(path="/v1/audio/speech", protocol="openai.audio", intent="speech.synthesis")
        with patch.dict(proxy.config["backends"], {"stt": service}), patch.object(proxy, "_voice_store"), \
             patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()):
            task = asyncio.create_task(proxy._reference_transcript(None, {"id": "test"}, source, receipt=receipt))
            if result == "cancel":
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif result in {"refusal", "timeout"}:
                with pytest.raises(custom_voices.VoiceError):
                    await task
            else:
                assert await task == "words"
        assert len(calls) == receipt.metadata["stt"]["attempts_started"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("envelope", ["disabled", "timestamps", "streaming", "multiple_files", "setup_failure", "sample_boundary"])
@pytest.mark.parametrize("policy", ["known", "unknown", "native_offline", "native_streaming"])
def test_final_whole_file_guard_covers_every_run_path(tmp_path, fake_upstreams, envelope, policy):
    async def run():
        paths = [wav(tmp_path / "request.wav", 0.3 + 1 / 48000, 48000) if envelope == "sample_boundary"
                 else wav(tmp_path / "request.wav", 0.6)]
        if envelope == "multiple_files":
            paths.append(wav(tmp_path / "request-2.wav", 0.7))
        contacted = []

        def handle(request):
            contacted.append(request)
            return httpx.Response(200, json={"text": "whole"})

        fake_upstreams.router.add_transport("guard.fk.test:9876", HandlerTransport(handle))
        execution = config(tmp_path, enabled=envelope not in {"disabled", "sample_boundary"}, target_seconds=0.25,
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


@pytest.mark.parametrize("seconds,threshold,reason", [(0.2, 0.3, "below_threshold"), (0.28, 0.26, "single_segment_plan")])
def test_passthrough_reuses_verified_decode(tmp_path, fake_upstreams, seconds, threshold, reason):
    async def run():
        source = wav(tmp_path / "short.wav", seconds)
        fake_upstreams.router.add_transport("short.fk.test:9876", HandlerTransport(
            lambda request: httpx.Response(200, json={"text": "whole"})))
        cleanup = stt.SttCleanupOwner()
        outcomes = []
        with source.open("rb") as handle:
            request = SttRun(op="stt.transcribe", execution=config(tmp_path, target_seconds=0.25,
                threshold_seconds=threshold, max_segment_seconds=0.3), fields=[], multipart_fields={},
                uploads=[("file", SimpleNamespace(file=handle, filename="short.wav", content_type="audio/wav"))],
                workspace=tmp_path, cleanup=cleanup, cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time()+10, on_cancel=lambda: None,
                on_attempt_started=lambda count: None, on_outcome=outcomes.append)
            try:
                with patch.object(stt, "run_media_command", wraps=stt.run_media_command) as media:
                    result = await ChunkedSttAdapter()._run("http://short.fk.test:9876/v1/audio/transcriptions",
                        httpx.Timeout(5), request)
                assert result.status == 200
                assert outcomes[0].fallback_reason == reason
                assert media.await_count == 2, [call.args[0] for call in media.await_args_list]
            finally:
                await cleanup.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("native", [False, True])
def test_reference_without_guard_survives_unhealthy_spool(tmp_path, fake_upstreams, native):
    source = wav(tmp_path / "reference.wav", 0.2)
    spool = tmp_path / "unavailable"
    spool.write_text("not a directory")
    fake_upstreams.router.add_transport("reference.fk.test:9876", HandlerTransport(
        lambda request: httpx.Response(200, json={"text": "words"})))
    service = {"host": "reference.fk.test", "port": 9876, "default_model": "test", "chunking": {
        "spool_dir": str(spool), "max_segment_seconds": 0.3 if native else None,
        "target_seconds": 0.25, "native_long_audio_offline": native}}
    with patch.dict(proxy.config["backends"], {"stt": service}), patch.object(proxy, "_voice_store"), \
         patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()), \
         patch.object(stt, "create_request_workspace", wraps=stt.create_request_workspace) as create:
        assert asyncio.run(proxy._reference_transcript(None, {"id": "test"}, source)) == "words"
    create.assert_not_called()


@pytest.mark.parametrize("result", ["success", "invalid", "deadline", "cancel"])
def test_reference_decode_holds_and_releases_resource_lease(tmp_path, fake_upstreams, result):
    async def run():
        source = wav(tmp_path / "reference.wav", 0.2)
        gate = stt.SttResourceGate()
        observed = []
        real_decode = stt.decode_planning_pcm
        async def decode(*args, **kwargs):
            observed.append(gate.active_count)
            if result == "invalid":
                raise stt.SttInvalidAudio("bad reference")
            if result == "deadline":
                raise stt.SttDeadlineExceeded("deadline")
            if result == "cancel":
                raise asyncio.CancelledError()
            return await real_decode(*args, **kwargs)
        fake_upstreams.router.add_transport("reference.fk.test:9876", HandlerTransport(
            lambda request: httpx.Response(200, json={"text": "words"})))
        service = {"host": "reference.fk.test", "port": 9876, "default_model": "test", "chunking": {
            "spool_dir": str(tmp_path / "spool"), "max_segment_seconds": 0.3, "target_seconds": 0.25}}
        with patch.dict(proxy.config["backends"], {"stt": service}), patch.object(proxy, "_voice_store"), \
             patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()), \
             patch.object(stt, "GLOBAL_STT_RESOURCE_GATE", gate), patch.object(stt, "decode_planning_pcm", side_effect=decode):
            if result == "success":
                assert await proxy._reference_transcript(None, {"id": "test"}, source) == "words"
            elif result == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await proxy._reference_transcript(None, {"id": "test"}, source)
            else:
                with pytest.raises(custom_voices.VoiceError) as caught:
                    await proxy._reference_transcript(None, {"id": "test"}, source)
                error = caught.value
                assert error.status == (400 if result == "invalid" else 504)
                assert error.code == ("invalid_audio" if result == "invalid" else "stt_deadline_exceeded")
                assert ("readable audio" if result == "invalid" else "deadline") in str(error)
        assert observed == [1]
        assert gate.active_count == gate._reserved_bytes == 0
        assert list((tmp_path / "spool").iterdir()) == []
    asyncio.run(run())


def test_disk_lease_covers_segment_and_verification_pcm(tmp_path):
    mib = 1024 * 1024
    execution = config(tmp_path, max_input_bytes=mib, max_decoded_bytes=mib, min_free_bytes=1)
    gate = stt.SttResourceGate()
    with patch.object(stt, "_disk_free_bytes", return_value=10**12):
        lease = gate.acquire(request_id="verify-peak", config=execution)
    try:
        assert gate._reserved_bytes == 5 * mib
        assert execution.request_claim().disk_mb == 5
    finally:
        lease.release()


def test_probe_exposes_only_used_audio_facts():
    assert not hasattr(stt.ProbedAudio(1, 16000, 1), "decoded_bytes")


def test_exact_sample_tail_needs_no_extra_segment():
    segments = stt.plan_quietest_frame_segments(duration_seconds=21.18,
        quietest_frame=lambda lo, hi, near: near, target_seconds=7.06, search_seconds=0,
        max_segment_seconds=7.06, max_segments=3)
    assert segments == [(0, 7.06), (7.06, 14.12), (14.12, 21.18)]


def test_final_plan_limit_compares_integer_sample_lengths(tmp_path):
    async def run():
        source = wav(tmp_path / "rounding.wav", 0.87)
        pieces = [(0, 0.29), (0.29, 0.58), (0.58, 0.87)]
        with patch.object(stt, "plan_quietest_frame_segments", return_value=pieces):
            outcome = await stt.execute_chunking(source_path=source, source_stem="rounding", fields=[],
                config=config(tmp_path, target_seconds=0.29, threshold_seconds=0.4, max_segment_seconds=0.29),
                post_file=AsyncMock(return_value=stt.UpstreamResponse(200, b'{"text":"piece"}', {})),
                cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
        assert outcome.attempts_started == 3
    asyncio.run(run())


def test_normalized_wav_decode_is_bounded_and_path_checked(tmp_path):
    from gateway.engine_protocol.chunked_stt import _RunWire
    async def run():
        source = wav(tmp_path / "long.wav", 2)
        cleanup = stt.SttCleanupOwner()
        execution = config(tmp_path, max_duration_seconds=0.3, max_decoded_bytes=32000)
        request = SimpleNamespace(workspace=tmp_path, cleanup=cleanup, execution=execution,
            cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
        wire = _RunWire("test", None, "unused", request)
        with source.open("rb") as handle:
            try:
                with patch.object(stt, "run_media_command", wraps=stt.run_media_command) as media:
                    with pytest.raises(stt.SttResourceLimitExceeded):
                        await wire.prepared_wav(SimpleNamespace(file=handle))
                command = media.await_args.args[0]
                assert "-t" in command and "-fs" in command
                assert (tmp_path / "normalized-16k.wav").stat().st_size < 11000
                request.workspace = tmp_path.parent / "escape"
                with patch.object(stt, "run_media_command", new=AsyncMock()) as rejected:
                    with pytest.raises(stt.SttConfigurationError):
                        await wire.prepared_wav(SimpleNamespace(file=handle))
                rejected.assert_not_awaited()
            finally:
                await cleanup.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("extension,codec", [("wav", "pcm_s16le"), ("flac", "flac"), ("mp3", "libmp3lame"), ("webm", "libopus")])
@pytest.mark.parametrize("channels", [1, 2])
def test_input_seek_preserves_planning_samples(tmp_path, extension, codec, channels):
    import random
    from array import array
    async def run():
        rate = 48000
        rng = random.Random(47)
        samples = array("h", (rng.randrange(-12000, 12000) for _ in range(rate * 12 * channels)))
        original = tmp_path / "original.wav"
        with wave.open(str(original), "wb") as audio:
            audio.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
            audio.writeframes(samples.tobytes())
        source = tmp_path / f"source.{extension}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(original), "-c:a", codec, str(source)],
                       check=True, capture_output=True)
        execution = config(tmp_path)
        planning = tmp_path / "source.pcm"
        await stt.decode_planning_pcm(source, planning, config=execution, sample_rate=rate, channels=channels,
            cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
        full = planning.read_bytes()
        # Run the real executor: every codec is decoded once for shared extraction PCM.
        cuts = [0, rate * 6 + 17, rate * 10 + 113, rate * 12]
        segments = [(first/rate, last/rate) for first, last in zip(cuts, cuts[1:])]
        sent = []
        async def post(path, _filename):
            index = len(sent)
            with wave.open(str(path)) as audio:
                assert audio.getframerate() == rate
                assert audio.getnchannels() == channels
                actual = audio.readframes(audio.getnframes())
            assert actual == full[cuts[index]*2*channels:cuts[index+1]*2*channels], (extension, index)
            sent.append(path)
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})
        with patch.object(stt, "plan_quietest_frame_segments", return_value=segments), \
             patch.object(stt, "run_media_command", wraps=stt.run_media_command) as media:
            outcome = await stt.execute_chunking(source_path=source, source_stem="source", fields=[],
                config=config(tmp_path, target_seconds=4, threshold_seconds=5, max_segment_seconds=8),
                post_file=post, cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time()+10, on_cancel=None)
        assert outcome.attempts_started == len(sent) == 3
        commands = [call.args[0] for call in media.await_args_list]
        extracts = [command for command in commands if command[0] == "ffmpeg" and command[-1].endswith(".wav")]
        assert len(extracts) == 3
        assert all(command.index("-ss") < command.index("-i") for command in extracts)
        whole_decodes = [command for command in commands if command[0] == "ffmpeg"
                         and command[command.index("-i")+1] == str(source)]
        assert len(whole_decodes) == 2  # planning plus shared source-rate PCM, independent of segment count
        assert not list(tmp_path.glob("extraction-*.pcm"))
    asyncio.run(run())


@pytest.mark.parametrize("rate", [48000, 96000])
def test_resampled_passthrough_still_checks_one_source_sample_over_cap(tmp_path, fake_upstreams, rate):
    async def run():
        source = wav(tmp_path / "boundary.wav", 0.3 + 1/rate, rate)
        cleanup = stt.SttCleanupOwner()
        execution = config(tmp_path, target_seconds=0.25, threshold_seconds=0.3, max_segment_seconds=0.3, max_sample_rate=rate)
        with source.open("rb") as handle:
            request = SttRun(op="stt.transcribe", execution=execution, fields=[], multipart_fields={},
                uploads=[("file", SimpleNamespace(file=handle, filename="boundary.wav", content_type="audio/wav"))],
                workspace=tmp_path, cleanup=cleanup, cancellation_waiters=(),
                deadline_at=asyncio.get_running_loop().time()+10, on_cancel=lambda: None,
                on_attempt_started=lambda count: None, on_outcome=lambda outcome: None)
            try:
                with pytest.raises(stt.SttAudioNotSplittable):
                    await ChunkedSttAdapter()._run("http://never.fk.test:9876/v1/audio/transcriptions", httpx.Timeout(5), request)
            finally:
                await cleanup.aclose()
        assert not list(tmp_path.glob("*.pcm"))
    asyncio.run(run())


def test_reference_guard_obeys_concurrency_admission_before_spooling(tmp_path):
    async def run():
        from gateway.engine_protocol.chunked_stt import SttUpload
        execution = config(tmp_path, global_concurrency=1)
        gate = stt.SttResourceGate()
        lease = gate.acquire(request_id="occupied", config=execution)
        request = SttUpload(op="stt.transcribe", execution=execution, parts={"file": ("ref.wav", b"audio", "audio/wav")})
        try:
            with patch.object(stt, "GLOBAL_STT_RESOURCE_GATE", gate), \
                 patch.object(stt, "create_request_workspace") as create:
                with pytest.raises(stt.SttResourceLimitExceeded):
                    await ChunkedSttAdapter()._post_once("http://never.fk.test:9876/v1/audio/transcriptions", httpx.Timeout(5), request)
                create.assert_not_called()
            assert gate.active_count == 1
        finally:
            lease.release()
        assert gate.active_count == gate._reserved_bytes == 0
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_implicit_owner_reports_cleanup_failure_without_hiding_cancellation(tmp_path, cancel):
    async def run():
        source = wav(tmp_path / "source.wav", 0.6)
        unlink = stt._unlink_chunk_path
        async def post(path, filename):
            if cancel:
                raise asyncio.CancelledError()
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})
        def fail_shared_pcm(path):
            if path.name == "extraction-source.pcm":
                raise OSError("test unlink failed")
            unlink(path)
        with patch.object(stt, "_unlink_chunk_path", side_effect=fail_shared_pcm):
            with pytest.raises(asyncio.CancelledError if cancel else stt.SttCleanupFailure):
                await stt.execute_chunking(source_path=source, source_stem="source", fields=[],
                    config=config(tmp_path, target_seconds=0.25, threshold_seconds=0.3, max_segment_seconds=0.3),
                    post_file=post, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10,
                    on_cancel=None)
        assert not list(tmp_path.glob("segment-*"))
        unlink(tmp_path / "extraction-source.pcm")
    asyncio.run(run())


def test_source_sample_tail_is_replanned_instead_of_refused(tmp_path):
    async def run():
        source = wav(tmp_path / "tail.wav", 0.9 + 1/48000, 48000)
        frames = []
        async def post(path, filename):
            with wave.open(str(path)) as audio:
                assert audio.getframerate() == 48000
                frames.append(audio.getnframes())
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})
        outcome = await stt.execute_chunking(source_path=source, source_stem="tail", fields=[],
            config=config(tmp_path, target_seconds=0.3, threshold_seconds=0.4,
                          max_segment_seconds=0.3, search_seconds=0),
            post_file=post, cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10,
            on_cancel=None)
        assert outcome.attempts_started == len(frames) == 4
        assert sum(frames) == 43201
        assert max(frames) <= 14400
    asyncio.run(run())
