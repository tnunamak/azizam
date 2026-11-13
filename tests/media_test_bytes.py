"""Small decodable media fixtures for tests that store or deliver assets."""

from __future__ import annotations

import io
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def mp4_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "sample.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
             "color=c=black:s=16x16:d=0.2:r=5", "-c:v", "libx264", "-threads", "1", str(output)],
            capture_output=True, check=True,
        )
        return output.read_bytes()


def png_bytes(color: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """A decodable 2x2 PNG; a different colour gives different bytes."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(out, format="PNG")
    return out.getvalue()


def glb_bytes(tag: bytes = b"") -> bytes:
    """A valid minimal GLB already in stripped form (strip_metadata returns it unchanged).

    `tag` goes in a BIN chunk, NUL-padded to 4 bytes, so different tags give different files."""
    import struct

    doc = b'{"asset":{"version":"2.0"}}'
    doc += b" " * (-len(doc) % 4)
    chunks = struct.pack("<I4s", len(doc), b"JSON") + doc
    if tag:
        tag += b"\x00" * (-len(tag) % 4)
        chunks += struct.pack("<I4s", len(tag), b"BIN\x00") + tag
    return b"glTF" + struct.pack("<II", 2, len(chunks) + 12) + chunks


def wav_bytes(frames: int = 80, rate: int = 8000) -> bytes:
    """A real tiny mono 16-bit PCM WAV of silence."""
    import wave

    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return out.getvalue()
