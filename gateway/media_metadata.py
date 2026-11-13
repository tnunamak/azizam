"""Lossless metadata strip / embed for generated media.

Pure helpers: no proxy, no FastAPI. Images are rewritten at the container
level (PNG chunks, JPEG markers, WebP RIFF chunks and GIF blocks). GLB JSON
is rewritten while its BIN chunk stays byte-identical. WAV, FLAC and common
MP3 tags are stripped directly; other audio/video use ``ffmpeg -c copy``.
Video user-data units are removed without re-encoding; HEVC codec headers
are rebuilt through a transport-stream intermediate.

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

import json
import os
import shutil
import struct
import subprocess
import zlib
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Mapping

import yaml

from gateway.strict_yaml import safe_load_strict

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


_FORMAT_ROWS_PATH = Path(__file__).resolve().parents[1] / "registry" / "media_metadata_formats.yml"
# Closed set of stripping algorithms and explicit metadata-free content rows.
_HANDLERS = frozenset({"png", "jpeg", "webp", "gif", "glb", "content", "audio", "video"})


def load_media_metadata_formats(
    path: Path | str = _FORMAT_ROWS_PATH,
) -> tuple[dict[str, str | None], dict[str, str | None], dict[str, str], dict[str, str]]:
    """Load container rows; a muxer's first suffix is its MIME processing suffix."""
    source = Path(path)

    def fail(where: str, message: str) -> None:
        raise ValueError(f"{source}: {where}: {message}")

    def fields(value, required: set[str], optional: set[str], where: str) -> None:
        if not isinstance(value, dict):
            fail(where, "must be a mapping")
        missing = required - value.keys()
        unknown = value.keys() - required - optional
        if missing:
            fail(where, f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            fail(where, f"unknown fields: {', '.join(sorted(map(str, unknown)))}")

    def token(value, where: str, field: str) -> str:
        if (
            not isinstance(value, str) or not value
            or value != value.strip().lower() or any(char.isspace() for char in value)
        ):
            fail(where, f"{field} must be a nonempty lowercase token without whitespace")
        return value

    def tokens(row, field: str, where: str) -> list[str]:
        values = row[field]
        if not isinstance(values, list):
            fail(where, f"{field} must be a list")
        return [token(value, where, field) for value in values]

    try:
        document = safe_load_strict(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        fail("document", f"could not read format rows: {error}")
    fields(document, {"schema_version", "formats"}, set(), "document")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        fail("document", "schema_version must be 1")
    if not isinstance(document["formats"], list):
        fail("document", "formats must be a list")

    suffix_kind: dict[str, str | None] = {}
    mime_kind: dict[str, str | None] = {}
    muxers: dict[str, str] = {}
    mime_suffix: dict[str, str] = {}
    for number, row in enumerate(document["formats"], start=1):
        where = f"formats row {number}"
        fields(row, {"handler", "suffixes", "mime_types"}, {"muxer"}, where)
        handler = row["handler"]
        if handler is not None:
            handler = token(handler, where, "handler")
            if handler not in _HANDLERS:
                fail(where, f"handler must be null or one of {', '.join(sorted(_HANDLERS))}: {handler!r}")
        suffixes = tokens(row, "suffixes", where)
        mime_types = tokens(row, "mime_types", where)
        for suffix in suffixes:
            if not suffix.startswith(".") or len(suffix) == 1 or "/" in suffix or ";" in suffix:
                fail(where, f"invalid suffix: {suffix!r}")
        for mime_type in mime_types:
            if mime_type.count("/") != 1 or not all(mime_type.split("/")) or ";" in mime_type:
                fail(where, f"invalid MIME type: {mime_type!r}")
        muxer = token(row["muxer"], where, "muxer") if "muxer" in row else None
        if handler in {"audio", "video"} and muxer is None:
            fail(where, "stream handler requires a muxer")
        if muxer is not None and not suffixes:
            fail(where, "muxer requires at least one suffix")
        if not suffixes and not mime_types:
            fail(where, "at least one suffix or MIME type is required")
        for suffix in suffixes:
            if suffix in suffix_kind:
                fail(where, f"duplicate suffix: {suffix!r}")
            suffix_kind[suffix] = handler
            if muxer is not None:
                muxers[suffix] = muxer
        for mime_type in mime_types:
            if mime_type in mime_kind:
                fail(where, f"duplicate MIME type: {mime_type!r}")
            mime_kind[mime_type] = handler
            if muxer is not None:
                mime_suffix[mime_type] = suffixes[0]
    return suffix_kind, mime_kind, muxers, mime_suffix


_SUFFIX_KIND, _MIME_KIND, _MUXER, _MIME_SUFFIX = load_media_metadata_formats()


def _norm(suffix_or_mime: str) -> str:
    return (suffix_or_mime or "").strip().lower().split(";")[0].strip()


def media_kind(suffix_or_mime: str) -> str | None:
    """Return the declared stripping handler, or None when unknown/refused."""
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
        raise MediaMetadataError("invalid PNG")
    out = [_PNG_SIG]
    structural = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA",
                  b"iCCP", b"sBIT", b"sRGB", b"bKGD", b"hIST", b"pHYs", b"sPLT",
                  b"acTL", b"fcTL", b"fdAT", b"cICP", b"mDCV", b"cLLI"}
    for ctype, _data, raw in _png_chunks(content):
        if ctype in structural:
            out.append(raw)
        if ctype == b"IEND":
            return b"".join(out)
    raise MediaMetadataError("PNG missing IEND")


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

def _jpeg_keeps(marker: int, payload: bytes) -> bool:
    if marker == 0xFE:
        return False
    if 0xE0 <= marker <= 0xEF:
        return ((marker == 0xE0 and payload.startswith((b"JFIF\x00", b"JFXX\x00")))
                or (marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"))
                or (marker == 0xEE and payload.startswith(b"Adobe")))
    return True


def _strip_jpeg(content: bytes) -> bytes:
    if content[:2] != b"\xff\xd8":
        raise MediaMetadataError("invalid JPEG")
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
        if _jpeg_keeps(marker, content[pos + 2:end]):
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
    raise MediaMetadataError("JPEG missing EOI")


# -------------------------------------------------------------------- WebP

def _strip_webp(content: bytes) -> bytes:
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        raise MediaMetadataError("invalid WebP")
    riff_size = struct.unpack("<I", content[4:8])[0]
    end = 8 + riff_size
    if end > len(content):
        raise MediaMetadataError("truncated WebP")
    pos = 12
    chunks: list[bytes] = []
    while pos < end:
        if pos + 8 > end:
            raise MediaMetadataError("truncated WebP header")
        fourcc = content[pos:pos + 4]
        size = struct.unpack("<I", content[pos + 4:pos + 8])[0]
        total = 8 + size + (size & 1)
        if pos + total > end:
            raise MediaMetadataError("truncated WebP chunk")
        raw = content[pos:pos + 8 + size]
        if size & 1:
            raw += b"\x00"
        pos += total
        if fourcc not in {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}:
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
                    extra_out: list[str], metadata_file: bytes | None = None,
                    *, output_muxer: str | None = None) -> bytes:
    suffix = _suffix(suffix_or_mime)
    muxer = output_muxer or _MUXER.get(suffix)
    if muxer is None:
        raise MediaMetadataError(f"no muxer for {suffix_or_mime!r}")
    if shutil.which("ffmpeg") is None:
        raise MediaMetadataError("ffmpeg not found")
    # Retain the optional workdir API, but never put media bytes in it.
    if workdir is not None:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        src, dst = stack.enter_context(_media_buffers(content))
        meta_in: list[str] = []
        fds = (src.fileno(), dst.fileno())
        if metadata_file is not None:
            meta = os.fdopen(_memfd("azizam-media-metadata"), "w+b")
            stack.enter_context(meta)
            meta.write(metadata_file)
            meta.flush()
            meta.seek(0)
            meta_in = ["-f", "ffmetadata", "-i", f"/proc/self/fd/{meta.fileno()}"]
            fds += (meta.fileno(),)
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", f"/proc/self/fd/{src.fileno()}", *meta_in,
               "-map", "0", *extra_out, "-c", "copy", "-fflags", "+bitexact",
               "-f", muxer, f"/proc/self/fd/{dst.fileno()}"]
        proc = _run(cmd, pass_fds=fds)
        if proc.returncode != 0 or os.fstat(dst.fileno()).st_size == 0:
            # Tool stderr can echo an embedded prompt; keep it out of logs/errors.
            raise MediaMetadataError(f"ffmpeg failed ({proc.returncode})")
        dst.seek(0)
        return dst.read()


