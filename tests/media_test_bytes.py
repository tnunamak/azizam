"""Small decodable media fixtures for tests that store or deliver assets."""

from __future__ import annotations

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
             "color=c=black:s=16x16:d=0.2:r=5", "-c:v", "mpeg4", str(output)],
            capture_output=True, check=True,
        )
        return output.read_bytes()
