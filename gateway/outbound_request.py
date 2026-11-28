"""Credential and path boundaries for requests sent to inference services."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode


# Body framing belongs to the HTTP client. Cookies, gateway authentication,
# internal protocol headers and arbitrary client headers have no backend role.
BACKEND_HEADER_ALLOWLIST = frozenset({"content-type", "accept", "user-agent"})
_CREDENTIAL_QUERY_PARAMS = frozenset({
    "api_key", "api-key", "admin_key", "access_token", "authorization",
    "x-api-key", "x-admin-key",
})
_ENCODED_PATH_DELIMITERS = re.compile(r"%(?:2e|2f|5c|25)", re.IGNORECASE)


def backend_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy only representation headers; downstream auth is injected separately."""
    result = {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in BACKEND_HEADER_ALLOWLIST
    }
    # Relay callers use decoded httpx bytes. Avoid passing an encoding header
    # that describes a different representation to the downstream client.
    result["accept-encoding"] = "identity"
    return result


def backend_query_string(raw_query: str) -> str:
    """Remove gateway credentials, including encoded and repeated query keys."""
    return urlencode([
        (key, value)
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if key.lower() not in _CREDENTIAL_QUERY_PARAMS
    ])


def canonical_native_path(path: str, *, raw_path: bytes | None = None) -> str:
    """Reject paths whose interpretation can change between policy and HTTP I/O."""
    raw = raw_path.decode("latin-1") if raw_path is not None else path
    canonical = path if path.startswith("/") else f"/{path}"
    for value in (raw, canonical):
        if (
            "\\" in value
            or "//" in value
            or _ENCODED_PATH_DELIMITERS.search(value)
            or any(segment in {".", ".."} for segment in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "?" in value
            or "#" in value
        ):
            raise ValueError("Native paths must not contain dot segments, encoded delimiters, backslashes or double slashes.")
    return canonical
