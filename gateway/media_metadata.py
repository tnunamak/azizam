"""Lossless metadata strip / embed for generated media.

Pure helpers: no proxy, no FastAPI. Images are rewritten at the container
level (PNG chunks, JPEG marker segments, WebP RIFF chunks) so pixel data and
entropy-coded payloads stay byte-identical. WAV, FLAC and common MP3 tags
are stripped directly; other audio/video use ``ffmpeg -c copy`` (no re-encode).

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

import os
import shutil
import struct
import subprocess
import zlib
from contextlib import ExitStack, contextmanager
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
    "embed_image_metadata",
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


def png_text_chunks_from_path(path: Path | str) -> dict[str, str]:
    """``png_text_chunks`` of a file, reading only the chunk headers and the text chunks.

    Image data (IDAT) is skipped by seeking, so a 20 MB picture costs a few reads, not 20 MB.
    The result, and the error for a truncated chunk, are those of ``png_text_chunks``.
    """
    result: dict[str, str] = {}
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if handle.read(len(_PNG_SIG)) != _PNG_SIG:
            return {}
        pos = len(_PNG_SIG)
        while pos + 12 <= size:
            header = handle.read(8)
            if len(header) < 8:
                break
            length = struct.unpack(">I", header[:4])[0]
            ctype = header[4:8]
            if pos + 12 + length > size:
                raise MediaMetadataError("truncated PNG chunk")
            if ctype in (b"tEXt", b"zTXt", b"iTXt"):
                entry = _png_text_entry(ctype, handle.read(length))
                handle.seek(4, os.SEEK_CUR)
                if entry:
                    result[entry[0]] = entry[1]
            else:
                handle.seek(length + 4, os.SEEK_CUR)
            pos += 12 + length
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
            # Data after EOI is not image payload and can carry private text.
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


def _strip_wav(content: bytes) -> bytes:
    """Drop non-audio RIFF chunks without a subprocess or PCM re-encoding."""
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        raise MediaMetadataError("invalid WAV")
    end = 8 + struct.unpack("<I", content[4:8])[0]
    if end > len(content):
        raise MediaMetadataError("truncated WAV")
    pos = 12
    chunks = []
    changed = end != len(content)
    # Structural chunks can affect decoded samples or playback order.
    structural = {b"fmt ", b"data", b"fact", b"ds64", b"cue ", b"plst", b"slnt"}
    while pos < end:
        if pos + 8 > end:
            raise MediaMetadataError("truncated WAV chunk")
        tag = content[pos:pos + 4]
        size = struct.unpack("<I", content[pos + 4:pos + 8])[0]
        next_pos = pos + 8 + size + (size & 1)
        if next_pos > end:
            raise MediaMetadataError("truncated WAV chunk")
        raw = content[pos:next_pos]
        if tag in structural or (tag == b"LIST" and content[pos + 8:pos + 12] == b"wavl"):
            chunks.append(raw)
        else:
            changed = True
        pos = next_pos
    if not changed:
        return content
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _strip_flac(content: bytes) -> bytes:
    """Keep decoding/seek blocks and compressed frames; discard all text/art."""
    if not content.startswith(b"fLaC"):
        raise MediaMetadataError("invalid FLAC")
    pos, kept = 4, []
    while True:
        if pos + 4 > len(content):
            raise MediaMetadataError("truncated FLAC metadata")
        header = content[pos]
        kind = header & 0x7f
        size = int.from_bytes(content[pos + 1:pos + 4], "big")
        end = pos + 4 + size
        if end > len(content):
            raise MediaMetadataError("truncated FLAC metadata")
        if kind in {0, 1, 3}:  # STREAMINFO, PADDING, SEEKTABLE
            kept.append(bytes([kind]) + content[pos + 1:end])
        pos = end
        if header & 0x80:
            break
    if not kept or kept[0][0] != 0:
        raise MediaMetadataError("FLAC missing stream information")
    kept[-1] = bytes([kept[-1][0] | 0x80]) + kept[-1][1:]
    return b"fLaC" + b"".join(kept) + content[pos:]


def _strip_mp3(content: bytes) -> bytes | None:
    """Losslessly remove ID3; None delegates uncommon APE tags to ffmpeg."""
    start, end = 0, len(content)
    while content[start:start + 3] == b"ID3":
        if start + 10 > end or any(byte & 0x80 for byte in content[start + 6:start + 10]):
            raise MediaMetadataError("invalid ID3 header")
        header = content[start:start + 10]
        size = 0
        for byte in header[6:10]:
            size = (size << 7) | byte
        start += 10 + size + (10 if header[3] == 4 and header[5] & 0x10 else 0)
        if start > end:
            raise MediaMetadataError("truncated ID3 tag")
    if end - start >= 128 and content[end - 128:end - 125] == b"TAG":
        end -= 128
    if b"APETAGEX" in content[max(start, end - 160):end] or content[start:start + 8] == b"APETAGEX":
        return None
    if end - start < 2 or content[start] != 0xff or content[start + 1] & 0xe0 != 0xe0:
        return None  # Unknown content is not certified metadata-free.
    return content[start:end]


# ------------------------------------------------------------- ffmpeg side

_MFD_CLOEXEC = 0x0001  # linux/memfd.h


def _memfd(name: str) -> int:
    """An anonymous RAM file descriptor.

    Some Python builds omit ``os.memfd_create`` although the kernel has the
    system call, so fall back to calling libc directly.
    """
    if hasattr(os, "memfd_create"):
        return os.memfd_create(name, os.MFD_CLOEXEC)
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    create = getattr(libc, "memfd_create", None)
    if create is None:
        raise MediaMetadataError("anonymous media buffers unavailable")
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    fd = create(name.encode(), _MFD_CLOEXEC)
    if fd < 0:
        raise MediaMetadataError("anonymous media buffers unavailable")
    return fd


@contextmanager
def _media_buffers(content: bytes):
    """Anonymous RAM files: no prompt-bearing pathname, even after SIGKILL.

    The gateway runs on Linux. Fail closed if memfd is unavailable; a disk
    fallback would violate the forgotten-request storage contract.
    """
    with ExitStack() as stack:
        src = os.fdopen(_memfd("azizam-media-input"), "w+b")
        stack.enter_context(src)
        dst = os.fdopen(_memfd("azizam-media-output"), "w+b")
        stack.enter_context(dst)
        src.write(content)
        src.flush()
        src.seek(0)
        yield src, dst


def _run(cmd: list[str], *, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=600, pass_fds=pass_fds)
    except OSError as exc:
        raise MediaMetadataError("media tool unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaMetadataError("media tool timed out") from exc


def _ffmpeg_rewrite(content: bytes, suffix_or_mime: str, workdir: Path | None,
                    extra_out: list[str]) -> bytes:
    suffix = _suffix(suffix_or_mime)
    muxer = _MUXER.get(suffix)
    if muxer is None:
        raise MediaMetadataError(f"no muxer for {suffix_or_mime!r}")
    if shutil.which("ffmpeg") is None:
        raise MediaMetadataError("ffmpeg not found")
    # Retain the optional workdir API, but never put media bytes in it.
    if workdir is not None:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    with _media_buffers(content) as (src, dst):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", f"/proc/self/fd/{src.fileno()}",
               "-map", "0", *extra_out, "-c", "copy", "-fflags", "+bitexact",
               "-f", muxer, f"/proc/self/fd/{dst.fileno()}"]
        proc = _run(cmd, pass_fds=(src.fileno(), dst.fileno()))
        if proc.returncode != 0 or os.fstat(dst.fileno()).st_size == 0:
            # Tool stderr can echo an embedded prompt; keep it out of logs/errors.
            raise MediaMetadataError(f"ffmpeg failed ({proc.returncode})")
        dst.seek(0)
        return dst.read()


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
    if kind == "audio":
        suffix = _suffix(suffix_or_mime)
        if suffix == ".wav":
            return _strip_wav(content)
        if suffix == ".flac":
            return _strip_flac(content)
        if suffix == ".mp3":
            clean = _strip_mp3(content)
            if clean is not None:
                return clean
    if kind in ("video", "audio"):
        if _suffix(suffix_or_mime) not in _MUXER:
            return content
        return _ffmpeg_rewrite(
            content, suffix_or_mime, workdir,
            ["-map_metadata", "-1", "-map_chapters", "-1",
             "-map_metadata:s", "-1"],
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


def embed_image_metadata(content: bytes, suffix_or_mime: str,
                         entries: Mapping[str, str]) -> bytes:
    """Restore maker metadata without recompressing JPEG/WebP image payloads."""
    kind = media_kind(suffix_or_mime)
    if not entries:
        return content
    if kind == "png":
        return embed_png_text(content, entries)
    blob = exif_bytes(entries)
    if kind == "jpeg":
        clean = _strip_jpeg(content)
        if not clean.startswith(b"\xff\xd8"):
            raise MediaMetadataError("invalid JPEG")
        if len(blob) + 2 > 65535:
            raise MediaMetadataError("JPEG metadata exceeds segment limit")
        return clean[:2] + b"\xff\xe1" + struct.pack(">H", len(blob) + 2) + blob + clean[2:]
    if kind == "webp":
        clean = _strip_webp(content)
        if len(clean) < 12 or clean[:4] != b"RIFF" or clean[8:12] != b"WEBP":
            raise MediaMetadataError("invalid WebP")
        chunks = clean[12:]
        if chunks[:4] == b"VP8X":
            chunks = chunks[:8] + bytes([chunks[8] | 0x08]) + chunks[9:]
        else:
            # A basic VP8/VP8L file needs the extended header to advertise EXIF.
            # Pillow only reads dimensions; it never saves/re-encodes pixels.
            from io import BytesIO
            from PIL import Image
            with Image.open(BytesIO(clean)) as image:
                width, height = image.size
                alpha = "A" in image.getbands()
            flags = 0x08 | (0x10 if alpha else 0)
            header = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
            chunks = b"VP8X" + struct.pack("<I", 10) + header + chunks
        payload = blob.removeprefix(b"Exif\x00\x00")
        chunks += b"EXIF" + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")
        body = b"WEBP" + chunks
        return b"RIFF" + struct.pack("<I", len(body)) + body
    return content


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
    if workdir is not None:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    with _media_buffers(content) as (src, _dst):
        proc = _run(["ffprobe", "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", f"/proc/self/fd/{src.fileno()}"],
                    pass_fds=(src.fileno(),))
        if proc.returncode != 0:
            raise MediaMetadataError(f"ffprobe failed ({proc.returncode})")
        data = json.loads(proc.stdout or b"{}")
    merged: dict[str, str] = {}
    for stream in data.get("streams", []):
        for k, v in (stream.get("tags") or {}).items():
            merged[k.lower()] = v
    for k, v in ((data.get("format") or {}).get("tags") or {}).items():
        merged[k.lower()] = v
    return merged
