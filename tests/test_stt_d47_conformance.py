"""D47 conformance against real, synthetic audio; no inference engines."""

import asyncio
import json
import struct
import subprocess
import wave
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from gateway import stt_chunking as stt
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
        element = b"\xec\x89" + bytes(9) if duration is None else b"\x44\x89\x88" + struct.pack(">d", duration * 1000)
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
