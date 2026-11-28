"""Speech (TTS) and transcription (STT) behaviour capture against fake engines.

Each test is one scenario; ``play`` runs it twice in this process, requires identical
traces, records the trace for the junit dump and compares it with
``tests/fixtures/adapter_differential/<name>.json``
(see docs/verification/ENGINE_W6B_DIFFERENTIAL.md).

All services run from a config copy without lifecycle blocks (always on), so no unit
manager or health check runs. Timeouts and dropped connections are injected as the
transport error the engine client would see (``rig.down_engine`` with an httpx error)
or as a fake that cuts the connection; no scenario waits on the real clock.
"""
from __future__ import annotations

import struct

import httpx
import pytest

from gateway import custom_voices, stt_chunking

from tests.speech_tier_helpers import HAVE_FFMPEG, _wav_bytes
from tests.support import adapter_differential as ad
from tests.support.adapter_differential_rig import exchange, hex_body, play, scenario
from tests.support.adapter_differential_speech import (
    CountingIds, handler_engine, long_silenced_wav, multipart_upload, patch_config, silent_wav, speech_config,
)

TTS_HOST = "127.0.0.1:8010"      # the fast speech engine (streams PCM)
TTS_SECOND = "127.0.0.1:8002"    # the second engine of the tier (buffered)
STT_HOST = "127.0.0.1:5092"      # the default transcription engine
STT_STREAM_HOST = "127.0.0.1:8012"  # a transcription engine that streams SSE
WAV = _wav_bytes(24000, 1)
SPEECH = {"model": "tts-fast", "voice": "shimmer", "input": "hello"}


def _speech_engine(rig, response, *, host=TTS_HOST, name="tts", required=True):
    rig.engine(name, scenario("openai-compatible", f"{rig.name}-{name}", [
        exchange("speech", "POST", "/v1/audio/speech", response, repeat=True, required=required)]), host=host)


def _setup_config(rig, **kwargs):
    rig.recorder.skip_sleep = lambda delay: delay < 30  # a longer sleep is a deadline timer raced against work
    # The STT admission check reads the host's free disk space, which is not a gateway behaviour.
    rig.set(stt_chunking, "_disk_free_bytes", lambda _path: 10 << 30)
    patch_config(rig, speech_config(rig.tmp / "spool", **kwargs))


def _setup_stt(rig, **kwargs):
    _setup_config(rig, **kwargs)
    # proxy._record_stt_input_shape accepts inputs by iterating a set, so the receipt's
    # accepted_inputs order follows the process hash seed. Compare it sorted (STT routes only).
    rig.norm.unordered_keys = frozenset({"accepted_inputs"})


# --------------------------------------------------------------------------- TTS

def test_tts_buffered_wav(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, hex_body(WAV, "audio/wav"))

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="buffered wav", json_body={**SPEECH, "response_format": "wav"})

    play("tts_buffered_wav", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/audio/speech, response_format wav, engine answers a wav; returned unchanged")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to convert the engine wav to mp3")
def test_tts_buffered_mp3(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, hex_body(WAV, "audio/wav"))

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="default format is mp3", json_body=SPEECH)

    play("tts_buffered_mp3", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/audio/speech without response_format: the engine wav is converted to mp3 by ffmpeg")


def _pcm(frames: int = 4410) -> bytes:
    return struct.pack("<h", 2500) * frames


def test_tts_stream_pcm(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, {"status": 200, "headers": {"content-type": "audio/pcm"},
                             "body": [{"hex": _pcm(1000).hex()}, {"hex": _pcm(2000).hex()}, {"hex": _pcm(500).hex()}]})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="stream_format audio, response_format pcm",
                       json_body={**SPEECH, "response_format": "pcm", "stream_format": "audio"})

    play("tts_stream_pcm", tmp_path, record_testsuite_property, setup, drive,
         description="Streamed PCM relay through the resampler: the joined bytes reach the caller (chunk cuts follow an ffmpeg pipe read, so they are not recorded)")


def test_tts_stream_sse(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, {"status": 200, "headers": {"content-type": "audio/pcm"},
                             "body": [{"hex": _pcm(1000).hex()}, {"hex": _pcm(1500).hex()}]})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="stream_format sse", sse_audio=True,
                       json_body={**SPEECH, "response_format": "pcm", "stream_format": "sse"})

    play("tts_stream_sse", tmp_path, record_testsuite_property, setup, drive,
         description="Streamed speech as SSE: speech.audio.delta events then speech.audio.done")


