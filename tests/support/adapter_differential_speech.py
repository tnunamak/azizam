"""Speech and transcription helpers for the differential scenarios.

Config copy for always-on services, wav builders, the long recording for the
chunked transcription path, and the multipart builder for caller uploads.
"""

from __future__ import annotations

import io
import os
import struct
import wave
from pathlib import Path
from typing import Any, Iterable

import proxy
from tests.speech_tier_helpers import _base_config, _wav_bytes  # noqa: F401  (re-exported for the scenarios)


def speech_config(spool: Path, *, voices_dir: Path | None = None, stt_chunking: dict[str, Any] | None = None,
                  voices: dict[str, Any] | None = None) -> dict[str, Any]:
    """A copy of the gateway config in which every service is always on (no lifecycle block).

    Always-on services are never probed or started, so no unit manager or health
    check runs. ``stt_chunking`` replaces the default STT service's chunking data.
    """
    cfg = _base_config(str(spool))
    for service in cfg["backends"].values():
        if isinstance(service, dict):
            service.pop("lifecycle", None)
    if stt_chunking is not None:
        cfg["backends"]["stt"]["chunking"] = {"spool_dir": str(spool), **stt_chunking}
    if voices_dir is not None or voices:
        cfg["voices"] = {**(cfg.get("voices") or {}), "dir": str(voices_dir or spool / "voices"), **(voices or {})}
    return cfg


def patch_config(rig, cfg: dict[str, Any]) -> None:
    rig.set(proxy, "config", cfg)


def multipart_upload(fields: Iterable[tuple[str, str]], audio: bytes, *, filename: str = "a.wav") -> dict[str, Any]:
    """httpx ``data``/``files`` arguments for an audio upload."""
    return {"data": dict(fields), "files": {"file": (filename, audio, "audio/wav")}}


def silent_wav(rate: int = 16000, seconds: float = 0.5, channels: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 1000) * int(rate * seconds) * channels)
    return out.getvalue()


def long_silenced_wav(directory: Path) -> bytes:
    """75 s of tone with 1 s gaps at 20, 40 and 60 s: the cut points the chunker looks for."""
    path = directory / "long.wav"
    rate = 8000
    gaps = ((20.0, 21.0), (40.0, 41.0), (60.0, 61.0))
    with wave.open(os.fspath(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(rate * 75):
            second = index / rate
            silent = any(start <= second < end for start, end in gaps)
            frames += struct.pack("<h", 0 if silent else int(8000 * ((index % 31) - 15) / 15))
        handle.writeframes(bytes(frames))
    return path.read_bytes()


def handler_engine(rig, host: str, handler) -> None:
    """Route ``host`` to ``handler(httpx.Request) -> httpx.Response`` (an engine route the shared fakes do not list).

    The recorder still logs every request, so the trace shows what the gateway sent.
    """
    from tests.support.fake_upstream import HandlerTransport

    rig.fakes.router.add_transport(host, HandlerTransport(handler))


class CountingIds:
    """Stand-in for the ``secrets`` module of the voice store: ids that count up, not random ones."""

    def __init__(self) -> None:
        self.count = 0

    def token_hex(self, nbytes: int = 32) -> str:
        self.count += 1
        return f"{self.count:0{nbytes * 2}x}"
