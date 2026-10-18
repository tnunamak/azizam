"""Asset stores for the executor: where uploads are read from and binary answers are saved.

``AssetStore`` is the narrow interface the executor needs; the gateway's media
store is wired behind it by the adapter that serves media. ``MemoryAssetStore``
is the in-process store used by tests and by callers that need no persistence.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError
from gateway.engine_protocol.types import AssetRef


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
