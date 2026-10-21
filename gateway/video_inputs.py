"""Pure helpers for video-conditioned workflows (remix, character replace).

These workflows take a source video as input. The route needs to know the
source clip's frame rate and length before it picks the frame count, and it
needs to read nested multipart form keys that the OpenAI SDK produces for
``extra_body``. Nothing here talks to ComfyUI or FastAPI.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from gateway import private_io, private_tmp

VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")
_MIME_SUFFIX = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}
MAX_SOURCE_VIDEO_BYTES = 256 * 1024 * 1024
# Starlette caps a multipart text part at 1 MB; a data URI for a source video or
# reference image is far larger (base64 is 4/3 of the file).
MAX_FORM_PART_BYTES = MAX_SOURCE_VIDEO_BYTES * 2
_BRACKET_KEY = re.compile(r"^([^\[\]]+)((?:\[[^\[\]]*\])+)$")


@dataclass(frozen=True)
class VideoProbe:
    fps: float
    duration: float
    frames: int
    width: int
    height: int
    has_audio: bool = False


def suffix_for_video(filename: str | None, content_type: str | None = None) -> str:
    """Return a video file suffix, preferring the filename, then the MIME type."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return suffix
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return _MIME_SUFFIX.get(mime, ".mp4")


def _parse_rate(text: str | None) -> float:
    try:
        num, _, den = str(text or "").partition("/")
        value = float(num) / float(den) if den else float(num)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return value if value > 0 else 0.0


def probe_video(data: bytes, suffix: str = ".mp4", *, workdir: Path | None = None) -> VideoProbe | None:
    """Probe a video's frame rate and length with ffprobe.

    Returns None when ffprobe is missing, fails, or finds no video stream; the
    caller then falls back to the workflow's configured frame rate.
    """
    workdir = workdir if workdir is not None else private_tmp.current() / "video_inputs"
    if not private_tmp.inside(workdir):
        raise private_tmp.PrivateTmpError("Video probe workspace must stay inside the private temporary directory")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    private_io.ensure_private_dir(workdir)
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        path = Path(tmp) / f"source{suffix}"
        private_io.create_private_file(path)
        path.write_bytes(data)
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-count_frames", "-show_entries",
                    "stream=avg_frame_rate,r_frame_rate,nb_read_frames,nb_frames,duration,width,height:format=duration",
                    "-of", "json", str(path),
                ],
                capture_output=True, timeout=30, check=False,
            )
            audio = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "stream=codec_type", "-of", "csv=p=0", str(path)],
                capture_output=True, timeout=30, check=False,
            )
            has_audio = audio.returncode == 0 and b"audio" in (audio.stdout or b"")
        except (OSError, subprocess.TimeoutExpired):
            return None
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(proc.stdout or b"{}")
        stream = (info.get("streams") or [None])[0]
    except (ValueError, TypeError):
        return None
    if not isinstance(stream, Mapping):
        return None
    fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
    frames = 0
    for key in ("nb_read_frames", "nb_frames"):
        try:
            frames = int(stream.get(key) or 0)
        except (TypeError, ValueError):
            frames = 0
        if frames:
            break
    try:
        duration = float(stream.get("duration") or (info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if fps and not frames and duration:
        frames = int(duration * fps)
    if fps and frames and not duration:
        duration = frames / fps
    if not fps or not frames:
        return None
    return VideoProbe(
        fps=fps,
        duration=duration,
        frames=frames,
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        has_audio=has_audio,
    )


def snap_length_down(length: int, *, step: int, offset: int, available: int | None) -> int:
    """Largest ``step*k+offset`` that is <= ``length`` and <= ``available``."""
    limit = length if available is None else min(length, available)
    k = max(0, (limit - offset) // step)
    return k * step + offset


def parse_bracket_form(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Rebuild nested dicts from ``a[b][c]`` form keys.

    The OpenAI SDK flattens ``extra_body={"extensions": {...}}`` into
    ``extensions[com.azizam.workflow][seed]`` multipart fields. Plain keys
    pass through unchanged. A key that is both a scalar and a container is
    rejected with ValueError.
    """
    out: dict[str, Any] = {}
    # `list[][field]` (the SDK's form for a list of objects, e.g. `shots`): the
    # i-th occurrence of a field belongs to the i-th object. The SDK emits all
    # values of one field before the next field, so grouping is by occurrence.
    seen_in_list: dict[tuple[str, str], int] = {}
    for name, value in items:
        match = _BRACKET_KEY.match(name)
        if not match:
            out[name] = value
            continue
        path = [match.group(1), *re.findall(r"\[([^\[\]]*)\]", match.group(2))]
        if len(path) == 3 and path[1] == "" and path[2]:
            index = seen_in_list.get((path[0], path[2]), 0)
            seen_in_list[(path[0], path[2])] = index + 1
            items_list = out.setdefault(path[0], [])
            if not isinstance(items_list, list):
                raise ValueError(f"form field {name} conflicts with a scalar field")
            while len(items_list) <= index:
                items_list.append({})
            items_list[index][path[2]] = value
            continue
        if any(not part for part in path):
            raise ValueError(f"unsupported form field name: {name}")
        node = out
        for part in path[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"form field {name} conflicts with a scalar field")
            node = child
        if isinstance(node.get(path[-1]), dict):
            raise ValueError(f"form field {name} conflicts with a nested field")
        node[path[-1]] = value
    return out


def coerce_form_scalars(values: Mapping[str, Any], specs: Mapping[str, Any]) -> dict[str, Any]:
    """Convert string form values to the manifest's integer/number/boolean types."""
    out: dict[str, Any] = {}
    for name, value in values.items():
        spec = specs.get(name) if isinstance(specs, Mapping) else None
        kind = spec.get("type") if isinstance(spec, Mapping) else None
        if isinstance(value, str):
            try:
                if kind == "integer":
                    value = int(value)
                elif kind == "number":
                    value = float(value)
                elif kind == "boolean" and value.lower() in {"true", "false"}:
                    value = value.lower() == "true"
            except ValueError:
                pass  # leave as text; the manifest validator reports the type error
        out[name] = value
    return out