def strip_audio(content: bytes, suffix_or_mime: str, *, workdir: Path | None = None) -> bytes:
    """Remove every audio stream from a video without re-encoding the picture.

    A stream copy keeps the video frames byte-for-byte. Non-video kinds and
    containers ffmpeg cannot mux are returned unchanged. ffmpeg failures raise
    ``MediaMetadataError``: a caller that asked for no audio must not receive
    bytes that still carry it.
    """
    if media_kind(suffix_or_mime) != "video":
        return content
    if _suffix(suffix_or_mime) not in _MUXER:
        return content
    return _ffmpeg_rewrite(content, suffix_or_mime, workdir, ["-an"])


def sniff_metadata_type(content: bytes) -> str | None:
    """Identify a container from its bytes, never from a filename or MIME hint."""
    for signature, kind in ((_PNG_SIG, "png"), (b"\xff\xd8", "jpeg"),
                            (b"GIF87a", "gif"), (b"GIF89a", "gif"),
                            (b"glTF", "glb"), (b"fLaC", "flac"),
                            (b"OggS", "ogg"), (b"ID3", "mp3"),
                            (b"\x1a\x45\xdf\xa3", "ebml")):
        if content.startswith(signature):
            return kind
    if len(content) >= 12 and content[:4] == b"RIFF":
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(content[8:12])
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return "bmff"
    if len(content) >= 2 and content[0] == 0xff:
        if content[1] & 0xf6 == 0xf0:
            return "adts"
        if content[1] & 0xe0 == 0xe0 and content[1] & 0x06:
            return "mp3"
    return None


