"""Small preview images for the Library grid.

A tile needs about 480 px, not a 2 MB original. ``thumbnail_for`` makes a
WebP next to the asset on the first request and serves the stored file after
that. It never enlarges, and it drops all metadata (re-encoded pixels only).
Video previews come from the first frame, read with ``ffmpeg``.

The file sits in ``<asset dir>/thumbs/``. The asset catalog lists plain files
only, so the folder is never mistaken for assets. Removing an asset removes
its preview (``remove_thumbnail``).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

THUMB_EDGE = 480
THUMB_MODE = "thumb"  # signing mode of a thumbnail URL; no content URL can carry it
THUMB_MEDIA_TYPE = "image/webp"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(3)  # a screen of new tiles must not take every core
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def thumbnail_path(source: Path) -> Path:
    return source.parent / "thumbs" / f"{source.stem}.{THUMB_EDGE}.webp"


def can_thumbnail(source: Path | None) -> bool:
    return source is not None and source.suffix.lower() in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES


def remove_thumbnail(source: Path | None) -> None:
    if source is not None:
        thumbnail_path(source).unlink(missing_ok=True)


def thumbnail_for(source: Path) -> Path | None:
    """Return the preview file for ``source``, making it if needed; None if it cannot be made."""
    if not can_thumbnail(source) or not source.is_file():
        return None
    target = thumbnail_path(source)
    if _fresh(target, source):
        return target
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(target), threading.Lock())
    with lock:
        if _fresh(target, source):
            return target
        with _SLOTS:
            try:
                return _make(source, target)
            except Exception:
                return None


def _fresh(target: Path, source: Path) -> bool:
    try:
        return target.stat().st_mtime_ns >= source.stat().st_mtime_ns
    except OSError:
        return False


def _make(source: Path, target: Path) -> Path | None:
    from PIL import Image, ImageOps

    is_video = source.suffix.lower() in _VIDEO_SUFFIXES
    if not is_video and source.stat().st_size > _MAX_SOURCE_BYTES:
        return None  # ffmpeg reads one frame of a video, so only images are capped
    frame: Path | None = None
    try:
        if is_video:
            frame = _first_frame(source)
            if frame is None:
                return None
        with Image.open(frame or source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((THUMB_EDGE, THUMB_EDGE))  # shrinks only
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "transparency" in image.info or image.mode in ("LA", "PA") else "RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            os.close(fd)
            try:
                image.save(tmp_name, "WEBP", quality=78, method=4)
                os.replace(tmp_name, target)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
        return target
    finally:
        if frame is not None:
            frame.unlink(missing_ok=True)


def _first_frame(video: Path) -> Path | None:
    """Decode the frame at 0.1 s (the one the old tile showed); the first frame for a shorter clip."""
    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    out = Path(name)
    transferred = False
    try:
        for seek in (("-ss", "0.1"), ()):
            run = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", *seek, "-i", str(video), "-frames:v", "1",
                 "-vf", f"scale='min({THUMB_EDGE},iw)':-2", str(out)],
                capture_output=True, timeout=15, check=False,
            )
            if run.returncode == 0 and out.stat().st_size > 0:
                transferred = True
                return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if not transferred:
            out.unlink(missing_ok=True)
    return None
