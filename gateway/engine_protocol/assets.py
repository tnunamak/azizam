"""Asset stores for the executor: where uploads are read from and binary answers are saved.

``AssetStore`` is the narrow interface the executor needs; the gateway's media
store is wired behind it by the adapter that serves media. ``MemoryAssetStore``
is the in-process store used by tests and by callers that need no persistence.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError
from gateway.engine_protocol.types import AssetRef


# What an engine's Content-Type claims is not what Azizam serves. Each row is a kind of file the bytes can show, with
# the type served by default and the other names an engine may use for it (a label is kept only inside its row).
_FAMILIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("image/png", frozenset({"image/png"})),
    ("image/jpeg", frozenset({"image/jpeg"})),
    ("image/gif", frozenset({"image/gif"})),
    ("image/webp", frozenset({"image/webp"})),
    ("image/avif", frozenset({"image/avif"})),
    ("audio/wav", frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})),
    ("audio/flac", frozenset({"audio/flac", "audio/x-flac"})),
    ("audio/ogg", frozenset({"audio/ogg", "audio/opus", "video/ogg"})),
    ("audio/mpeg", frozenset({"audio/mpeg", "audio/mp3"})),
    ("audio/aac", frozenset({"audio/aac"})),
    ("video/mp4", frozenset({"video/mp4", "audio/mp4"})),
    ("audio/mp4", frozenset({"audio/mp4", "audio/x-m4a", "video/mp4"})),
    ("video/quicktime", frozenset({"video/quicktime"})),
    ("video/webm", frozenset({"video/webm", "audio/webm"})),
)
_FAMILY = dict(_FAMILIES)
_UNSNIFFABLE = frozenset({"application/octet-stream", "audio/pcm"})  # raw bytes have no signature; served as a download
ALLOWED_MEDIA_TYPES = frozenset().union(*(family for _, family in _FAMILIES)) | _UNSNIFFABLE


def sniff_media_type(head: bytes) -> str | None:
    """The type the first bytes of a file show, or ``None``. Needs about 12 bytes."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand == b"qt  ":
            return "video/quicktime"
        return "audio/mp4" if brand in (b"M4A ", b"M4B ", b"M4P ") else "video/mp4"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:  # an MPEG audio frame header
        if head[1] & 0xF6 == 0xF0:
            return "audio/aac"  # ADTS: layer bits 00
        if (head[1] >> 3) & 3 != 1 and (head[1] >> 1) & 3 != 0:
            return "audio/mpeg"
    return None


def served_media_type(declared: str | None, head: bytes) -> str | None:
    """The one type an asset is served as, or ``None`` when the answer must be refused.

    The claimed type must be on the allowlist (parameters are dropped, never kept). The first bytes then decide: the
    claimed type stands only when it names the kind of file the bytes show, otherwise the sniffed type is served. Bytes
    with no signature are served only as raw data (``application/octet-stream`` or ``audio/pcm``), never as a type that
    a browser would run or render as a page.
    """
    claimed = (declared or "").split(";", 1)[0].strip().lower()
    if claimed and claimed not in ALLOWED_MEDIA_TYPES:
        return None
    sniffed = sniff_media_type(head)
    if sniffed is not None:
        return claimed if claimed in _FAMILY[sniffed] else sniffed
    if claimed in _UNSNIFFABLE:
        return claimed
    return "application/octet-stream" if not claimed else None



class AssetStore(Protocol):
    async def read(self, ref: AssetRef, *, max_bytes: int) -> bytes: ...

    async def save(self, chunks: AsyncIterator[bytes], *, content_type: str, max_bytes: int, name: str | None = None) -> AssetRef: ...


class MemoryAssetStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, data: bytes, content_type: str, name: str | None = None) -> AssetRef:
        ref = AssetRef(f"asset-{len(self._data) + 1}", content_type, len(data), name)
        self._data[ref.id] = data
        return ref

    def get(self, ref: AssetRef) -> bytes:
        return self._data[ref.id]

    def count(self) -> int:
        return len(self._data)

    async def read(self, ref: AssetRef, *, max_bytes: int) -> bytes:
        try:
            data = self._data[ref.id]
        except KeyError:
            raise ProtocolError(f"Asset {ref.id} is not in the store.", code="asset_missing") from None
        if len(data) > max_bytes:
            raise BudgetExceeded("max_upload_bytes", f"Asset {ref.id} is larger than the {max_bytes} byte upload budget.")
        return data

    async def save(self, chunks: AsyncIterator[bytes], *, content_type: str, max_bytes: int, name: str | None = None) -> AssetRef:
        parts: list[bytes] = []
        total = 0
        async for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                raise BudgetExceeded("max_asset_bytes", f"A binary answer is larger than the {max_bytes} byte asset budget.")
            parts.append(chunk)
        return self.put(b"".join(parts), content_type, name)
