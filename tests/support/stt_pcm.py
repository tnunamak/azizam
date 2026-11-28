"""Synthetic decoded audio for the long-audio cut planner.

The planner reads raw 16 kHz mono 16-bit samples. These helpers write a speech-like signal (a loud repeating
pattern) with quiet stretches at chosen times, so a test can say where the quietest place is.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from gateway import stt_chunking

SAMPLE_RATE = 16_000
# One 10 ms period of a fixed pseudo-random pattern. Every 20 ms frame (320 samples) that starts on a 10 ms step holds
# two whole periods, so all loud frames have the same energy and only a quiet stretch can be quieter.
_PERIOD_SAMPLES = 160
_LOUD_PERIOD = struct.pack(f"<{_PERIOD_SAMPLES}h", *(int(9000 * (((index * 7919) % 201) - 100) / 100) for index in range(_PERIOD_SAMPLES)))
_LOUD_SECOND = _LOUD_PERIOD * (SAMPLE_RATE // _PERIOD_SAMPLES)


def pcm_bytes(duration_seconds: float, quiet_at: Iterable[float] = (), *, quiet_width: float = 0.3, floor: int = 0,
              sample_rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    """Loud everywhere except ``quiet_width`` seconds centred on each time in ``quiet_at`` (samples ``floor`` loud)."""
    total = int(duration_seconds * sample_rate)
    second = _LOUD_SECOND if sample_rate == SAMPLE_RATE else (_LOUD_PERIOD * (sample_rate // _PERIOD_SAMPLES + 1))[:sample_rate*2]
    if channels != 1:
        second = b"".join(second[index:index+2] * channels for index in range(0, len(second), 2))
    loud = bytearray((second * (total // sample_rate + 1))[: total * channels * 2])
    quiet = struct.pack("<h", floor) * channels
    for centre in quiet_at:
        first = max(0, int((centre - quiet_width / 2) * sample_rate))
        last = min(total, int((centre + quiet_width / 2) * sample_rate))
        loud[first * channels * 2 : last * channels * 2] = quiet * (last - first)
    return bytes(loud)


def write_pcm(path: Path, duration_seconds: float, quiet_at: Iterable[float] = (), **kwargs: Any) -> None:
    path.write_bytes(pcm_bytes(duration_seconds, quiet_at, **kwargs))


def planning_pcm(duration_seconds: float, quiet_at: Iterable[float] = (), **kwargs: Any):
    """Patch the planning decode so it writes the synthetic signal instead of running ffmpeg."""
    quiet = tuple(quiet_at)

    async def decode(_source, destination, **options: Any):
        write_pcm(Path(destination), duration_seconds, quiet, sample_rate=options.get("sample_rate", SAMPLE_RATE),
                  channels=options.get("channels", 1), **kwargs)

    return patch.object(stt_chunking, "decode_planning_pcm", new=AsyncMock(side_effect=decode))