def test_tts_sse_from_buffered_engine(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, hex_body(WAV, "audio/wav"), host=TTS_SECOND, name="tts-second")

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="stream_format sse, engine cannot stream",
                       json_body={"model": "omnivoice", "voice": "alloy", "input": "hello", "stream_format": "sse"})

    play("tts_sse_from_buffered_engine", tmp_path, record_testsuite_property, setup, drive,
         description="stream_format sse against an engine that cannot stream: the wav is answered as one SSE delta")


def test_tts_upstream_4xx(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, {"status": 422, "headers": {"retry-after": "7"}, "json": {"error": {"message": "bad input", "type": "invalid_request_error"}}})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="engine answers 422", json_body={**SPEECH, "response_format": "wav"})

    play("tts_upstream_4xx", tmp_path, record_testsuite_property, setup, drive,
         description="The engine refuses with 422 and retry-after: what the caller sees")


def test_tts_upstream_5xx(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, {"status": 503, "headers": {"retry-after": "3"}, "json": {"error": {"message": "loading", "type": "api_error"}}})
        _speech_engine(rig, {"status": 500, "json": {"error": {"message": "boom", "type": "api_error"}}}, host=TTS_SECOND, name="tts-second")

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="both engines answer 5xx", json_body={**SPEECH, "response_format": "wav"})

    play("tts_upstream_5xx", tmp_path, record_testsuite_property, setup, drive,
         description="The first engine answers 503 and the second 500: the error the caller sees and both engine requests")


def test_tts_fallback_to_second_engine(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, {"status": 503, "headers": {"retry-after": "3"}, "json": {"error": {"message": "loading", "type": "api_error"}}})
        _speech_engine(rig, hex_body(WAV, "audio/wav"), host=TTS_SECOND, name="tts-second")

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="first engine 503, second engine serves", json_body={**SPEECH, "response_format": "wav"})

    play("tts_fallback_to_second_engine", tmp_path, record_testsuite_property, setup, drive,
         description="The first engine answers 503; the tier's second engine serves the request and the receipt records it")


def test_tts_stream_upstream_error(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        _speech_engine(rig, {"status": 429, "headers": {"retry-after": "7"}, "json": {"error": {"type": "rate_limited", "message": "slow down"}}})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="streamed request, engine answers 429",
                       json_body={**SPEECH, "response_format": "pcm", "stream_format": "audio"})

    play("tts_stream_upstream_error", tmp_path, record_testsuite_property, setup, drive,
         description="Streamed speech where the engine answers 429 with retry-after before any audio")


def test_tts_timeout(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        rig.down_engine(TTS_HOST, httpx.ReadTimeout("engine read timed out"))

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="engine read times out", json_body={**SPEECH, "response_format": "wav"})

    play("tts_timeout", tmp_path, record_testsuite_property, setup, drive,
         description="The engine client raises ReadTimeout (injected at the transport): 504 'TTS request timeout'")


def test_tts_unreachable(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_config(rig)
        rig.down_engine(TTS_HOST)
        rig.down_engine(TTS_SECOND)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/speech", label="engine refuses connections", json_body={**SPEECH, "response_format": "wav"})

    play("tts_unreachable", tmp_path, record_testsuite_property, setup, drive,
         description="Every speech engine of the tier refuses the connection")


# --------------------------------------------------------------------------- STT

def _stt_engine(rig, response, *, host=STT_HOST, name="stt", times=None, required=True):
    rig.engine(name, scenario("openai-compatible", f"{rig.name}-{name}", [
        exchange("transcribe", "POST", "/v1/audio/transcriptions", response, repeat=times is None, times=times, required=required)]), host=host)


def test_stt_json(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"json": {"text": "hello world"}})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="transcribe, json",
                       **multipart_upload([("model", "parakeet"), ("response_format", "json")], silent_wav()))

    play("stt_json", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/audio/transcriptions passthrough, response_format json")


def test_stt_verbose_json(tmp_path, record_testsuite_property):
    verbose = {"task": "transcribe", "language": "english", "duration": 0.5, "text": "hello world",
               "segments": [{"id": 0, "start": 0.0, "end": 0.5, "text": "hello world"}]}

    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"json": verbose})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="transcribe, verbose_json",
                       **multipart_upload([("model", "parakeet"), ("response_format", "verbose_json"),
                                           ("timestamp_granularities[]", "segment")], silent_wav()))

    play("stt_verbose_json", tmp_path, record_testsuite_property, setup, drive,
         description="Passthrough with response_format verbose_json and a timestamp granularity")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to cut a long recording into chunks")
