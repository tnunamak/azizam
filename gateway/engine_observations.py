"""Optional, declared facts about an engine. A failed or absent probe is unknown."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from gateway.engine_credentials import credential_headers


@dataclass(frozen=True)
class Observation:
    known: bool
    value: Any = None


UNKNOWN = Observation(False)


def _select(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("JSON selector must be a pointer")
    values = [document]
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        next_values = []
        for value in values:
            if key == "*" and isinstance(value, list):
                next_values.extend(value)
            elif isinstance(value, dict) and key in value:
                next_values.append(value[key])
            elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
                next_values.append(value[int(key)])
        values = next_values
        if not values:
            raise KeyError(pointer)
    return values if "*" in pointer.split("/") else values[0]


class ObservationReader:
    """Read one declared HTTP fact without guessing from another fact."""

    def __init__(self, *, client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient) -> None:
        self._client_factory = client_factory

    async def read(self, service: Mapping[str, Any], name: str) -> Observation:
        declared = service.get("observations")
        spec = declared.get(name) if isinstance(declared, Mapping) else None
        if not isinstance(spec, Mapping):
            return UNKNOWN
        http = spec.get("http")
        connection = service.get("connection") or {}
        address = connection.get("address") if isinstance(connection, Mapping) else None
        if not isinstance(http, Mapping) or not isinstance(address, str):
            return UNKNOWN
        path = http.get("path")
        expected = http.get("expect", 200)
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(expected, int):
            return UNKNOWN
        origin = urlsplit(address)
        if origin.scheme not in {"http", "https"} or not origin.netloc:
            return UNKNOWN
        url = urljoin(address.rstrip("/") + "/", path.lstrip("/"))
        if urlsplit(url).netloc != origin.netloc:
            return UNKNOWN
        try:
            timeout_s = max(0.01, min(float(http.get("timeout_s", 2)), 30))
            headers = credential_headers(service, str(spec.get("credential_class") or "control"))
            async with asyncio.timeout(timeout_s):
                async with self._client_factory(follow_redirects=False, trust_env=False) as client:
                    async with client.stream("GET", url, headers=headers, timeout=timeout_s) as response:
                        if response.status_code != expected:
                            return UNKNOWN
                        pointer = http.get("select", spec.get("select"))
                        if pointer is None:
                            return Observation(True, True) if name == "process_ready" else UNKNOWN
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > 1048576:
                                return UNKNOWN
            return Observation(True, _select(json.loads(chunks), str(pointer)))
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return UNKNOWN

    @staticmethod
    def inventory_means(observations: Mapping[str, Any]) -> str:
        inventory = observations.get("inventory")
        means = inventory.get("means") if isinstance(inventory, Mapping) else None
        return means if means in {"available", "loaded"} else "unknown"