def _strip_gif(content: bytes) -> bytes:
    """Keep rendering blocks and animation loops; discard descriptive extensions."""
    if len(content) < 13 or content[:6] not in (b"GIF87a", b"GIF89a"):
        raise MediaMetadataError("invalid GIF")
    pos = 13 + (3 * (2 ** ((content[10] & 7) + 1)) if content[10] & 0x80 else 0)
    if pos > len(content):
        raise MediaMetadataError("truncated GIF colour table")
    out = bytearray(content[:pos])

    def subblocks(start: int) -> int:
        while start < len(content):
            size = content[start]
            start += 1
            if start + size > len(content):
                break
            start += size
            if size == 0:
                return start
        raise MediaMetadataError("truncated GIF sub-block")

    while pos < len(content):
        start = pos
        tag = content[pos]
        pos += 1
        if tag == 0x3b:
            return bytes(out) + b"\x3b"
        if tag == 0x2c:
            if pos + 9 > len(content):
                raise MediaMetadataError("truncated GIF image descriptor")
            packed = content[pos + 8]
            pos += 9 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
            if pos >= len(content):
                raise MediaMetadataError("truncated GIF image data")
            pos = subblocks(pos + 1)  # LZW minimum code size
            out += content[start:pos]
        elif tag == 0x21:
            if pos >= len(content):
                raise MediaMetadataError("truncated GIF extension")
            label = content[pos]
            pos += 1
            block = pos
            pos = subblocks(pos)
            if label == 0xf9:
                if content[block] != 4 or pos - block != 6:
                    raise MediaMetadataError("invalid GIF graphic control")
                out += content[start:pos]
            elif label == 0xff and content[block:block + 12] in (
                b"\x0bNETSCAPE2.0", b"\x0bANIMEXTS1.0"
            ):
                # Only the fixed loop-count block is rendering information.
                if pos - block != 17 or content[block + 12:block + 14] != b"\x03\x01":
                    raise MediaMetadataError("invalid GIF loop extension")
                out += content[start:pos]
        else:
            raise MediaMetadataError("invalid GIF block")
    raise MediaMetadataError("GIF missing trailer")


