"""Cross-vendor review regressions; real media, fake transcription only."""
import asyncio
import subprocess
import wave
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import proxy
from gateway import client_errors, custom_voices, stt_chunking as stt
from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter, SttUpload
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401
from tests.test_stt_d47_conformance import wav
from tests.test_stt_round4_planner import config


async def chunk(source, execution, post, **kwargs):
    return await stt.execute_chunking(source_path=source, source_stem="test", fields=[],
        config=execution, post_file=post, cancellation_waiters=(),
        deadline_at=asyncio.get_running_loop().time() + 120, on_cancel=None, **kwargs)


@pytest.mark.parametrize("constraint", ["decode_cap", "disk"])
@pytest.mark.parametrize("rate,channels,codec,suffix", [
    (48000, 2, "libmp3lame", "mp3"), (22050, 1, "libmp3lame", "mp3"),
    (48000, 2, "libopus", "ogg"),
])
def test_source_trim_matches_shared_pcm_under_decode_cap(tmp_path, rate, channels, codec, suffix, constraint):
    source = tmp_path / f"noise.{suffix}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"anoisesrc=r={rate}:d=4:seed=47", "-ac", str(channels), "-c:a", codec,
        *(["-q:a", "4"] if codec == "libmp3lame" else []), str(source)], check=True)

    async def run(limit, limited_disk=False):
        sent = []
        async def post(path, _name):
            with wave.open(str(path)) as audio:
                sent.append(audio.readframes(audio.getnframes()))
            return stt.UpstreamResponse(200, b'{"text":"piece"}', {})
        execution = config(tmp_path, target_seconds=0.4, search_seconds=0.1,
            threshold_seconds=0.5, max_segment_seconds=0.5, max_decoded_bytes=limit, min_free_bytes=1024**2 if limited_disk else 1)
        if limited_disk:
            total = source.stat().st_size + 4 * 32000 + 2 * round(0.5 * rate) * channels * 2 + execution.min_free_bytes + 4096
            def free_bytes(_root):
                return total - sum(path.stat().st_size for path in tmp_path.iterdir() if path.is_file())
            gate = stt.SttResourceGate()
            with patch.object(stt, "_disk_free_bytes", side_effect=free_bytes):
                lease = gate.acquire(request_id="disk", config=execution, upload_bytes=source.stat().st_size)
                try:
                    outcome = await chunk(source, execution, post, resource_lease=lease)
                finally:
                    lease.release()
            assert gate.active_count == gate._reserved_bytes == 0
        else:
            outcome = await chunk(source, execution, post)
        assert outcome.mode == "chunked"
        return sent

    shared = asyncio.run(run(2**20))
    # Enough for planning and two segment buffers, but not the full source PCM.
    streamed = asyncio.run(run(150000 if constraint == "decode_cap" else 2**20, constraint == "disk"))
    assert streamed == shared
    assert not list(tmp_path.glob("*.pcm"))


def test_two_hour_stereo_uses_bounded_source_extraction(tmp_path):
    async def run():
        source = tmp_path / "two-hours.mp3"
        source.write_bytes(b"compressed")
        execution = config(tmp_path, target_seconds=480, threshold_seconds=500,
            max_segment_seconds=500, max_segments=16)
        decodes, extractions = [], []
        async def decode(src, destination, **kwargs):
            decodes.append((src, kwargs.get("sample_rate", 16000)))
            assert kwargs.get("sample_rate", 16000) == 16000, "full source PCM exceeds 1 GiB"
            with destination.open("wb") as handle:
                handle.truncate(7200 * 32000)
        async def extract(src, destination, **kwargs):
            assert src == source
            assert kwargs["source_is_pcm"] is False
            assert (kwargs["end_seconds"] - kwargs["start_seconds"]) * 192000 < execution.max_decoded_bytes
            extractions.append(kwargs)
            destination.write_bytes(b"piece")
        with patch.object(stt, "probe_audio", new=AsyncMock(return_value=stt.ProbedAudio(7200, 48000, 2))), \
             patch.object(stt, "decode_planning_pcm", side_effect=decode), \
             patch.object(stt, "extract_segment", side_effect=extract):
            outcome = await chunk(source, execution,
                AsyncMock(return_value=stt.UpstreamResponse(200, b'{"text":"piece"}', {})))
        assert outcome.mode == "chunked"
        assert len(extractions) == outcome.attempts_started == 15
        assert len(decodes) == 1
        assert not list(tmp_path.glob("*.pcm"))
    asyncio.run(run())


