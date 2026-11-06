"""Synthetic speech-like audio for the custom voice tests: tone bursts (voiced) separated by quiet noise (pauses)."""

from __future__ import annotations

import array
import io
import math
import random
import shutil
import subprocess
import wave

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
RATE = 24000


def speechlike(seconds: float, *, amplitude: int = 9000, noise: int = 40, burst: float = 0.35, period: float = 0.5, seed: int = 1, rate: int = RATE) -> bytes:
    """WAV bytes: every ``period`` seconds a ``burst``-second voiced tone over low noise."""
    rng = random.Random(seed)
    total = int(seconds * rate)
    out = array.array("h")
    for n in range(total):
        t = n / rate
        sample = rng.randint(-noise, noise)
        if (t % period) < burst:
            sample += int(amplitude * (0.6 * math.sin(2 * math.pi * 180 * t) + 0.3 * math.sin(2 * math.pi * 540 * t) + 0.1 * math.sin(2 * math.pi * 1200 * t)))
        out.append(max(-32768, min(32767, sample)))
    return wav_of(out, rate)


def wav_of(samples: array.array, rate: int = RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def samples_of(wav: bytes) -> array.array:
    with wave.open(io.BytesIO(wav)) as handle:
        data = array.array("h")
        data.frombytes(handle.readframes(handle.getnframes()))
        return data


def transcode(wav: bytes, extension: str) -> bytes:
    """Re-encode ``wav`` with ffmpeg (mp3, ogg, flac, m4a ...)."""
    import pathlib
    import tempfile

    codec = {"m4a": ["-c:a", "aac"], "ogg": ["-c:a", "libvorbis"], "mp3": [], "flac": []}[extension]
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / f"out.{extension}"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "wav", "-i", "pipe:0", *codec, str(target)],
            input=wav, capture_output=True, check=True,
        )
        return target.read_bytes()
