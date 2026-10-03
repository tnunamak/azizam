"""Lossless metadata strip / embed for generated media.

Pure helpers: no proxy, no FastAPI. Images are rewritten at the container
level (PNG chunks, JPEG marker segments, WebP RIFF chunks) so pixel data and
entropy-coded payloads stay byte-identical. Audio/video use ``ffmpeg -c copy``
(stream copy, no re-encode).

Container findings for ``embed_media_tags`` (verified with ffprobe, ffmpeg 8):

* mp4 / mov / m4a: arbitrary keys survive ONLY with ``-movflags
  +use_metadata_tags`` (without it ffmpeg keeps only iTunes-known keys and
  silently drops the rest). With the flag, custom keys and a ~30 KB value
  round-trip, and existing keys (``prompt``/``workflow``) are preserved.
* webm / mkv: arbitrary keys round-trip (Matroska SimpleTag); large values OK.
* flac: arbitrary Vorbis-comment keys round-trip (keys are case-insensitive
  and come back lower-cased through ffprobe); a 30 KB value fits
  (metadata block limit is 16 MB).
* ogg: Vorbis-comment style, arbitrary keys survive.
* wav: only the RIFF INFO keys survive (artist, comment, title, ...). Custom
  keys are dropped, and values are limited; ``embed_media_tags`` therefore
  writes the whole set of tags as ``key=value`` lines into ``comment``.
* mp3: ID3v2 TXXX frames hold custom keys, so arbitrary keys survive.
  A 30 KB value fits (ID3v2.4 frames up to 256 MB).

Where a container cannot hold a key, the fallback is described per function.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import uuid
import zlib
from pathlib import Path
from typing import Mapping

__all__ = [
    "MediaMetadataError",
    "media_kind",
    "strip_metadata",
    "embed_png_text",
    "copy_png_text",
    "png_text_chunks",
    "exif_bytes",
    "embed_media_tags",
    "read_media_tags",
]


class MediaMetadataError(RuntimeError):
    """Raised when media metadata cannot be stripped/embedded safely."""


_SUFFIX_KIND = {
    ".png": "png",
    ".jpg": "jpeg", ".jpeg": "jpeg", ".jpe": "jpeg", ".jfif": "jpeg",
    ".webp": "webp",
    ".mp4": "video", ".m4v": "video", ".mov": "video", ".webm": "video",
    ".mkv": "video", ".avi": "video", ".gif": None,
    ".wav": "audio", ".flac": "audio", ".mp3": "audio", ".ogg": "audio",
    ".oga": "audio", ".opus": "audio", ".m4a": "audio", ".aac": "audio",
    ".mka": "audio",
}
_MIME_KIND = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
    "image/webp": "webp",
    "video/mp4": "video", "video/webm": "video", "video/quicktime": "video",
    "video/x-matroska": "video", "video/x-msvideo": "video",
    "audio/wav": "audio", "audio/x-wav": "audio", "audio/wave": "audio",
    "audio/vnd.wave": "audio", "audio/flac": "audio", "audio/x-flac": "audio",
    "audio/mpeg": "audio", "audio/mp3": "audio", "audio/ogg": "audio",
    "audio/opus": "audio", "audio/mp4": "audio", "audio/x-m4a": "audio",
    "audio/m4a": "audio", "audio/aac": "audio", "audio/x-matroska": "audio",
}
# suffix -> ffmpeg muxer
_MUXER = {
    ".mp4": "mp4", ".m4v": "mp4", ".m4a": "mp4", ".mov": "mov",
    ".webm": "webm", ".mkv": "matroska", ".mka": "matroska", ".avi": "avi",
    ".wav": "wav", ".flac": "flac", ".mp3": "mp3", ".ogg": "ogg",
    ".oga": "ogg", ".opus": "ogg", ".aac": "adts",
}
_MIME_SUFFIX = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/vnd.wave": ".wav", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg",
    "audio/opus": ".ogg", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/x-matroska": ".mka",
}


def _norm(suffix_or_mime: str) -> str:
    return (suffix_or_mime or "").strip().lower().split(";")[0].strip()


def media_kind(suffix_or_mime: str) -> str | None:
    """Return png/jpeg/webp/video/audio, or None when unknown."""
    key = _norm(suffix_or_mime)
    if not key:
        return None
    if "/" in key:
        return _MIME_KIND.get(key)
    if not key.startswith("."):
        key = "." + key
    return _SUFFIX_KIND.get(key)


def _suffix(suffix_or_mime: str) -> str:
    key = _norm(suffix_or_mime)
    if "/" in key:
        return _MIME_SUFFIX.get(key, "")
    return key if key.startswith(".") else "." + key


# --------------------------------------------------------------------- PNG

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


def _png_chunks(content: bytes):
    """Yield (type, data, raw_chunk_bytes). Stops quietly on truncation."""
    pos = len(_PNG_SIG)
    n = len(content)
    while pos + 12 <= n:
        length = struct.unpack(">I", content[pos:pos + 4])[0]
        end = pos + 12 + length
        if end > n:
            raise MediaMetadataError("truncated PNG chunk")
        yield content[pos + 4:pos + 8], content[pos + 8:pos + 8 + length], content[pos:end]
        pos = end


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _strip_png(content: bytes) -> bytes:
    if not content.startswith(_PNG_SIG):
        return content
    out = [_PNG_SIG]
    for ctype, _data, raw in _png_chunks(content):
        if ctype not in _PNG_DROP:
            out.append(raw)
    return b"".join(out)


def _png_text_entry(ctype: bytes, data: bytes) -> tuple[str, str] | None:
    try:
        if ctype == b"tEXt":
            key, _, val = data.partition(b"\x00")
            return key.decode("latin-1"), val.decode("latin-1")
        if ctype == b"zTXt":
            key, _, rest = data.partition(b"\x00")
            return key.decode("latin-1"), zlib.decompress(rest[1:]).decode("latin-1")
        if ctype == b"iTXt":
            key, _, rest = data.partition(b"\x00")
            flag, _method = rest[0], rest[1]
            rest = rest[2:]
            _lang, _, rest = rest.partition(b"\x00")
            _tkey, _, text = rest.partition(b"\x00")
            if flag:
                text = zlib.decompress(text)
            return key.decode("latin-1"), text.decode("utf-8")
    except (ValueError, zlib.error, IndexError, UnicodeDecodeError):
        return None
    return None


def png_text_chunks(content: bytes) -> dict[str, str]:
    """Read all tEXt/zTXt/iTXt chunks into a dict (later duplicates win)."""
    if not content.startswith(_PNG_SIG):
        return {}
    result: dict[str, str] = {}
    for ctype, data, _raw in _png_chunks(content):
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            entry = _png_text_entry(ctype, data)
            if entry:
                result[entry[0]] = entry[1]
    return result


def _make_png_text_chunk(key: str, value: str) -> bytes:
    try:
        kw = key.encode("latin-1")
        val = value.encode("latin-1")
        if b"\x00" in kw or not (1 <= len(kw) <= 79):
            raise ValueError
        if b"\x00" in val:
            raise ValueError
        return _png_chunk(b"tEXt", kw + b"\x00" + val)
    except (UnicodeEncodeError, ValueError):
        kw = key.encode("latin-1", "replace")[:79].replace(b"\x00", b"?")
        if not kw:
            raise MediaMetadataError("empty PNG text keyword")
        return _png_chunk(b"iTXt", kw + b"\x00\x00\x00\x00\x00" + value.encode("utf-8"))


def embed_png_text(content: bytes, entries: Mapping[str, str]) -> bytes:
    """Insert text chunks before the first IDAT, replacing same-keyword ones."""
    if not content.startswith(_PNG_SIG) or not entries:
        return content
    wanted = set(entries)
    out = [_PNG_SIG]
    new_chunks = b"".join(_make_png_text_chunk(k, v) for k, v in entries.items())
    inserted = False
    for ctype, data, raw in _png_chunks(content):
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            entry = _png_text_entry(ctype, data)
            if entry and entry[0] in wanted:
                continue
        if ctype == b"IDAT" and not inserted:
            out.append(new_chunks)
            inserted = True
        out.append(raw)
    if not inserted:
        raise MediaMetadataError("PNG has no IDAT chunk")
    return b"".join(out)


def copy_png_text(source: bytes, target: bytes) -> bytes:
    """Copy the text chunks of PNG ``source`` into PNG ``target``.

    For gateway steps that re-encode a ComfyUI PNG (mask composite, tile offset):
    Pillow drops ``prompt`` and ``workflow`` on save, so carry them across.
    """
    return embed_png_text(target, png_text_chunks(source))


# -------------------------------------------------------------------- JPEG

_JPEG_DROP = {0xE1, 0xED, 0xFE}  # APP1 (EXIF/XMP), APP13 (IPTC), COM


def _strip_jpeg(content: bytes) -> bytes:
    if content[:2] != b"\xff\xd8":
        return content
    n = len(content)
    out = bytearray(b"\xff\xd8")
    pos = 2
    while pos < n:
        if content[pos] != 0xFF:
            raise MediaMetadataError("malformed JPEG: expected marker")
        while pos < n and content[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= n:
            break
        marker = content[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            out += b"\xff\xd9"
            out += content[pos:]  # trailing bytes kept as is
            return bytes(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone
            out += bytes([0xFF, marker])
            continue
        if pos + 2 > n:
            raise MediaMetadataError("truncated JPEG segment")
        seglen = struct.unpack(">H", content[pos:pos + 2])[0]
        end = pos + seglen
        if seglen < 2 or end > n:
            raise MediaMetadataError("truncated JPEG segment")
        if marker not in _JPEG_DROP:
            out += bytes([0xFF, marker]) + content[pos:end]
        pos = end
        if marker == 0xDA:  # SOS: copy entropy-coded data verbatim
            start = pos
            while pos < n - 1:
                if content[pos] == 0xFF and content[pos + 1] not in (0x00, 0xFF) \
                        and not (0xD0 <= content[pos + 1] <= 0xD7):
                    break
                pos += 1
            else:
                pos = n
            out += content[start:pos]
    return bytes(out)


# -------------------------------------------------------------------- WebP

def _strip_webp(content: bytes) -> bytes:
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        return content
    riff_size = struct.unpack("<I", content[4:8])[0]
    end = min(len(content), 8 + riff_size)
    pos = 12
    chunks: list[bytes] = []
    while pos + 8 <= end:
        fourcc = content[pos:pos + 4]
        size = struct.unpack("<I", content[pos + 4:pos + 8])[0]
        total = 8 + size + (size & 1)
        if pos + 8 + size > end:
            raise MediaMetadataError("truncated WebP chunk")
        raw = content[pos:pos + 8 + size]
        if size & 1:
            raw += b"\x00"
        pos += total
        if fourcc in (b"EXIF", b"XMP "):
            continue
        if fourcc == b"VP8X" and size >= 10:
            raw = raw[:8] + bytes([raw[8] & ~0x0C & 0xFF]) + raw[9:]
        chunks.append(raw)
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ------------------------------------------------------------- ffmpeg side

def _workdir(workdir: Path | None) -> Path:
    path = Path(workdir) if workdir is not None else Path.home() / ".tmp" / "media_metadata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=600)
    except FileNotFoundError as exc:
        raise MediaMetadataError(f"{cmd[0]} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaMetadataError(f"{cmd[0]} timed out") from exc


def _ffmpeg_rewrite(content: bytes, suffix_or_mime: str, workdir: Path | None,
                    extra_out: list[str]) -> bytes:
    suffix = _suffix(suffix_or_mime)
    muxer = _MUXER.get(suffix)
    if muxer is None:
        raise MediaMetadataError(f"no muxer for {suffix_or_mime!r}")
    if shutil.which("ffmpeg") is None:
        raise MediaMetadataError("ffmpeg not found")
    wd = _workdir(workdir)
    token = uuid.uuid4().hex
    src = wd / f"{token}.in{suffix}"
    dst = wd / f"{token}.out{suffix}"
    try:
        src.write_bytes(content)
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
               "-map", "0", *extra_out, "-c", "copy", "-fflags", "+bitexact",
               "-f", muxer, str(dst)]
        proc = _run(cmd)
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            err = proc.stderr.decode("utf-8", "replace")[-500:]
            raise MediaMetadataError(f"ffmpeg failed ({proc.returncode}): {err}")
        return dst.read_bytes()
    finally:
        for p in (src, dst):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def strip_metadata(content: bytes, suffix_or_mime: str, *, workdir: Path | None = None) -> bytes:
    """Remove metadata without touching pixel/stream payloads.

    Unknown kinds are returned unchanged. ffmpeg failures raise
    ``MediaMetadataError`` (never silently returns bytes still carrying tags).
    """
    kind = media_kind(suffix_or_mime)
    if kind == "png":
        return _strip_png(content)
    if kind == "jpeg":
        return _strip_jpeg(content)
    if kind == "webp":
        return _strip_webp(content)
    if kind in ("video", "audio"):
        if _suffix(suffix_or_mime) not in _MUXER:
            return content
        return _ffmpeg_rewrite(
            content, suffix_or_mime, workdir,
            ["-map_metadata", "-1", "-map_chapters", "-1",
             "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1"],
        )
    return content


# -------------------------------------------------------------------- EXIF

def exif_bytes(entries: Mapping[str, str]) -> bytes:
    """Build an EXIF blob for Pillow ``exif=`` using ComfyUI's convention.

    ``workflow`` -> Make (0x010F) as ``workflow:<json>``; ``prompt`` -> Model
    (0x0110) as ``prompt:<json>``; every other key ``k`` -> ImageDescription
    (0x010E) as ``k:<value>`` (several are joined with newlines).
    """
    from PIL import Image

    exif = Image.Exif()
    other: list[str] = []
    for key, value in entries.items():
        if key == "workflow":
            exif[0x010F] = f"workflow:{value}"
        elif key == "prompt":
            exif[0x0110] = f"prompt:{value}"
        else:
            other.append(f"{key}:{value}")
    if other:
        exif[0x010E] = "\n".join(other)
    return exif.tobytes()


# ------------------------------------------------------------ embed / read

_MP4_LIKE = {".mp4", ".m4v", ".m4a", ".mov"}
_WAV_INFO_FALLBACK_KEY = "comment"


def embed_media_tags(content: bytes, suffix_or_mime: str, tags: Mapping[str, str],
                     *, workdir: Path | None = None) -> bytes:
    """Add global container tags, preserving existing ones.

    png -> ``embed_png_text``; jpeg/webp -> unchanged; video/audio -> ffmpeg
    stream copy with ``-metadata k=v`` (mp4/mov/m4a add ``-movflags
    +use_metadata_tags`` so custom keys survive). WAV cannot hold custom keys
    (only RIFF INFO), so all tags are written as ``key=value`` lines into the
    ``comment`` field. See module docstring for the per-container findings.
    """
    kind = media_kind(suffix_or_mime)
    if kind == "png":
        return embed_png_text(content, tags)
    if kind not in ("video", "audio") or not tags:
        return content
    suffix = _suffix(suffix_or_mime)
    if suffix not in _MUXER:
        return content
    extra: list[str] = []
    if suffix == ".wav":
        joined = "\n".join(f"{k}={v}" for k, v in tags.items())
        extra += ["-metadata", f"{_WAV_INFO_FALLBACK_KEY}={joined}"]
    else:
        for k, v in tags.items():
            extra += ["-metadata", f"{k}={v}"]
    if suffix in _MP4_LIKE:
        extra += ["-movflags", "+use_metadata_tags"]
    return _ffmpeg_rewrite(content, suffix_or_mime, workdir, extra)


def read_media_tags(content: bytes, suffix_or_mime: str, *,
                    workdir: Path | None = None) -> dict[str, str]:
    """ffprobe format + stream tags merged, keys lower-cased."""
    import json

    if shutil.which("ffprobe") is None:
        raise MediaMetadataError("ffprobe not found")
    suffix = _suffix(suffix_or_mime)
    wd = _workdir(workdir)
    path = wd / f"{uuid.uuid4().hex}.probe{suffix}"
    try:
        path.write_bytes(content)
        proc = _run(["ffprobe", "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(path)])
        if proc.returncode != 0:
            raise MediaMetadataError(proc.stderr.decode("utf-8", "replace")[-500:])
        data = json.loads(proc.stdout or b"{}")
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    merged: dict[str, str] = {}
    for stream in data.get("streams", []):
        for k, v in (stream.get("tags") or {}).items():
            merged[k.lower()] = v
    for k, v in ((data.get("format") or {}).get("tags") or {}).items():
        merged[k.lower()] = v
    return merged
