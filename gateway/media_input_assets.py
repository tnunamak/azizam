"""The asset store, seen through the resolver's port (``gateway/media_inputs.py``).

The store itself lives in ``proxy.py``: its cache directory, its sidecar files with the owner list, its read check and
its metadata stripping. This class holds no store logic of its own. It adds the two things the resolver needs on top: the
id the store will give some bytes (so an ingest marker can name the file before it is written), and the removal of an
owner's claim on a file the ingest created.

It names no workflow, model, engine or modality.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any


class CacheAssetPort:
    def __init__(
        self,
        *,
        parse: Callable[[str], tuple[str, str] | None],
        path_for: Callable[[str], Path | None],
        may_read: Callable[[str, Any], bool],
        deliver: Callable[[Path], bytes],
        owner_key: Callable[[Any], str | None],
        strip: Callable[[bytes, str], bytes],
        store: Callable[..., str],
        read_metadata: Callable[[str], Mapping[str, Any]],
        write_metadata: Callable[[str, Mapping[str, Any]], None],
        metadata_path: Callable[[str], Path | None],
        lock: AbstractContextManager[Any],
    ) -> None:
        self._parse = parse
        self._path_for = path_for
        self._may_read = may_read
        self._deliver = deliver
        self._owner_key = owner_key
        self._strip = strip
        self._store = store
        self._read_metadata = read_metadata
        self._write_metadata = write_metadata
        self._metadata_path = metadata_path
        self._lock = lock

    def parse(self, asset_id: str) -> tuple[str, str] | None:
        return self._parse(asset_id)

    def exists(self, asset_id: str) -> bool:
        path = self._path_for(asset_id)
        return path is not None and path.exists()

    def size(self, asset_id: str) -> int | None:
        path = self._path_for(asset_id)
        try:
            return path.stat().st_size if path is not None else None
        except OSError:
            return None

    def may_read(self, asset_id: str, auth: Any) -> bool:
        return self._may_read(asset_id, auth)

    def read(self, asset_id: str) -> bytes:
        path = self._path_for(asset_id)
        if path is None:
            raise FileNotFoundError(asset_id)
        return self._deliver(path)

    def owner_key(self, auth: Any) -> str | None:
        return self._owner_key(auth)

    def planned_id(self, data: bytes, suffix: str, kind: str) -> str:
        return f"{kind}_{hashlib.sha256(self._strip(data, suffix)).hexdigest()}"

    def put(self, data: bytes, *, kind: str, suffix: str, owner_key: str | None) -> str:
        # An input of a run is not a creation: it has no maker and keeps no prompt.
        return self._store(data, kind=kind, suffix=suffix, owner_key_hash=owner_key, maker_person_id=None,
                           keep_prompt=False)

    def discard(self, asset_id: str, owner_key: str) -> None:
        with self._lock:
            metadata = dict(self._read_metadata(asset_id))
            owners = [o for o in metadata.get("owner_key_hashes") or [] if isinstance(o, str) and o != owner_key]
            if owners or metadata.get("maker_person_ids"):
                # Another owner, or a person who made the file, still holds it: only this claim goes.
                metadata["owner_key_hashes"] = sorted(set(owners))
                self._write_metadata(asset_id, metadata)
                return
            for path in (self._path_for(asset_id), self._metadata_path(asset_id)):
                if path is not None:
                    path.unlink(missing_ok=True)