def _strip_glb(content: bytes) -> bytes:
    if len(content) < 12 or content[:4] != b"glTF":
        raise MediaMetadataError("invalid GLB")
    version, total = struct.unpack_from("<II", content, 4)
    if version != 2 or total != len(content):
        raise MediaMetadataError("invalid GLB header")
    pos, chunks = 12, []
    def without_extras(value):
        if isinstance(value, dict):
            return {key: without_extras(item) for key, item in value.items() if key != "extras"}
        if isinstance(value, list):
            return [without_extras(item) for item in value]
        return value
    while pos < total:
        if pos + 8 > total:
            raise MediaMetadataError("truncated GLB chunk")
        size, kind = struct.unpack_from("<I4s", content, pos)
        pos += 8
        if size % 4 or pos + size > total:
            raise MediaMetadataError("invalid GLB chunk length")
        payload = content[pos:pos + size]
        pos += size
        if not chunks and kind == b"JSON":
            try:
                value = without_extras(json.loads(payload))
                asset = value["asset"]
                if not isinstance(asset, dict) or asset.get("version") != "2.0":
                    raise ValueError("invalid asset")
                asset.pop("generator", None)
                asset.pop("copyright", None)
                payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
                raise MediaMetadataError("invalid GLB JSON") from exc
            payload += b" " * (-len(payload) % 4)
        elif len(chunks) != 1 or kind != b"BIN\x00":
            raise MediaMetadataError("unsupported GLB chunk")
        chunks.append(struct.pack("<I4s", len(payload), kind) + payload)
    if not chunks:
        raise MediaMetadataError("GLB missing JSON")
    body = b"".join(chunks)
    return b"glTF" + struct.pack("<II", 2, len(body) + 12) + body


def _probe_streams(content: bytes) -> list[dict[str, str]]:
    if shutil.which("ffprobe") is None:
        raise MediaMetadataError("ffprobe not found")
    with _media_buffers(content) as (src, _dst):
        probe = _run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json",
                      f"/proc/self/fd/{src.fileno()}"], pass_fds=(src.fileno(),))
    try:
        if probe.returncode:
            raise ValueError("probe failed")
        streams = json.loads(probe.stdout)["streams"]
        if not isinstance(streams, list) or not streams or any(
            not isinstance(stream, dict) or not isinstance(stream.get("codec_type"), str)
            or not isinstance(stream.get("codec_name"), str) for stream in streams
        ):
            raise ValueError("invalid streams")
        return streams
    except (ValueError, KeyError, TypeError) as exc:
        raise MediaMetadataError("could not probe media codecs") from exc


def _codec_strip_options(streams: list[dict[str, str]]) -> tuple[list[str], bool]:
    options = []
    index = 0
    rebuild_headers = False
    for stream in streams:
        kind = stream["codec_type"]
        if kind == "audio":
            continue
        if kind != "video":
            raise MediaMetadataError("unsupported ancillary stream")
        codec = stream["codec_name"]
        units = {"h264": "6", "hevc": "39|40", "av1": "5"}.get(codec)
        if units:
            filters = f"filter_units=remove_types={units}"
            if codec == "hevc":
                # filter_units leaves hvcC extradata untouched. Annex B through a
                # transport stream rebuilds it from the filtered parameter sets.
                filters = "hevc_mp4toannexb," + filters
                rebuild_headers = True
            options += [f"-bsf:v:{index}", filters]
        elif codec not in {"vp8", "vp9"}:
            raise MediaMetadataError("unsupported video codec")
        index += 1
    return options, rebuild_headers


