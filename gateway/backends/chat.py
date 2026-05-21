"""OpenAI-compatible chat/completions backend adapter helpers.

This module intentionally contains pure request/response shaping helpers.
Proxy route ownership stays in ``proxy.py`` until the backend execution seam is
large enough to extract without changing behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import httpx


@dataclass(frozen=True)
class ChatLoopbackRequest:
    """HTTP request contract for in-process chat/completions forwarding."""

    url: str
    headers: dict[str, str]
    body: bytes
    timeout: httpx.Timeout


def build_chat_loopback_request(
    *,
    proxy_port: int,
    body: Mapping[str, Any],
    internal_forward_header: str,
    internal_forward_secret: str,
    source_headers: Mapping[str, str],
    read_timeout: float,
) -> ChatLoopbackRequest:
    """Build the loopback request used by protocol frontends for chat.

    The output mirrors the historical ``proxy._forward_chat_completions`` wire
    contract: JSON is encoded with ``json.dumps(...).encode("utf-8")`` and only
    proxy auth headers are forwarded.
    """

    headers = {
        "content-type": "application/json",
        internal_forward_header: internal_forward_secret,
    }
    auth_header = _get_header(source_headers, "authorization")
    if auth_header:
        headers["authorization"] = auth_header
    api_key_header = _get_header(source_headers, "x-api-key")
    if api_key_header:
        headers["x-api-key"] = api_key_header

    return ChatLoopbackRequest(
        url=f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
        headers=headers,
        body=json.dumps(body).encode("utf-8"),
        timeout=httpx.Timeout(
            connect=10.0,
            read=read_timeout,
            write=60.0,
            pool=None,
        ),
    )


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    lower_name = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lower_name:
            return candidate
    return None
