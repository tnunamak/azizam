"""Tiny audio fixture generator for STT/translation compatibility probes.

Generates a minimal mono PCM16 WAV in memory using only the stdlib ``wave``
module, so probes can exercise multipart upload endpoints (transcriptions,
translations) without committing a binary fixture or pulling an audio
encoding dependency. The output is a few kilobytes of silence — enough to
satisfy backends that require a valid RIFF header but small enough to keep
test runs cheap.
"""

from __future__ import annotations

import io
import wave


def silent_wav_bytes(duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Return a mono 16-bit PCM WAV containing ``duration_s`` of silence.

    Defaults to a 0.5s 16 kHz clip (~16 KB) — large enough for STT backends
    to detect a valid WAV header, small enough to keep probes fast.
    """

    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    frame_count = int(sample_rate * duration_s)
    silence = b"\x00\x00" * frame_count
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(silence)
    return buf.getvalue()