def strip_metadata(content: bytes, suffix_or_mime: str, *, workdir: Path | None = None) -> bytes:
    """Strip by actual bytes; refuse unknown or contradictory container declarations."""
    actual = sniff_metadata_type(content)
    handlers = {"png": _strip_png, "jpeg": _strip_jpeg, "webp": _strip_webp,
                "gif": _strip_gif, "glb": _strip_glb}
    if actual in handlers:
        return handlers[actual](content)
    kind = media_kind(suffix_or_mime)
    if kind == "content" and actual is None:
        return content  # Explicit metadata-free format row (headerless PCM).
    if kind not in ("audio", "video"):
        raise MediaMetadataError("unsupported media metadata format")
    suffix = _suffix(suffix_or_mime)
    muxer = _MUXER.get(suffix)
    families = {"mp4": "bmff", "mov": "bmff", "webm": "ebml", "matroska": "ebml",
                "avi": "avi", "wav": "wav", "flac": "flac", "mp3": "mp3", "ogg": "ogg", "adts": "adts"}
    if muxer is None or actual is None or families.get(muxer) != actual:
        raise MediaMetadataError("unknown or contradictory media container")
    if actual == "wav":
        return _strip_wav(content)
    if actual == "flac":
        return _strip_flac(content)
    if actual == "mp3":
        clean = _strip_mp3(content)
        if clean is not None:
            return clean
    if shutil.which("ffmpeg") is None:
        raise MediaMetadataError("ffmpeg not found")
    streams = _probe_streams(content)
    filters, rebuild_headers = _codec_strip_options(streams)
    options = ["-map_metadata", "-1", "-map_chapters", "-1", "-map_metadata:s", "-1", *filters]
    if rebuild_headers:
        content = _ffmpeg_rewrite(content, suffix_or_mime, workdir, options, output_muxer="mpegts")
        if _probe_streams(content) != streams:
            raise MediaMetadataError("codec header rewrite changed streams")
    if muxer in ("mp4", "mov"):
        # The bitrate hint box would carry the source's figures through a filtered
        # copy, so a second strip would change it: leave it out to keep strip idempotent.
        options += ["-write_btrt", "0"]
    return _ffmpeg_rewrite(content, suffix_or_mime, workdir, options)


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
_ARG_LIMIT = 100_000  # bytes in one ``-metadata key=value`` argument; the kernel allows 131072
# Leave headroom below Linux's usual 2 MiB argv + environment limit for the
# environment, argument pointers, media paths, and the fixed ffmpeg options.
_TOTAL_ARG_LIMIT = 1_000_000


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
    if suffix == ".wav":
        joined = "\n".join(f"{k}={v}" for k, v in tags.items())
        tags = {_WAV_INFO_FALLBACK_KEY: joined}
    if suffix in _MP4_LIKE:
        flags = ["-movflags", "+use_metadata_tags"]
    else:
        flags = []
    # NUL cannot be passed through execve either; never leak ValueError from
    # subprocess or silently truncate it in an ffmetadata file.
    if any("\x00" in k or "\x00" in v for k, v in tags.items()):
        raise MediaMetadataError("NUL cannot be preserved in media metadata")
    sizes = [len(f"{k}={v}".encode("utf-8")) for k, v in tags.items()]
    # Include each -metadata flag and both argument terminators.
    if (all(size <= _ARG_LIMIT for size in sizes)
            and sum(size + len("-metadata") + 2 for size in sizes) <= _TOTAL_ARG_LIMIT):
        extra = [arg for k, v in tags.items() for arg in ("-metadata", f"{k}={v}")]
        return _ffmpeg_rewrite(content, suffix_or_mime, workdir, [*extra, *flags])
    # Linux caps one command-line argument at 128 KiB, and a chained clip's
    # workflow tag is larger. Such tags travel in an anonymous ffmetadata file.
    # ``-map_metadata`` replaces the global tags, so the file also holds the
    # tags already in the container.
    merged = {**_format_tags(content, workdir), **tags}
    return _ffmpeg_rewrite(content, suffix_or_mime, workdir, ["-map_metadata:g", "1:g", *flags],
                           metadata_file=_ffmetadata(merged))


def _ffmetadata(tags: Mapping[str, str]) -> bytes:
    """FFMETADATA1 text: ``=``, ``;``, ``#``, ``\\`` and newline are escaped with a backslash."""
    # The reader terminates at CR/NUL even when escaped. Check merged tags,
    # including existing container tags and the joined WAV comment.
    for control, cause in (("\r", "carriage return"), ("\x00", "NUL")):
        if any(control in k or control in v for k, v in tags.items()):
            raise MediaMetadataError(
                f"{cause} cannot be preserved in ffmetadata; metadata exceeds command-line limits")
    def esc(text: str) -> str:
        return "".join("\\" + c if c in "=;#\\\n" else c for c in text)
    return (";FFMETADATA1\n" + "".join(f"{esc(k)}={esc(v)}\n" for k, v in tags.items())).encode("utf-8")


def _format_tags(content: bytes, workdir: Path | None) -> dict[str, str]:
    """The container's own (format-level) tags, keys as stored."""
    import json

    if shutil.which("ffprobe") is None:
        raise MediaMetadataError("ffprobe not found")
    with _media_buffers(content) as (src, _dst):
        proc = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                     f"/proc/self/fd/{src.fileno()}"], pass_fds=(src.fileno(),))
        if proc.returncode != 0:
            raise MediaMetadataError(f"ffprobe failed ({proc.returncode})")
        data = json.loads(proc.stdout or b"{}")
    return dict((data.get("format") or {}).get("tags") or {})


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
