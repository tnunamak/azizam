"""One non-retried multipart POST to a service, returning its raw success bytes.

The executor knows nothing about what the service returns. A 2xx body is handed back as bytes,
bounded. Every failure is one of four typed kinds, and an upstream error body is read only up to
a small bound and then discarded, so the caller never sees it.
"""

from __future__ import annotations

from typing import Mapping

import httpx

from gateway.mesh_manifest import ImageAsset

KIND_CONNECT = "connect"
KIND_TIMEOUT = "timeout"
KIND_UPSTREAM = "upstream"
KIND_MALFORMED = "malformed"

_DISCARD_LIMIT = 4096


class MultipartFailure(Exception):
    """A typed downstream failure. ``status`` is set only for ``upstream``."""

    def __init__(self, kind: str, *, status: int | None = None) -> None:
        super().__init__(kind if status is None else f"{kind} {status}")
        self.kind = kind
        self.status = status


async def post_multipart(
    client: httpx.AsyncClient,
    url: str,
    *,
    image_field: str,
    image: ImageAsset,
    fields: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    files = {image_field: (image.filename, image.data, image.content_type)}
    limits = httpx.Timeout(timeout_seconds, connect=5.0)
    try:
        async with client.stream("POST", url, files=files, data=dict(fields), timeout=limits) as response:
            if 200 <= response.status_code < 300:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise MultipartFailure(KIND_MALFORMED)
                return bytes(body)
            discarded = 0
            async for chunk in response.aiter_bytes():
                discarded += len(chunk)
                if discarded >= _DISCARD_LIMIT:
                    break
            raise MultipartFailure(KIND_UPSTREAM, status=response.status_code)
    except MultipartFailure:
        raise
    except httpx.TimeoutException as error:
        if isinstance(error, httpx.ConnectTimeout):
            raise MultipartFailure(KIND_CONNECT) from error
        raise MultipartFailure(KIND_TIMEOUT) from error
    except httpx.ConnectError as error:
        raise MultipartFailure(KIND_CONNECT) from error
    except httpx.HTTPError as error:
        raise MultipartFailure(KIND_UPSTREAM) from error
