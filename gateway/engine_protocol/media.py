"""Bridge protocol binary results to the existing owner-scoped asset cache."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from gateway import asset_types
from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError
from gateway.engine_protocol.types import AssetRef


class GatewayAssetStore:
    def __init__(self, *, store, path_for, owner, origin: Mapping[str, Any] | None = None):
        self._store = store
        self._path_for = path_for
        self._owner = owner
        self._origin = dict(origin) if origin else None      # what made the file; the Library shows it

    async def save(self, chunks, *, content_type, max_bytes, name=None):
        data = bytearray()
        async for chunk in chunks:
            if len(data) + len(chunk) > max_bytes:
                raise BudgetExceeded('max_asset_bytes', 'The media exceeds the asset size limit.')
            data.extend(chunk)
        suffix = asset_types.suffix_for_media_type(content_type)
        kind = asset_types.asset_prefix_for_media_type(content_type)
        extra = {'origin': self._origin} if self._origin else {}
        asset_id = await asyncio.to_thread(self._store, bytes(data), kind=kind, suffix=suffix, owner_key_hash=self._owner, **extra)
        return AssetRef(asset_id, content_type, len(data), name)

    async def read(self, ref, *, max_bytes):
        path = self._path_for(ref.id)
        if path is None:
            raise ProtocolError('The saved media asset is missing.', code='asset_missing')
        if path.stat().st_size > max_bytes:
            raise BudgetExceeded('max_asset_bytes', 'The media exceeds the asset size limit.')
        return await asyncio.to_thread(path.read_bytes)