def test_stt_chunked(tmp_path, record_testsuite_property):
    """75 s of audio with three pauses; threshold 30 s, so it is cut at the pauses and the texts are merged."""
    chunking = {"threshold_seconds": 30, "target_seconds": 20, "max_input_bytes": 4 * 1024 * 1024,
                "max_decoded_bytes": 8 * 1024 * 1024, "max_duration_seconds": 120, "max_sample_rate": 16000,
                "max_channels": 2, "max_segments": 8, "min_free_bytes": 1, "global_concurrency": 2,
                "backend_timeout_seconds": 30, "deadline_seconds": 60, "setup_failure_policy": "passthrough",
                "enabled": True}

    def setup(rig):
        _setup_stt(rig, stt_chunking=chunking)
        rig.engine("stt", scenario("openai-compatible", f"{rig.name}-stt", [
            exchange(f"part-{i}", "POST", "/v1/audio/transcriptions", {"json": {"text": f"part {i}"}}) for i in (1, 2, 3, 4)]),
            host=STT_HOST)
        rig.fakes._fakes[-1].waive("part-4")  # the number of cuts is whatever the chunker decides; the trace records it

    async def drive(rig):
        audio = long_silenced_wav(rig.tmp)
        await rig.call("POST", "/v1/audio/transcriptions", label="transcribe a 75 s recording",
                       **multipart_upload([("model", "parakeet"), ("response_format", "json")], audio, filename="long.wav"))

    play("stt_chunked", tmp_path, record_testsuite_property, setup, drive,
         description="Chunked transcription: ffmpeg cuts at the pauses, each chunk is posted, the texts are merged")


def _stt_form(*extra, model="parakeet", fmt="json"):
    return multipart_upload([("model", model), ("response_format", fmt), *extra], silent_wav())


def test_stt_stream_native_sse(tmp_path, record_testsuite_property):
    events = [{"type": "transcript.text.delta", "delta": "hello "}, {"type": "transcript.text.delta", "delta": "world"},
              {"type": "transcript.text.done", "text": "hello world"}]

    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"status": 200, "body": [{"event": e} for e in events] + [{"done": True}]}, host=STT_STREAM_HOST, name="stt-stream")

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="stream=true against an engine that streams SSE",
                       chunks=True, **_stt_form(("stream", "true"), model="stt-fast"))

    play("stt_stream_native_sse", tmp_path, record_testsuite_property, setup, drive,
         description="stream=true with a streaming engine: the gateway relays the engine's SSE events")


def test_stt_stream_buffered_engine(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"json": {"text": "hello world"}})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="stream=true against an engine that does not stream",
                       chunks=True, **_stt_form(("stream", "true")))

    play("stt_stream_buffered_engine", tmp_path, record_testsuite_property, setup, drive,
         description="stream=true with a buffered engine: the gateway answers in the SSE shape from the JSON transcript; stream is not forwarded")


def test_stt_upstream_4xx(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"status": 400, "json": {"error": {"message": "unsupported audio", "type": "invalid_request_error"}}})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="engine answers 400", **_stt_form())

    play("stt_upstream_4xx", tmp_path, record_testsuite_property, setup, drive,
         description="The engine refuses the audio with 400: what the caller sees")


def test_stt_upstream_5xx(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"status": 503, "headers": {"retry-after": "9"}, "json": {"error": {"message": "loading", "type": "api_error"}}})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="engine answers 503", **_stt_form())

    play("stt_upstream_5xx", tmp_path, record_testsuite_property, setup, drive,
         description="The engine answers 503 with retry-after: what the caller sees")


def test_stt_timeout(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        rig.down_engine(STT_HOST, httpx.ReadTimeout("engine read timed out"))

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="engine read times out", **_stt_form())

    play("stt_timeout", tmp_path, record_testsuite_property, setup, drive,
         description="The engine client raises ReadTimeout (injected at the transport): 504 stt_request_timeout")


def test_stt_connection_lost(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        _stt_engine(rig, {"status": 200, "headers": {"content-type": "application/json"},
                          "body": [{"text": '{"text": "hel'}, {"disconnect": True}]})

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="engine drops the connection mid-response", **_stt_form())

    play("stt_connection_lost", tmp_path, record_testsuite_property, setup, drive,
         description="The engine cuts the connection after part of the body: 502 stt_backend_connection_lost")


