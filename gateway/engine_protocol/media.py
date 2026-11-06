"""Bridge protocol binary results to the existing owner-scoped asset cache."""
from __future__ import annotations

import asyncio
import mimetypes

from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError
from gateway.engine_protocol.types import AssetRef


class GatewayAssetStore:
    def __init__(self, *, store, path_for, owner):
        self._store = store
        self._path_for = path_for
        self._owner = owner

    async def save(self, chunks, *, content_type, max_bytes, name=None):
        data = bytearray()
        async for chunk in chunks:
            if len(data) + len(chunk) > max_bytes:
                raise BudgetExceeded('max_asset_bytes', 'The media exceeds the asset size limit.')
            data.extend(chunk)
        suffix = mimetypes.guess_extension(content_type) or '.bin'
        kind = {'image':'img','audio':'aud','video':'vid'}.get(content_type.split('/')[0])
        if kind is None:
            raise ProtocolError('The protocol did not return an image, audio or video.', code='invalid_media_type')
        asset_id = await asyncio.to_thread(self._store, bytes(data), kind=kind, suffix=suffix, owner_key_hash=self._owner)
        return AssetRef(asset_id, content_type, len(data), name)

    async def read(self, ref, *, max_bytes):
        path = self._path_for(ref.id)
        if path is None:
            raise ProtocolError('The saved media asset is missing.', code='asset_missing')
        if path.stat().st_size > max_bytes:
            raise BudgetExceeded('max_asset_bytes', 'The media exceeds the asset size limit.')
        return await asyncio.to_thread(path.read_bytes)