@pytest.mark.parametrize("replan", [False, True])
def test_shared_pcm_is_gone_before_run_owner_closes(tmp_path, replan):
    async def run():
        source = wav(tmp_path / "input.wav", 0.8, 48000, 2)
        cleanup = stt.SttCleanupOwner()
        execution = config(tmp_path, target_seconds=0.3, threshold_seconds=0.5,
            max_segment_seconds=None if replan else 0.4, search_seconds=0)
        decoder = stt.decode_planning_pcm
        async def decode(src, destination, **kwargs):
            await decoder(src, destination, **kwargs)
            if replan and destination.name == "extraction-source.pcm":
                with destination.open("r+b") as handle:
                    handle.truncate(48000 * 4 // 5)
        try:
            with patch.object(stt, "decode_planning_pcm", side_effect=decode):
                outcome = await chunk(source, execution,
                    AsyncMock(return_value=stt.UpstreamResponse(200, b'{"text":"piece"}', {})), cleanup=cleanup)
            assert outcome.mode == ("passthrough" if replan else "chunked")
            assert not (tmp_path / "extraction-source.pcm").exists()
        finally:
            await cleanup.aclose()
    asyncio.run(run())


def test_short_clip_admitted_and_large_clip_refused_at_topup(tmp_path):
    async def run():
        gate = stt.SttResourceGate()
        execution = config(tmp_path, max_input_bytes=1536 * 1024**2, max_segments=64,
            target_seconds=480, threshold_seconds=500, max_segment_seconds=500)
        for large in (False, True):
            source = tmp_path / "input.wav"
            wav(source, 0.1)
            if large:
                with source.open("r+b") as handle:
                    handle.truncate(execution.max_input_bytes)
            with patch.object(stt, "_disk_free_bytes", return_value=2 * 1024**3) as free:
                lease = gate.acquire(request_id="test", config=execution)
                try:
                    free.return_value -= source.stat().st_size
                    post = AsyncMock()
                    if large:
                        with patch.object(stt, "probe_audio", new=AsyncMock(return_value=stt.ProbedAudio(14400, 48000, 2))), \
                             patch.object(stt, "decode_planning_pcm", new=AsyncMock()) as decoder:
                            with pytest.raises(stt.SttResourceLimitExceeded):
                                await chunk(source, execution, post, resource_lease=lease)
                            decoder.assert_not_awaited()
                    else:
                        assert (await chunk(source, execution, post, resource_lease=lease)).mode == "passthrough"
                    post.assert_not_awaited()
                finally:
                    lease.release()
        assert gate._reserved_bytes == gate.active_count == 0
    asyncio.run(run())


def test_voice_reference_reserves_clip_size_on_two_gib_host(tmp_path, fake_upstreams):
    async def run():
        gate = stt.SttResourceGate()
        source = wav(tmp_path / "reference.wav", 0.2, 48000, 2)
        observed = []
        def respond(request):
            observed.append(gate._reserved_bytes)
            return httpx.Response(200, json={"text": "voice"})
        fake_upstreams.router.add_transport("review.fk.test:9876", HandlerTransport(respond))
        with patch.object(stt, "GLOBAL_STT_RESOURCE_GATE", gate), \
             patch.object(stt, "_disk_free_bytes", return_value=2 * 1024**3):
            reply = await ChunkedSttAdapter()._post_once("http://review.fk.test:9876/v1/audio/transcriptions",
                httpx.Timeout(5), SttUpload(op="stt.transcribe", execution=config(tmp_path),
                    parts={"file": ("reference.wav", source.read_bytes(), "audio/wav")}))
        assert reply.status == 200
        assert observed and 0 < observed[0] < 1024**2
        assert gate.active_count == gate._reserved_bytes == 0
    asyncio.run(run())


def test_written_lease_bytes_are_credited_once_and_failed_topup_is_atomic(tmp_path):
    mib = 1024**2
    execution = config(tmp_path, max_input_bytes=mib, min_free_bytes=mib)
    gate = stt.SttResourceGate()
    with patch.object(stt, "_disk_free_bytes", return_value=6 * mib) as free:
        first = gate.acquire(request_id="first", config=execution)
        second = gate.acquire(request_id="second", config=execution)
        try:
            first.workspace = tmp_path
            (tmp_path / "upload.part").write_bytes(b"x" * (mib // 2))
            free.return_value -= mib // 2
            first.resize(4 * mib)
            assert gate._reserved_bytes == 5 * mib
            with pytest.raises(stt.SttResourceLimitExceeded):
                second.resize(2 * mib)
            assert gate._reserved_bytes == 5 * mib
            assert second._reserved_bytes == mib
            assert gate.active_count == 2
        finally:
            first.release()
            second.release()
    assert gate.active_count == gate._reserved_bytes == 0


def test_understated_metadata_tops_up_before_each_decode_trial(tmp_path):
    async def run():
        source = wav(tmp_path / "source.wav", 0.6, 48000, 2)
        destination = tmp_path / "planning.pcm"
        execution = config(tmp_path, max_duration_seconds=1, max_decoded_bytes=40000)
        gate = stt.SttResourceGate()
        lease = gate.acquire(request_id="metadata", config=execution, upload_bytes=source.stat().st_size)
        lease.workspace = tmp_path
        command = stt.run_media_command
        limits = []
        async def decode(argv, **kwargs):
            limit = int(argv[argv.index("-fs") + 1])
            assert source.stat().st_size + limit <= lease._reserved_bytes
            limits.append(limit)
            return await command(argv, **kwargs)
        try:
            with patch.object(stt, "run_media_command", side_effect=decode):
                await stt.decode_planning_pcm(source, destination, config=execution,
                    cancellation_waiters=(), deadline_at=asyncio.get_running_loop().time()+10,
                    on_cancel=None, resource_lease=lease, expected_duration_seconds=0.1)
            assert len(limits) > 1
            assert destination.stat().st_size == 19200
        finally:
            destination.unlink(missing_ok=True)
            lease.release()
    asyncio.run(run())


@pytest.mark.parametrize("error,status,code", [
    (stt.SttInvalidAudio("bad"), 400, "invalid_value"),
    (stt.SttDeadlineExceeded("deadline"), 504, "stt_request_deadline"),
])
def test_voice_errors_use_catalogued_codes_and_voice_param(tmp_path, error, status, code):
    source = wav(tmp_path / "reference.wav", 0.2)
    with patch.dict(proxy.config["backends"], {"stt": {"host": "review.fk.test", "port": 9876}}), \
         patch.object(proxy, "_stt_adapter", return_value=type("Adapter", (), {"invoke": AsyncMock(side_effect=error)})()):
        with pytest.raises(custom_voices.VoiceError) as caught:
            asyncio.run(proxy._reference_transcript(None, {"id": "test"}, source))
    assert (caught.value.status, caught.value.code, caught.value.param) == (status, code, "voice")
    assert code in client_errors.ERROR_CODES


def test_voice_contention_returns_retryable_busy_response(tmp_path):
    async def run():
        gate = stt.SttResourceGate()
        source = wav(tmp_path / "reference.wav", 0.2)
        execution = config(tmp_path)
        leases = [gate.acquire(request_id=str(i), config=execution) for i in range(2)]
        service = {"host": "review.fk.test", "port": 9876,
                   "chunking": {"spool_dir": str(tmp_path.parent), "max_segment_seconds": 40, "target_seconds": 30}}
        try:
            with patch.object(stt, "GLOBAL_STT_RESOURCE_GATE", gate), \
                 patch.dict(proxy.config["backends"], {"stt": service}), \
                 patch.object(proxy, "_stt_adapter", return_value=ChunkedSttAdapter()), \
                 patch.object(proxy, "_voice_store") as store, \
                 patch.object(proxy, "_custom_voice_engines", return_value=["test"]):
                store.return_value.get_voice.return_value = {"id": "test"}
                store.return_value.reference_path.return_value = source
                response = await proxy._apply_custom_voice({}, "test", {"custom_voices": True},
                    None, "omnivoice")
            assert response.status_code == 503
            assert response.headers.get("Retry-After") == "5", response.body
            assert b'resource_admission_denied' in response.body
            assert b'busy' in response.body
        finally:
            for lease in leases:
                lease.release()
    asyncio.run(run())