def test_stt_unreachable(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        rig.down_engine(STT_HOST)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/transcriptions", label="engine refuses connections", **_stt_form())

    play("stt_unreachable", tmp_path, record_testsuite_property, setup, drive,
         description="The engine refuses the connection: 502 stt_backend_unavailable")


def test_stt_translations(tmp_path, record_testsuite_property):
    def setup(rig):
        _setup_stt(rig)
        rig.engine("stt", scenario("openai-compatible", f"{rig.name}-stt", [
            exchange("translate", "POST", "/v1/audio/translations", {"json": {"text": "hello world"}}, repeat=True, required=False)]),
            host=STT_HOST)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/translations", label="translate", **_stt_form())

    play("stt_translations", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/audio/translations against the default engine (whatever the gateway decides for this config)")


# --------------------------------------------------------------------------- custom voices

OMNI_HOST = TTS_SECOND  # the engine that clones voices, designs them and answers the voice check
SAMPLE_VOICE = None


def _voice_clip(seconds: float = 8.0) -> bytes:
    from tests.voice_test_audio import speechlike

    return speechlike(seconds)


def _clone_engine(rig, *, similarity=0.82):
    """The voice engine's routes: speech, and the voice-check route the shared fakes do not list."""
    import json as _json

    designed = _voice_clip(8.0)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/voice-check":
            return httpx.Response(200, json={"speaker_similarity": similarity, "encoder": "fake-encoder"})
        if request.url.path == "/v1/audio/speech":
            body = _json.loads(request.content)
            # A design request carries a seed; it must answer a clip long enough to clone.
            return httpx.Response(200, content=designed if "seed" in body else WAV, headers={"content-type": "audio/wav"})
        return httpx.Response(404, json={"error": "unknown route"})

    handler_engine(rig, OMNI_HOST, handle)


def _note_voice_files(rig):
    """The voice store on disk: metadata records and reference clips (size and sha256)."""
    rig.notes["voice_store"] = ad.describe_assets(rig.tmp / "voices", rig.norm)


def _voice_setup(rig):
    _setup_stt(rig, voices={"quality_engine": "omnivoice", "design_engine": "omnivoice"}, voices_dir=rig.tmp / "voices")
    rig.set(custom_voices, "secrets", CountingIds())  # voice and consent ids count up instead of being random


def test_tts_custom_voice_clone(tmp_path, record_testsuite_property):
    """Create a voice from a sample (quality check posts /v1/audio/voice-check), then speak with it.

    The voice engine here is an omnivoice-style engine, so the speech request first asks the STT
    engine to transcribe the reference clip (`_reference_transcript`), then sends voice_ref and ref_text.
    """
    def setup(rig):
        _voice_setup(rig)
        _clone_engine(rig)
        _stt_engine(rig, {"json": {"text": "the reference clip says this"}})

    async def drive(rig):
        created = await rig.call("POST", "/v1/audio/voices", label="create a voice from an audio sample",
                                 data={"name": "narrator", "type": "audio_sample"},
                                 files={"audio_sample": ("sample.wav", _voice_clip(), "audio/wav")})
        voice_id = created.json()["id"]
        await rig.call("POST", "/v1/audio/speech", label="speak with the custom voice",
                       json_body={"model": "omnivoice", "voice": voice_id, "input": "hello", "response_format": "wav"})
        await rig.call("POST", "/v1/audio/speech", label="speak again: the transcript is cached in the voice record",
                       json_body={"model": "omnivoice", "voice": voice_id, "input": "again", "response_format": "wav"})
        _note_voice_files(rig)

    play("tts_custom_voice_clone", tmp_path, record_testsuite_property, setup, drive,
         description="Voice from an audio sample, quality probe, speech with the custom voice, reference transcription once")


def test_tts_voice_design(tmp_path, record_testsuite_property):
    def setup(rig):
        _voice_setup(rig)
        _clone_engine(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/voices", label="design a voice from a prompt",
                       json_body={"name": "designed", "type": "prompt", "prompt": "young british female, high pitch", "seed": 7})
        _note_voice_files(rig)

    play("tts_voice_design", tmp_path, record_testsuite_property, setup, drive,
         description="Voice design: the design request goes to the speech route of the design engine, then the quality probe")


def test_tts_voice_quality_unavailable(tmp_path, record_testsuite_property):
    def setup(rig):
        _voice_setup(rig)
        rig.down_engine(OMNI_HOST)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/voices", label="create a voice while the engine is down",
                       data={"name": "narrator", "type": "audio_sample"},
                       files={"audio_sample": ("sample.wav", _voice_clip(), "audio/wav")})
        _note_voice_files(rig)

    play("tts_voice_quality_unavailable", tmp_path, record_testsuite_property, setup, drive,
         description="The voice-check call is refused three times (sleeps between tries are logged); the voice is created unchecked")
