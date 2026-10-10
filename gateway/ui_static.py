"""Static file delivery policy for the exported browser UI."""
from __future__ import annotations

from email.utils import parsedate_to_datetime
from functools import lru_cache
import gzip
import os
from pathlib import Path
import re
from typing import Any

import anyio
import brotli
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles


_COMPRESSIBLE_TYPES = frozenset({
    ".css", ".html", ".js", ".json", ".map", ".svg", ".txt",
})
_HASHED_FILENAME = re.compile(
    r"(?:^|[.-])(?=[A-Za-z0-9_-]{8,}(?:[.-]|$))"
    r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+(?=[.-]|$)"
)
_ONE_YEAR = "public, max-age=31536000, immutable"


@lru_cache(maxsize=128)
def _compressed_file(path: str, mtime_ns: int, ctime_ns: int, size: int,
                     encoding: str) -> bytes:
    """Compress one file version once; stat fields prevent stale cache hits."""
    data = Path(path).read_bytes()
    if encoding == "br":
        return brotli.compress(data, quality=5)
    return gzip.compress(data, mtime=0)


def _accepted_quality(header: str, coding: str) -> float:
    wildcard: float | None = None
    for item in header.split(","):
        parts = item.strip().lower().split(";")
        name, parameters = parts[0].strip(), parts[1:]
        quality = 1.0
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if key == "q" and separator:
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                if not 0 <= quality <= 1:
                    quality = 0.0
        if name == coding:
            return quality
        if name == "*":
            wildcard = quality
    return wildcard or 0.0


def _identity_quality(header: str) -> float:
    wildcard_quality: float | None = None
    for item in header.split(","):
        parts = item.strip().lower().split(";")
        name, parameters = parts[0].strip(), parts[1:]
        quality = 1.0
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if key == "q" and separator:
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                if not 0 <= quality <= 1:
                    quality = 0.0
        if name == "identity":
            return quality
        if name == "*":
            wildcard_quality = quality
    # Identity is acceptable by default; `*;q=0` also excludes it unless
    # identity has an explicit quality above.
    return 0.0 if wildcard_quality == 0 else 1.0


def _choose_encoding(header: str) -> str | None:
    choices = [("br", _accepted_quality(header, "br")),
               ("gzip", _accepted_quality(header, "gzip")),
               ("identity", _identity_quality(header))]
    coding, quality = max(choices, key=lambda choice: choice[1])
    return coding if quality > 0 else None


def _etag_matches(header: str, etag: str) -> bool:
    if not header:
        return False
    target = etag.removeprefix("W/")
    return any(item.strip() == "*" or item.strip().removeprefix("W/") == target
               for item in header.split(","))


class UIStaticFiles(StaticFiles):
    """Serve UI assets with cached compression and browser revalidation."""

    def file_response(self, full_path: Any, stat_result: os.stat_result,
                      scope: dict[str, Any], status_code: int = 200) -> Response:
        # Conditional requests need to use the selected representation's ETag,
        # which get_response computes after choosing identity, Brotli, or gzip.
        return FileResponse(full_path, status_code=status_code,
                            stat_result=stat_result)

    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code != 200 or not isinstance(response, FileResponse):
            return response

        request_headers = Headers(scope=scope)
        method = scope["method"]
        file_path = Path(response.path)
        stat_result = response.stat_result
        response_headers = dict(response.headers.items())
        response_headers["cache-control"] = (
            _ONE_YEAR if self._is_hashed_ui_asset(scope, file_path.name)
            else "no-cache"
        )

        # Leave byte ranges on Starlette's identity FileResponse path. This
        # keeps Content-Range and If-Range tied to the original file bytes.
        range_request = "range" in request_headers
        compressible = file_path.suffix.lower() in _COMPRESSIBLE_TYPES
        encoding = None
        response_headers["vary"] = self._merge_vary(
            response_headers.get("vary"), "Accept-Encoding"
        )
        accept_encoding = ", ".join(request_headers.getlist("accept-encoding"))
        if compressible:
            encoding = (
                ("identity" if _identity_quality(accept_encoding) > 0 else None)
                if range_request else _choose_encoding(accept_encoding)
            )
            if encoding is None:
                return Response(status_code=406, headers={
                    "cache-control": "no-store",
                    "vary": response_headers["vary"],
                })
            if encoding == "identity":
                encoding = None
        elif _identity_quality(accept_encoding) == 0:
            return Response(status_code=406, headers={
                "cache-control": "no-store",
                "vary": response_headers["vary"],
            })

        etag = response_headers["etag"]
        if encoding:
            etag = f'{etag[:-1]}-{encoding}"'
            response_headers["content-encoding"] = encoding
        response_headers["etag"] = etag

        if self._is_not_modified(request_headers, etag,
                                 response_headers.get("last-modified", "")):
            not_modified_headers = {
                key: value for key, value in response_headers.items()
                if key.lower() not in {"content-length", "accept-ranges"}
            }
            return Response(status_code=304, headers=not_modified_headers)

        if range_request:
            response.headers.update(response_headers)
            return response

        if not encoding:
            response.headers.update(response_headers)
            return response

        body = await anyio.to_thread.run_sync(
            _compressed_file,
            str(file_path), stat_result.st_mtime_ns,
            stat_result.st_ctime_ns, stat_result.st_size, encoding,
        )

        content_length = len(body)
        headers = {
            key: value for key, value in response_headers.items()
            if key.lower() not in {"content-length", "content-encoding"}
        }
        if encoding:
            headers["content-encoding"] = encoding
        result = Response(content=b"" if method == "HEAD" else body,
                          status_code=200, headers=headers)
        result.headers["content-length"] = str(content_length)
        return result

    @staticmethod
    def _is_hashed_ui_asset(scope: dict[str, Any], filename: str) -> bool:
        return (scope["path"].startswith("/ui/_next/static/")
                and _HASHED_FILENAME.search(filename) is not None)

    @staticmethod
    def _merge_vary(current: str | None, value: str) -> str:
        values = [part.strip() for part in (current or "").split(",") if part.strip()]
        if not any(part.lower() == value.lower() for part in values):
            values.append(value)
        return ", ".join(values)

    @staticmethod
    def _is_not_modified(headers: Headers, etag: str, last_modified: str) -> bool:
        if "if-none-match" in headers:
            return _etag_matches(headers["if-none-match"], etag)
        if "if-modified-since" not in headers or not last_modified:
            return False
        try:
            modified = parsedate_to_datetime(last_modified).timestamp()
            since = parsedate_to_datetime(headers["if-modified-since"]).timestamp()
        except (TypeError, ValueError, OverflowError):
            return False
        return modified <= since
