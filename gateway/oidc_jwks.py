"""Bounded-lifetime signing keys, with one throttled refresh for rotation."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Callable

from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError
import httpx


@dataclass
class _Entry:
    keys: dict
    fetched_at: float
    refresh_attempted_at: float = float("-inf")


class JWKSCache:
    def __init__(self, *, ttl_seconds: float = 300, refresh_interval: float = 30,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.refresh_interval = refresh_interval
        self.clock = clock
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()

    async def _fetch(self, uri: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(uri)
            response.raise_for_status()
            keys = response.json()
        if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
            raise ValueError("Invalid identity provider signing keys")
        return keys

    async def verify(self, token: str, *, uri: str, leeway: int) -> dict:
        try:
            header_part = token.split(".", 1)[0]
            if len(header_part) > 8192:
                raise ValueError("Invalid identity token")
            header = json.loads(base64.urlsafe_b64decode(header_part + "=" * (-len(header_part) % 4)))
            kid = header.get("kid")
            if kid is not None and not isinstance(kid, str):
                raise ValueError("Invalid identity token")
            lock = self._locks.setdefault(uri, asyncio.Lock())
            async with lock:
                now = self.clock()
                entry = self._entries.get(uri)
                if entry is None or now - entry.fetched_at >= self.ttl_seconds:
                    keys = await self._fetch(uri)
                    entry = _Entry(keys, self.clock())
                    self._entries[uri] = entry
                known = kid is None or any(key.get("kid") == kid for key in entry.keys["keys"])
                if not known and now - entry.refresh_attempted_at >= self.refresh_interval:
                    # Record failures too: invalid kids cannot force outbound fetches.
                    entry.refresh_attempted_at = now
                    keys = await self._fetch(uri)
                    entry.keys, entry.fetched_at = keys, self.clock()
                if kid is not None and not any(key.get("kid") == kid for key in entry.keys["keys"]):
                    raise ValueError("Invalid identity token")
                key_set = JsonWebKey.import_key_set(entry.keys)
            claims = jwt.decode(token, key_set)
            claims.validate(leeway=leeway)
            return dict(claims)
        except (JoseError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
            raise ValueError("Invalid identity token") from exc
